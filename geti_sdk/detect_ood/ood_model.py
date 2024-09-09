# Copyright (C) 2024 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import json
import logging
import os
import tempfile
from abc import ABCMeta, abstractmethod
from enum import Enum
from typing import List, Union

import cv2
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from tqdm.auto import tqdm

from geti_sdk import Geti
from geti_sdk.data_models import Prediction, Project
from geti_sdk.data_models.enums.task_type import TaskType
from geti_sdk.data_models.project import Dataset
from geti_sdk.deployment import Deployment
from geti_sdk.rest_clients import AnnotationClient, ImageClient, ModelClient

from .utils import (
    CutoutTransform,
    calculate_entropy_nearest_neighbours,
    fit_pca_model,
    fre_score,
    get_deployment_with_xai_head,
    normalise_features,
    perform_knn_indexing,
    perform_knn_search,
    stratified_selection,
)

# Names of the Geti datasets that are used as reference/training data for the COOD model, if available
ID_DATASET_NAMES = ["Dataset"]
OOD_DATASET_NAMES = ["OOD reference dataset"]


class DistributionDataItemPurpose(Enum):
    """
    Enum to represent the purpose of the DistributionDataItem.
    This is used during splitting of the data into TRAIN or TEST
    """

    TRAIN = "train"
    TEST = "test"


# TODO[OOD] : If required, a DistributionData class can be created which is a collection of DistributionDataItems
class DistributionDataItem:
    """
    A class to store the data for the COOD model.
    An DistributionDataItem for an image contains the following:
    - media_name: Name of the media (optional)
    - image_path: Path to the image (optional)
    - annotated_label: Annotated label for the image (optional)
    - raw_prediction: Prediction object for the image (required)
    - feature_vector: Feature vector extracted from the image (extracted from raw_prediction)

    All OOD models take a list of DistributionDataItems as input for training and inference.
    """

    def __init__(
        self,
        raw_prediction: Prediction,
        media_name: Union[str, None],
        media_path: Union[str, None],
        annotated_label: Union[str, None],
        normalise_feature_vector: bool = True,
        purpose: Union[DistributionDataItemPurpose, None] = None,
    ):
        self.media_name = media_name
        self.image_path = media_path
        self.annotated_label = annotated_label
        self.raw_prediction = raw_prediction
        self.purpose = purpose

        feature_vector = raw_prediction.feature_vector

        if len(feature_vector.shape) != 1:
            feature_vector = feature_vector.flatten()

        if normalise_feature_vector:
            feature_vector = normalise_features(feature_vector)[0]

        self._normalise_feature_vector = normalise_feature_vector
        self.feature_vector = feature_vector
        self.max_prediction_probability = (
            raw_prediction.annotations[0].labels[0].probability,
        )
        self.predicted_label = raw_prediction.annotations[0].labels[0].name

    def __repr__(self):
        """
        Return a string representation of the DistributionDataItem.
        """
        return (
            f"DataItem(media_name={self.media_name}, "
            f"shape(feature_vector)={self.feature_vector.shape}), "
            f"feature_vector normalised={self.is_feature_vector_normalised})"
        )

    @property
    def is_feature_vector_normalised(self) -> bool:
        """
        Return True if the feature vector is normalised.
        """
        return self._normalise_feature_vector


class COODModel:
    """
    Out-of-distribution detection model. Uses the Combined out-of-distribution (COOD) detection
    algorithm (see : https://arxiv.org/abs/2403.06874).

    Uses a temporary directory for storing data with the following structure:
    temp_dir
    ├── ood_detection
    │   └── project_name
    │       ├── data
    │       │   ├── Geti_dataset_1
    │       │   │   ├── images
    │       │   │   └── annotations
    │       │   ├── geti_dataset_2
    │       │   │   ├── images
    │       │   │   └── annotations
    │       │   ├── ood_images (if generated)
    │       │   │   ├── image_001.jpg
    │       │   │   ├── image_002.jpg
    │       │       └── image_003.jpg

    """

    def __init__(
        self,
        geti: Geti,
        project: Union[str, Project],
        deployment: Deployment = None,
        ood_images_dir: str = None,
        workspace_dir: str = None,
    ):
        """
        Model for Combined Out-of-Distribution (COOD) detection .
        :param geti: Geti instance representing the GETi server from which the project is to be used.
        :param project: Project or project name to use to fetch the deployment and the in-distribution data.
        The project must exist on the specified Geti instance and should have at least one trained model.
        :param deployment: Deployment to use for learning the data distribution. If None, a deployment with an XAI head is
        automatically selected from the project. If this COODModel is used in an OODTrigger,then make sure that
        the same deployment is used for post-inference hook.
        :param ood_images_dir: Path to the directory containing out-of-distribution images for training. If
        not provided, near-OOD images are generated by applying strong corruptions to the in-distribution images.
        """
        self.geti = geti

        self.id_distribution_data = List[DistributionDataItem]
        self.ood_distribution_data = List[DistributionDataItem]
        self.ood_reference_images_dir = ood_images_dir

        self.ood_classifier = None  # The COOD random forest classifier

        self.train_test_split = 0.90  # The ratio of train-test split

        if isinstance(project, str):
            project_name = project
            self.project = geti.get_project(project_name=project_name)
        else:
            self.project = project

        self.model_client = ModelClient(
            session=self.geti.session,
            workspace_id=self.geti.workspace_id,
            project=self.project,
        )
        self.image_client = ImageClient(
            session=self.geti.session,
            workspace_id=self.geti.workspace_id,
            project=self.project,
        )

        self.annotation_client = AnnotationClient(
            session=self.geti.session,
            workspace_id=self.geti.workspace_id,
            project=self.project,
        )

        if workspace_dir is None:
            workspace_dir = tempfile.mkdtemp()

        self.workspace_dir = os.path.join(
            workspace_dir, "ood_detection", self.project.name
        )

        if not os.path.exists(self.workspace_dir):
            os.makedirs(self.workspace_dir)

        self.data_dir = os.path.join(self.workspace_dir, "data")

        # Checks if project is a single-task classification project
        self._check_project_fit()

        if deployment is None:
            # If no deployment is provided, select an XAI model with the highest accuracy to be deployed
            self.deployment = get_deployment_with_xai_head(
                geti=self.geti, model_client=self.model_client
            )
        else:
            if not deployment.models[0].has_xai_head:
                raise ValueError(
                    "The provided deployment does not have an model with an XAI head."
                    "Please reconfigure the deployment to include a model with an XAI head "
                    "(OptimizedModel.has_xai_head must be True). "
                    "Hint : You can use the get_deployment_with_xai_head() method from detect_ood.utils"
                )

            self.deployment = deployment

        if not self.deployment.are_models_loaded:
            self.deployment.load_inference_models(device="CPU")

        logging.info(
            f"Building Combined OOD detection model for Intel® Geti™ project `{self.project.name}`."
        )

        # The transformation to apply to in-distribution images to generate near-OOD images
        # if no OOD images are provided
        self.corruption_transform = (
            CutoutTransform() if self.ood_reference_images_dir is None else None
        )

        # Download, extract, and prepare the in-distribution and out-of-distribution data
        distribution_data = self._prepare_id_ood_data()
        self.id_distribution_data = distribution_data["id_data"]
        self.ood_distribution_data = distribution_data["ood_data"]

        # Split the data into train and test sets. The COOD model once trained (on the train split) will be evaluated on
        # the test split. This accuracy is indicated to the user and if a user feels the accuracy is too low,
        # they can create a new COOD model with more/better data.

        self._split_data(
            data=self.id_distribution_data,
            stratified=True,
            split_ratio=self.train_test_split,
        )
        self._split_data(
            data=self.ood_distribution_data,
            stratified=False,
            split_ratio=self.train_test_split,
        )

        # A dict consisting smaller OOD models (FRE, EnWeDi, etc)
        self.sub_models = {
            "knn_based": KNNBasedOODModel(knn_k=10),
            "class_fre": ClassFREBasedModel(n_components=0.995),
            "global_fre": GlobalFREBasedModel(n_components=0.995),
            "max_softmax_probability": ProbabilityBasedModel(),
        }

        train_id_data = [
            item
            for item in self.id_distribution_data
            if item.purpose == DistributionDataItemPurpose.TRAIN
        ]

        train_ood_data = [
            item
            for item in self.ood_distribution_data
            if item.purpose == DistributionDataItemPurpose.TRAIN
        ]

        self._train(train_id_data=train_id_data, train_ood_data=train_ood_data)
        logging.info("COOD Model is trained and ready for inference.")

        test_id_data = [
            item
            for item in self.id_distribution_data
            if item.purpose == DistributionDataItemPurpose.TEST
        ]
        test_ood_data = [
            item
            for item in self.ood_distribution_data
            if item.purpose == DistributionDataItemPurpose.TEST
        ]

        accuracy = self._evaluate_model(
            test_id_data=test_id_data, test_ood_data=test_ood_data
        )
        logging.info(f"COOD Model Metrics on Test Data: {accuracy}")

    def _prepare_id_ood_data(self) -> dict:
        """
        Prepare the in-distribution and out-of-distribution data for training the COOD and the sub-models.
        For in-distribution data, all the images and annotations from the dataset names mentioned in ID_DATASET_NAMES
        are downloaded from the Geti Project.
        For out-of-distribution data, the same is done for the dataset names mentioned in OOD_DATASET_NAMES. If there is
        no out-of-distribution dataset, near-OOD images are generated by applying strong corruptions to the downloaded
        in-distribution images.
        All images are locally inferred to get prediction probabilities and feature vectors
        """
        datasets_in_project = self.project.datasets

        id_datasets_in_geti = []
        ood_datasets_in_geti = []

        # Figure out the datasets that can be used as a reference for in-distribution and out-of-distribution data
        for dataset in datasets_in_project:
            if dataset.name in ID_DATASET_NAMES:
                id_datasets_in_geti.append(dataset)
            elif dataset.name in OOD_DATASET_NAMES:
                ood_datasets_in_geti.append(dataset)

        if len(id_datasets_in_geti) == 0:
            raise ValueError(
                "Could not find any relevant datasets for in-distribution data. "
                "Please make sure that the project contains at least one dataset with the names: "
                f"{ID_DATASET_NAMES}."
            )

        id_distribution_data_items = []  # List[DistributionDataItem]
        for dataset in id_datasets_in_geti:
            logging.info(f"Extracting ID data from the Geti dataset '{dataset.name}'")
            id_distribution_data_items.extend(
                self._prepare_distribution_data(source=dataset)
            )

        # For OOD data, this is the order of preference :
        # 1) Use the reference images provided by the user
        # 2) If not provided, use the OOD datasets in the project
        # 3) If no OOD datasets are found, generate near-OOD images by applying strong
        # corruptions to the in-distribution images

        ood_distribution_data_items = []  # List[DistributionDataItem]
        if self.ood_reference_images_dir:
            # Case 1: Use the reference images provided by the user
            logging.info(
                f"Extracting OOD data from the provided reference images in {self.ood_reference_images_dir}"
            )

            ood_distribution_data_items = self._prepare_distribution_data(
                source=self.ood_reference_images_dir
            )

        else:
            if len(ood_datasets_in_geti) == 0:
                # Case 3: If no OOD datasets are found, generate near-OOD images by applying strong corruptions
                logging.info(
                    "No out-of-distribution datasets found in the project. "
                    "Generating near-OOD images by applying strong corruptions to the in-distribution images."
                )
                for dataset in id_datasets_in_geti:
                    ood_path = self._create_ood_images(reference_dataset=dataset)
                    ood_distribution_data_items.extend(
                        self._prepare_distribution_data(source=ood_path)
                    )

            else:
                # Case 2: Use the OOD datasets in the project
                for dataset in ood_datasets_in_geti:
                    logging.info(
                        f"Extracting OOD data from the Geti dataset '{dataset.name}'"
                    )
                    ood_distribution_data_items.extend(
                        self._prepare_distribution_data(source=dataset)
                    )

        logging.info(
            f"Number of in-distribution samples: {len(id_distribution_data_items)}"
        )
        logging.info(
            f"Number of out-of-distribution samples: {len(ood_distribution_data_items)}"
        )

        return {
            "id_data": id_distribution_data_items,
            "ood_data": ood_distribution_data_items,
        }

    def _train_sub_models(self, train_data: List[DistributionDataItem]) -> None:
        """
        Train the sub-models for OOD detection.
        Currently, all sub-models are trained on in-distribution data.
        """
        for sub_model in self.sub_models.values():
            sub_model.train(distribution_data=train_data)

    def _train(
        self,
        train_id_data: List[DistributionDataItem],
        train_ood_data: List[DistributionDataItem],
    ) -> None:
        """
        Train the COOD model using the RandomForestClassifier
        :param train_id_data: List of DistributionDataItems for in-distribution data (Train split)
        :param train_ood_data: List of DistributionDataItems for out-of-distribution data (Train split)
        """
        logging.info("Training COOD Model")
        logging.info(
            f"Training data: ID - {len(train_id_data)}, OOD - {len(train_ood_data)}"
        )
        self._train_sub_models(train_data=train_id_data)

        num_id_images = len(train_id_data)
        num_ood_images = len(train_ood_data)

        # Get scores from sub-models
        id_scores_all_sub_models = self._infer_sub_models(train_id_data)
        ood_scores_all_sub_models = self._infer_sub_models(train_ood_data)

        # Arrange features
        id_features = self._aggregate_sub_model_scores_into_cood_features(
            id_scores_all_sub_models, num_id_images
        )
        ood_features = self._aggregate_sub_model_scores_into_cood_features(
            ood_scores_all_sub_models, num_ood_images
        )

        # Combine features and labels
        all_features = np.concatenate((id_features, ood_features))
        # We take ood images as True or 1 and id images as False or 0
        all_labels = np.concatenate((np.zeros(num_id_images), np.ones(num_ood_images)))

        # Train the RandomForestClassifier
        self.ood_classifier = RandomForestClassifier()
        self.ood_classifier.fit(all_features, all_labels)

    def _infer_sub_models(self, distribution_data: List[DistributionDataItem]) -> dict:
        """
        Get OOD scores from all the sub-models for the given distribution data.
        :param distribution_data: List of DistributionDataItems for which ood scores are calculated
        :return: A dictionary containing the OOD scores from all the sub-models.
        """
        scores_all_sub_models = {}
        for sub_model in self.sub_models.values():
            scores_dict = sub_model(distribution_data)
            for score_type in scores_dict:
                scores_all_sub_models[score_type] = scores_dict[score_type]
        return scores_all_sub_models

    def _evaluate_model(
        self,
        test_id_data: List[DistributionDataItem],
        test_ood_data: List[DistributionDataItem],
    ) -> dict:
        """
        Evaluate the COOD model on the test data.
        :param test_id_data: List of DistributionDataItems for in-distribution test data
        :param test_ood_data: List of DistributionDataItems for out-of-distribution test data
        :return: A dictionary containing the evaluation metrics for the COOD model.
        """
        # Note that a cleaner way to evaluate the model is to call the self.__call__ method with the prediction of
        # each data item. However, this might be slower when the test set is large.

        logging.info("Evaluating COOD Model on Test Data")
        logging.info(f"Test data: ID - {len(test_id_data)}, OOD - {len(test_ood_data)}")

        # Get scores from sub-models
        id_scores_all_sub_models = self._infer_sub_models(test_id_data)
        ood_scores_all_sub_models = self._infer_sub_models(test_ood_data)

        num_id_images = len(test_id_data)
        num_ood_images = len(test_ood_data)

        # Arrange features
        id_features = self._aggregate_sub_model_scores_into_cood_features(
            id_scores_all_sub_models, num_id_images
        )
        ood_features = self._aggregate_sub_model_scores_into_cood_features(
            ood_scores_all_sub_models, num_ood_images
        )

        # Combine features and labels
        all_features = np.concatenate((id_features, ood_features))
        all_labels = np.concatenate((np.zeros(num_id_images), np.ones(num_ood_images)))

        # Evaluate the model
        mean_accuracy = self.ood_classifier.score(all_features, all_labels)
        return {"mean_accuracy": mean_accuracy}

    def __call__(self, prediction: Prediction) -> float:
        """
        Return the COOD Score based using feature vector and prediction probabilities in "prediction".
        """
        data_item = DistributionDataItem(
            media_name="sample",  # We do not need this data for inference
            media_path="sample",
            annotated_label="",
            raw_prediction=prediction,
        )
        scores_all_sub_models = self._infer_sub_models(distribution_data=[data_item])
        features_arranged = self._aggregate_sub_model_scores_into_cood_features(
            scores_all_sub_models=scores_all_sub_models, num_images=1
        )

        cood_score = self.ood_classifier.predict_proba(features_arranged)

        return cood_score[0][1]  # Return only the probability of being OOD

    def _split_data(
        self,
        data: List[DistributionDataItem],
        stratified: bool,
        split_ratio: float,
    ) -> None:
        """
        Split and assign the data into TRAIN or TEST purpose based on the split_ratio.
        Note that we do not need a validation split as no hyperparameters are tuned during COOD training.
        :param data: List of DistributionDataItems to be assigned with Train and Test purposes.
        :param stratified: If True, the split is stratified based on the annotated labels.
        :param split_ratio: The fraction of data to be used for training. The remaining data is used for testing.
        :return: None - The data is modified in-place.
        """
        if stratified:
            labels = [item.annotated_label for item in data]
            selected_indices = stratified_selection(
                x=data,
                y=labels,
                fraction=split_ratio,
                min_samples_per_class=1,
            )
        else:
            selected_indices = np.random.choice(
                len(data), int(split_ratio * len(data)), replace=False
            )

        self._assign_train_test_purpose_to_data(
            data=data, train_indices=selected_indices
        )

    @staticmethod
    def _assign_train_test_purpose_to_data(
        data: List[DistributionDataItem], train_indices: List[int]
    ) -> None:
        """
        Assign the TRAIN or TEST purpose to the data based on the train_indices.
        All data with index in train_indices is assigned the purpose TRAIN, and the rest is assigned the purpose TEST.
        If the data is already assigned a purpose, it is overwritten.
        :param data: List of DistributionDataItems to be assigned with Train and Test purposes.
        :param train_indices: List of indices to be assigned the TRAIN purpose.
        :return: None
        """
        for idx in train_indices:
            data[idx].purpose = DistributionDataItemPurpose.TRAIN
        for idx in range(len(data)):
            if idx not in train_indices:
                data[idx].purpose = DistributionDataItemPurpose.TEST

    def _infer_image_on_deployment(
        self, image_path: str, explain: bool = False
    ) -> Prediction:
        """
        Infer the image and get the prediction using the deployment
        """
        img = cv2.imread(image_path)
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        if explain:
            # Note that a check to see if xai model is present in the deployment is not done.
            # If the model is not present, then feature_vector will be None
            return self.deployment.explain(image=img_rgb)
        else:
            return self.deployment.infer(image=img_rgb)

    def _create_ood_images(self, reference_dataset: Dataset) -> str:
        """
        Create near-OOD images by applying strong corruptions to the images in the reference datasets.
        """
        # Options  : Applying corruptions, generating Perlin Noise Images, Background extraction (using saliency maps)
        ref_images_path = os.path.join(self.data_dir, reference_dataset.name, "images")
        corrupted_images_path = os.path.join(self.data_dir, "ood_images")
        if not os.path.exists(corrupted_images_path):
            os.makedirs(corrupted_images_path)

        for image_name in tqdm(
            os.listdir(ref_images_path), desc="Generating OOD images"
        ):
            image_path = os.path.join(ref_images_path, image_name)
            img = cv2.imread(image_path)
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            corrupted_img = self.corruption_transform(img)
            corrupted_image_path = os.path.join(corrupted_images_path, image_name)
            cv2.imwrite(corrupted_image_path, corrupted_img)

        return corrupted_images_path

    def _check_project_fit(self):
        """
        Check if the project is suited for the current OOD detection task.
        Currently, only a single task of type "classification" is supported.
        """
        tasks_in_project = self.project.get_trainable_tasks()
        if len(tasks_in_project) != 1:
            raise ValueError(
                "Out-of-distribution detection is only "
                "supported for projects with a single task for now."
            )

        # get the task type and check if it is classification
        task_type = tasks_in_project[0].task_type
        if task_type != TaskType.CLASSIFICATION:
            raise ValueError(
                "Out-of-distribution detection models are only "
                "supported for classification tasks for now."
            )

    def _prepare_distribution_data(
        self, source: Union[Dataset, str]
    ) -> List[DistributionDataItem]:
        """
        Prepare the distribution data from the source by inferencing the images and extracting the feature vectors.
        param source: Dataset or directory containing images. If a dataset is provided, the images and annotations are
        downloaded from the dataset. If a directory is provided, the images are read from the directory.
        """
        valid_extensions = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff")

        if isinstance(source, Dataset):

            media_list = self.image_client.get_all_images(dataset=source)
            self.image_client.download_all(
                path_to_folder=self.data_dir, append_image_uid=True, dataset=source
            )

            self.annotation_client.download_annotations_for_images(
                images=media_list,
                path_to_folder=self.data_dir,
                append_image_uid=True,
            )

            annotations_dir = os.path.join(self.data_dir, "annotations")
            image_dir = os.path.join(self.data_dir, "images", source.name)

            image_paths = [
                os.path.join(image_dir, f"{media.name}_{media.id}.jpg")
                for media in media_list
            ]
            annotation_files = [
                os.path.join(annotations_dir, f"{media.name}_{media.id}.json")
                for media in media_list
            ]
        else:
            # Find all the images in the directory
            image_paths = [
                os.path.join(source, image_name)
                for image_name in os.listdir(source)
                if image_name.lower().endswith(valid_extensions)
            ]

            annotation_files = [None] * len(image_paths)

        distribution_data_items = []
        for image_path, annotation_file in zip(image_paths, annotation_files):
            annotation_label = (
                self._load_annotations(annotation_file) if annotation_file else None
            )
            data_item = self._prepare_data_item(
                image_path=image_path, annotation_label=annotation_label
            )
            distribution_data_items.append(data_item)

        return distribution_data_items

    def _prepare_data_item(
        self, image_path: str, annotation_label: Union[str, None]
    ) -> DistributionDataItem:
        """
        Prepare the DistributionDataItem for the given image. Infers the image and extracts the feature vector.
        :param image_path: Path to the image
        :param annotation_label: Annotated label for the image (optional)
        return: DistributionDataItem for the image
        """
        prediction = self._infer_image_on_deployment(
            image_path=image_path, explain=True
        )
        return DistributionDataItem(
            media_name=os.path.splitext(os.path.basename(image_path))[0],
            media_path=image_path,
            annotated_label=annotation_label,
            raw_prediction=prediction,
        )

    @staticmethod
    def _load_annotations(annotation_file: str) -> Union[str, None]:
        """
        Read the annotations from the annotation file downloaded from Geti and returns the single label.
        Only to be used for classification tasks an image is annotated with a single label.
        :param annotation_file: Path to the annotation file
        :return: Annotated label for the image, if available, else None
        """
        if os.path.exists(annotation_file):
            with open(annotation_file, "r") as f:
                annotation = json.load(f)
                return annotation["annotations"][0]["labels"][0]["name"]
        return None

    @staticmethod
    def _aggregate_sub_model_scores_into_cood_features(
        scores_all_sub_models: dict, num_images: int
    ) -> np.ndarray:
        """
        Combine the OOD scores from all the sub-models into a single feature vector that can be passed to the
        COOD's random forest classifier
        :param scores_all_sub_models: A dictionary containing the OOD scores from all the sub-models
        :param num_images: Number of images for which the OOD scores are calculated
        :return: A feature vector containing the OOD scores from all the sub-models as a numpy array
        """
        features = np.zeros((num_images, len(scores_all_sub_models)))
        for score_idx, score_type in enumerate(scores_all_sub_models):
            features[:, score_idx] = scores_all_sub_models[score_type]
        return features

    def __repr__(self):
        """
        Return a string representation of the COODModel.
        """
        return (
            f"COODModel(project={self.project.name}, "
            f"Sub models: {list(self.sub_models.keys())}, "
            f"Data Items: {len(self.id_distribution_data)} ID, {len(self.ood_distribution_data)} OOD)"
        )


class OODSubModel(metaclass=ABCMeta):
    """
    Base class for OOD detection sub-models.
    """

    def __init__(self):
        self._is_trained = False

    @abstractmethod
    def train(self, distribution_data: List[DistributionDataItem]):
        """
        Train the OOD detection sub-model using a list in-distribution data items
        """
        raise NotImplementedError

    def __call__(self, data_items: List[DistributionDataItem]) -> dict:
        """
        Check if the model is trained and call the forward method.
        """
        if not self._is_trained:
            raise ValueError(
                "Model is not trained. Please train the model first before calling."
            )
        return self.forward(data_items)

    @abstractmethod
    def forward(self, data_items: List[DistributionDataItem]) -> dict:
        """
        Return the OOD score for the given data items.
        """
        raise NotImplementedError

    @property
    def is_trained(self) -> bool:
        """
        Return True if the model is trained.
        """
        return self._is_trained

    def __repr__(self):
        """
        Return a string representation of the OODSubModel.
        """
        return f"{self.__class__.__name__} (is_trained={self.is_trained})"


class KNNBasedOODModel(OODSubModel):
    """
    Model for OOD detection based on k-Nearest Neighbours (kNN) search in the feature space.
    The model calculates OOD scores based on distance to the nearest neighbours, entropy among the nearest neighbours,
    and EnWeDi (which combines distance and entropy).
    """

    # # TODO[OOD]: Add more features to the model
    # 1) distance to prototypical center
    # 2) ldof (to expensive ?)
    # 3) exact combination of entropy and distance from thesis

    def __init__(self, knn_k: int = 10):
        super().__init__()
        self.knn_k = knn_k
        self.knn_search_index = None
        self.train_set_labels = None

    def train(self, distribution_data: List[DistributionDataItem]):
        """
        Build the knn search index using faiss for the in-distribution data.
        :param distribution_data: List of DistributionDataItems for training the model. These are typically user
        annotated images from a Geti project from datasets that correspond to "in-distribution". Please note that the
        annotated labels are required if entropy based ood scores measures are calculated in the forward method.
        """
        id_data = distribution_data
        feature_vectors = np.array([data.feature_vector for data in id_data])
        labeled_set_labels = np.array([data.annotated_label for data in id_data])

        self.train_set_labels = labeled_set_labels
        self.knn_search_index = perform_knn_indexing(feature_vectors, use_gpu=False)
        self._is_trained = True

    def forward(self, data_items: List[DistributionDataItem]) -> dict:
        """
        Perform kNN search and calculates different types of OOD scores.
        :param data_items: List of DistributionDataItems for which OOD scores are calculated
        :return: A dictionary containing the OOD score names as keys and the OOD scores as values.
        """
        features = np.array([item.feature_vector for item in data_items])
        distances, nn_indices = perform_knn_search(
            knn_search_index=self.knn_search_index,
            feature_vectors=features,
            k=self.knn_k,
        )

        knn_distance = distances[:, -1]  # distance to the kth nearest neighbour
        nn_distance = distances[:, 1]  # distance to the nearest neighbour
        # TODO[OOD] : When doing kNN Search for ID, the 0th index is the same image. So, should we use k+1 ?
        average_nn_distance = np.mean(distances[:, 1:], axis=1)

        entropy_score = calculate_entropy_nearest_neighbours(
            train_labels=self.train_set_labels,
            nns_labels_for_test_fts=nn_indices,
            k=self.knn_k,
        )

        # Add one to the entropy scores
        # This is to offset the range to [1,2] instead of [0,1] and avoids division by zero
        # if used elsewhere
        entropy_score += 1

        enwedi_score = average_nn_distance * entropy_score
        enwedi_nn_score = nn_distance * entropy_score

        return {
            "knn_distance": knn_distance,
            "nn_distance": nn_distance,
            "average_nn_distance": average_nn_distance,
            "entropy_score": entropy_score,
            "enwedi_score": enwedi_score,
            "enwedi_nn_score": enwedi_nn_score,
        }


class GlobalFREBasedModel(OODSubModel):
    """
    Global Feature Reconstruction Error (FRE) Model. Builds a single PCA model for the whole in-distribution
    data provided thereby providing a "Global" subspace representation of the in-distribution features.
    See https://arxiv.org/abs/2012.04250 for details.
    """

    def __init__(self, n_components=0.995):
        super().__init__()
        self.n_components = n_components
        self.pca_model = None

    def train(self, distribution_data: List[DistributionDataItem]):
        """
        Fit a single (global) PCA model for the in-distribution data
        """
        feature_vectors = np.array([data.feature_vector for data in distribution_data])
        self.pca_model = fit_pca_model(
            feature_vectors=feature_vectors, n_components=self.n_components
        )
        self._is_trained = True

    def forward(self, data_items: List[DistributionDataItem]) -> dict:
        """
        Return the global fre score for the given data items.
        """
        if not self._is_trained:
            raise ValueError(
                "Model is not trained. Please train the model first before calling."
            )
        features = np.array([item.feature_vector for item in data_items])
        fre_scores = fre_score(feature_vectors=features, pca_model=self.pca_model)
        return {"global_fre_score": fre_scores}


class ClassFREBasedModel(OODSubModel):
    """
    Per-class Feature Reconstruction Error (FRE) Model. Each class present in the in-distribution data is represented
    by a subspace model.
    See https://arxiv.org/abs/2012.04250 for details
    """

    def __init__(self, n_components=0.995):
        super().__init__()
        self.n_components = n_components
        self.pca_models_per_class = {}

    def train(self, distribution_data: List[DistributionDataItem]):
        """
        Fit PCA Models on the in-distribution data for each class.
        """
        id_data = distribution_data
        feature_vectors = np.array([data.feature_vector for data in id_data])
        labels = np.array([data.annotated_label for data in id_data])

        # iterate through unique labels and fit pca model for each class
        pca_models = {}

        for label in np.unique(labels):
            # labels are list of class names and not indices
            class_indices = [i for i, j in enumerate(labels) if j == label]
            class_features = feature_vectors[class_indices]
            pca_models[label] = fit_pca_model(
                feature_vectors=class_features, n_components=self.n_components
            )

        self.pca_models_per_class = pca_models
        self._is_trained = True

    def forward(self, data_items: List[DistributionDataItem]) -> dict:
        """
        Return various fre-based ood scores
        """
        if not self._is_trained:
            raise ValueError(
                "Model is not trained. Please train the model first before calling."
            )

        num_data_items = len(data_items)
        features = np.array([item.feature_vector for item in data_items])
        fre_scores_per_class = {}
        # class_fre_models is a dict with label name and pca model.
        for label, pca_model in self.pca_models_per_class.items():
            fre_scores_per_class[label] = fre_score(
                feature_vectors=features, pca_model=pca_model
            )

        # FRE Score # 1 - FRE  w.r.t the class the sample is predicted to be
        predicted_labels = [item.predicted_label for item in data_items]
        fre_scores_for_predicted_class = np.array(
            [fre_scores_per_class[label][i] for i, label in enumerate(predicted_labels)]
        )

        # FRE Score # 2 - Calculating the minimum FRE score across all classes

        min_fre_scores = np.zeros(num_data_items)
        # For each data point, find the minimum FRE score across all classes (labels)
        for i in range(num_data_items):
            min_fre_scores[i] = np.min(
                [fre_scores_per_class[label][i] for label in fre_scores_per_class]
            )

        # Note - It is observed that the minimum FRE scores are almost always same as the FRE scores for the predicted
        # class i.e., the predicted class is the class with minimum fre score.
        # However, this is true largely for ID images (99.95% of example data points).
        # For OOD images, this applies, but less frequently (78.3% of example data points).
        # Therefore, the difference of the two scores can also be considered as a "feature"

        return {
            "min_class_fre_score": min_fre_scores,
            "predicted_class_fre_score": fre_scores_for_predicted_class,
            "diff_min_and_predicted_class_fre": 1e-8
            + (fre_scores_for_predicted_class - min_fre_scores),
        }


class ProbabilityBasedModel(OODSubModel):
    """
    Maximum Softmax Probability Model - A baseline OOD detection model.
    Uses the concept that a lower maximum softmax probability indicates that the image could be OOD.
    """

    def __init__(self):
        super().__init__()

    def train(self, distribution_data: List[DistributionDataItem]):
        """
        MSP model does not require training.
        """
        self._is_trained = True

    def forward(self, data_items: List[DistributionDataItem]) -> dict:
        """
        Return the maximum softmax probability for the given prediction.
        """
        msp_scores = np.ndarray(len(data_items))
        for i, data_item in enumerate(data_items):
            # deployment.infer gives a single highest probability- no need to find the max
            msp_scores[i] = data_item.max_prediction_probability[0]

        return {"max_softmax_probability": msp_scores}
