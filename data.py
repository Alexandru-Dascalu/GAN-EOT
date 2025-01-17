import os
import csv
import random

import numpy as np
from PIL import Image

import preproc
import uv_renderer
from config import hyper_params

DATA_DIR = "./dataset"


class Model3D:
    """
    A class that holds information related to a 3D model.

    Attributes
    ----------
    index : str
        The index of the folder of the 3D model. It is based on ascending alphabetical order of the model folders
        within the parent folder.
    name : str
        The name of the object, the same name as the folder where the model's files are.
    raw_texture : numpy array
        width x height x 3 numpy array representing the raw texture of the model.
    obj_path : str
        Absolute path to .obj file of the model.
    labels : list
        List of correct labels, represented, as integers from 0 to 1, for this object. For the dog model, this attribute
        will be the string "dog" instead, as that model has 120+ correct labels.
    """
    def __init__(self, folder, data_dir, index):
        self.name = folder
        self.index = index
        absolute_model_path = os.path.join(data_dir, self.name)

        self.raw_texture = Model3D._get_texture(absolute_model_path)
        self.obj_path = os.path.join(absolute_model_path, "{}.obj".format(self.name))
        self.labels = Model3D._load_labels(absolute_model_path)

    def __str__(self):
        return "{}: labels {}".format(self.name, self.labels)

    @staticmethod
    def _get_texture(path):
        """
        Read texture from file and return it in the appropriate format.

        Parameters
        ----------
        image_path : String
            Absolute path to texture file.

        Returns
        -------
        Numpy array
            Numpy array representing the raw texture. Has shape width x height x 3.
        """
        image_path = Model3D._get_texture_path(path)
        texture_image = Image.open(image_path)

        # convert image to a numpy array with float values
        raw_texture = np.array(texture_image).astype(np.float32)
        texture_image.close()
        # some raw textures have an alfa channel too, we only want three colour channels
        raw_texture = raw_texture[:, :, :3]
        # normalise pixel vaues to between 0 and 1
        raw_texture = raw_texture / 255.0

        return raw_texture

    @staticmethod
    def _get_texture_path(path):
        """
        Determines if texture is a jpg or png file, and returns absolute path to texture file.

        Parameters
        ----------
        path : String
            Absolute path to dataset sample folder.

        Returns
        -------
        String
            Absolute path to texture file.
        """
        if not os.path.isdir(path):
            raise ValueError("The given absolute path is not a directory!")

        for file in os.listdir(path):
            if file.endswith(".jpg"):
                return os.path.join(path, file)
            elif file.endswith(".png"):
                return os.path.join(path, file)

        raise ValueError("No jpg or png files found in the given directory!")

    @staticmethod
    def _load_labels(path):
        """
        Reads labels of a certain model from the dataset and returns them.

        Parameters
        ----------
        path : String
            Absolute path to dataset sample folder.

        Returns
        -------
        List
            Returns a list of integers, or if this is the dog model, just returns "dog" as a label.
        """
        if not os.path.isdir(path):
            raise ValueError("The given absolute path is not a directory!")

        labels_file_path = os.path.join(path, "labels.txt")
        try:
            labels_file = open(labels_file_path)
        except FileNotFoundError:
            raise FileNotFoundError("No txt files found in the given path! Can not find labels!")

        # labels are written only on the first line of the file, we only read the first line
        labels = next(csv.reader(labels_file, delimiter=','))
        # German shepherd model has all 120+ dog labels as true labels, that is encoded only as "dog" to save
        # make things easier
        if labels[0] == 'dog':
            return labels[0]
        else:
            try:
                int_labels = [int(label) for label in labels]
                return int_labels
            except ValueError as e:
                print("Original exception message: {}".format(str(e)))
                raise ValueError("A label of {} does not represent an int!".format(path))
            finally:
                labels_file.close()


def get_object_folders(data_dir):
    """
    Returns a list of all folders in the given folder.

    Parameters
    ----------
    data_dir : str
        Absolute path to dataset sample folder.

    Returns
    -------
    List
        Returns a list with the name of each sub folder in the given folder.
    """
    if not os.path.isdir(data_dir):
        raise ValueError("The given data path is not a directory!")

    return [folder for folder in os.listdir(data_dir) if os.path.isdir(os.path.join(data_dir, folder))]


def load_dataset(data_dir):
    """
    Reads models from the dataset files, creates Model3D objects and returns them.

    Parameters
    ----------
    data_dir : str
        Absolute path to dataset sample folder.

    Returns
    -------
    List
        Returns a list of all 3D models.
    """
    object_folders = get_object_folders(data_dir)
    models = [Model3D(folder, data_dir, i) for i, folder in enumerate(object_folders)]
    for model in models:
        print(str(model))

    return models


class BatchGenerator():
    def __init__(self, batch_size=hyper_params['BatchSize']):
        """
        Creates a generator that generate batches of data samples from the 3D model dataset, with a random adversarial
        target label and UV map based on random parameters.

        Parameters
        ----------
        batch_size : int
            Size of the batches generated by this object. Defaults to the batch size set in the hyper param dictionary
            in config.
        """
        # the dataset of 3D models
        self.models = load_dataset(DATA_DIR)

        # the renderer used to create UV maps
        self.renderer = uv_renderer.UVRenderer(self.models)
        self.renderer.set_parameters(
            camera_distance=(hyper_params['MinCameraDistance'], hyper_params['MaxCameraDistance']),
            x_translation=(hyper_params['MinTranslationX'], hyper_params['MaxTranslationX']),
            y_translation=(hyper_params['MinTranslationY'], hyper_params['MaxTranslationY'])
        )

        self.batch_size = batch_size
        # variables for the batches of texture, UV maps and labels that will generated
        self.batch_textures = np.zeros(shape=(self.batch_size, 2048, 2048, 3), dtype=np.float32)
        self.batch_uv_maps = np.zeros(shape=(self.batch_size, 299, 299, 2), dtype=np.float32)
        self.batch_labels = []
        self.batch_target_labels = np.zeros(shape=(batch_size,), dtype=np.int64)

        # used for generating lists of indexes of the 3D objects in random order, used for randomly picking 3D models
        self.index_generator = preproc.get_index_generator(len(self.models))

    def get_next_batch(self):
        """
        Generates new batch.

        Returns
        -------
        Tuple
            A tuple with four values. The first is a numpy array of size [batch_size, 2048, 2048, 3], holding textures
            of 3D models from the dataset. The second is a numpy array of size [batch_size, image_size, image_size, 2],
            with the UV maps for creating rendered images of those objects. The UV maps are created based on random
            parameters for the rotation, translation, and camera distance. The third is a numpy array with shape
            [batch_size], containing random target labels, one for each sample. The fourth is a list with the correct
            labels of each sample.
        """
        # discard ground truth labels from the previous batch
        self.batch_labels = []
        for i in range(self.batch_size):
            next_sample = self.get_next_sample()

            self.batch_textures[i] = next_sample.raw_texture
            self.batch_labels.append(next_sample.labels)
            self.batch_uv_maps[i] = self.renderer.render(next_sample.index, i)
            self.batch_target_labels[i] = BatchGenerator.get_random_target_label(next_sample.labels)

        return self.batch_textures, self.batch_uv_maps, self.batch_labels, self.batch_target_labels

    def get_next_sample(self):
        """
        Picks the next 3D model to be included in the batch. Repeated calls of this method pick 3D models in a random
        order.

        Returns
        -------
        Model3D
            The next 3D model to be included.
        """
        index = next(self.index_generator)
        return self.models[index]

    @staticmethod
    def get_random_target_label(ground_truth_labels):
        """
        Generates new random target label.

        Parameters
        ----------
        ground_truth_labels : list
            List of correct labels for a certain 3D model. For the dog model, this argument should be a string with the
            value "dog" instead. "dog" signifies Imagenet labels 151 to 275, inclusive.

        Returns
        -------
        int
            New target label.
        """
        label_set = set(ground_truth_labels)

        # loop until we have a random target label distinct from the true labels
        while True:
            target_label = random.randint(0, 999)

            # dog model has al 120+ dog breeds as true labels, so we need to check if the label is outside that range
            if ground_truth_labels == "dog":
                if target_label < 151 or target_label > 275:
                    return target_label
            # just check that the chosen target is not in the set of true labels
            elif target_label not in label_set:
                return target_label


def is_prediction_true(true_labels, predicted_label):
    """
    Check if predicted label is a ground truth label.

    Parameters
    ----------
    true_labels : list
        The list of ground truth labels for a 3D model.
    predicted_label : int
        The predicted label.

    Returns
    -------
    bool
        True if the prediction is correct, false if not.
    """
    if true_labels == "dog":
        # dog model has all 120 dog breed and dog-like animals as true labels
        if 150 < predicted_label < 276:
            return True
    # even if object only has one true label, it is still represented as a list with just one element
    elif type(true_labels) == list:
        if predicted_label in true_labels:
            return True
    else:
        raise ValueError("true labels list for a sample should be either \"dog\" or a list of ints.")

    # if it has not returned so far, then the prediction is incorrect
    return False


if __name__ == '__main__':
    load_dataset(DATA_DIR)
