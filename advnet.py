import matplotlib.pyplot as plt
import tensorflow as tf
import tensorflow_io as tfio
gpu = tf.config.list_physical_devices('GPU')[0]
tf.config.experimental.set_memory_growth(gpu, True)
tf.config.set_logical_device_configuration(
    gpu,
    [tf.config.LogicalDeviceConfiguration(memory_limit=3800)])

import numpy as np
import os

import data
import nets
import differentiable_rendering as diff_rendering
import generator
import config

cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits


class AdvNet:

    def __init__(self, architecture, hyper_params=None):
        if hyper_params is None:
            hyper_params = config.hyper_params
        self._hyper_params = hyper_params

        self.enemy = tf.keras.applications.xception.Xception(
            include_top=True,
            weights='imagenet',
            classifier_activation=None
        )
        self.enemy.trainable = False

        images_size = [self._hyper_params['BatchSize']] + self._hyper_params['ImageShape'] + [3]
        self.adv_images = tf.zeros(shape=images_size, dtype=tf.float32, name="adversarial images")

        # input to generator must be textures with values normalised to -1 and 1
        self.generator = generator.create_generator(self._hyper_params['NumSubnets'])
        self.generator.summary()
        # define simulator
        self.simulator = nets.create_simulator(architecture)
        self.simulator.summary()

        sim_learning_rate_schedule = tf.keras.optimizers.schedules.ExponentialDecay(
            initial_learning_rate=self._hyper_params['SimulatorLearningRate'],
            decay_steps=self._hyper_params['DecayAfter'],
            decay_rate=self._hyper_params['DecayRate'])

        self.generator_optimiser = tf.keras.optimizers.Adam(self._hyper_params['GeneratorLearningRate'], epsilon=1e-8)
        self.simulator_optimiser = tf.keras.optimizers.Adam(sim_learning_rate_schedule, epsilon=1e-8)

        # lists to save training history to
        self.generator_loss_history = []
        self.generator_l2_loss_history = []
        self.generator_tfr_history = []

        self.simulator_loss_history = []
        self.simulator_accuracy_history = []

        self.test_loss_history = []
        self.test_tfr_history = []

    @staticmethod
    def inference(logits):
        """
        Computes hard label prediction (label is one categorical value, not a vector of probabilities for each class.)

        Parameters
        ----------
        logits : Tensor
            Tensor representing the output of the NN with one minibatch as input.

        Returns
        ----------
        predictions
            A tensor with the label prediction for every sample in the minibatch.
        """
        return tf.argmax(input=logits, axis=-1, name='inference')

    @staticmethod
    def accuracy(predictions, true_labels):
        """
        Computes classification accuracy of predictions.

        Parameters
        ----------
        predictions : Tensor
            1D tensor representing the hard-label predictions of the NN on some images.
        true_labels : Tensor
            1D tensor with the ground truth labels of the images.

        Returns
        ----------
        tensor
            A tensor with one element: the classification accuracy, as a float value from 0 to 1.
        """
        return tf.reduce_mean(input_tensor=tf.cast(tf.equal(predictions, true_labels), dtype=tf.float32))

    @staticmethod
    def get_ufr(predictions, true_labels):
        """
        Computes UFR of adversarial images based on predictions of the enemy model on those images.

        Parameters
        ----------
        predictions : Tensor
            1D tensor representing the hard-label predictions of the NN on the adversarial images.
        true_labels : Tensor
            1D tensor with the ground truth labels of the images.

        Returns
        ----------
        tensor
            A tensor with one element: the UFR, as a float value from 0 to 1.
        """
        are_predictions_correct = [data.is_prediction_true(ground_truth, prediction) for ground_truth, prediction in
                                   zip(true_labels, predictions)]
        # negate accuracy of each prediction, because we want to measure ufr, not accuracy
        ufr = np.mean([not correct for correct in are_predictions_correct])

        return ufr

    def train(self, data_generator, load_checkpoint=False):
        """
        Trains network according the hyper params of the Net subclass.

        Parameters
        ----------
        data_generator : BatchGenerator
            Generator which returns each step a tuple with two tensors: the first is the mini-batch of training images,
            and the second is a list of their coresponding hard labels.
        load_checkpoint : bool
            Whether to restore simulator and generator weights from checkpoints. False by default.
        """
        print("\n Begin Training: \n")

        if load_checkpoint:
            global_step = self.load_model()
        else:
            self.warm_up_simulator(data_generator)
            global_step = 1
        self.evaluate(data_generator)

        # main training loop
        while global_step <= self._hyper_params['TotalSteps']:
            # train simulator for a couple of steps
            for _ in range(self._hyper_params['SimulatorSteps']):
                # ground truth labels are not needed for training the simulator
                textures, uv_maps, _, target_labels = data_generator.get_next_batch()

                # perform one optimisation step to train simulator so it has the same predictions as the target
                # model does on normal images
                self.simulator_training_step(textures, uv_maps, global_step)

                # we want simulator and generator training history to have the same number of elements, so we only keep
                # the loss and accuracy when training the simulator on adversarial images
                self.simulator_loss_history.pop()
                self.simulator_accuracy_history.pop()

                # perform one optimisation step to train simulator so it has the same predictions as the target
                # model does on adversarial images.
                textures = self.generate_adversarial_texture(textures, target_labels, is_training=False)
                self.simulator_training_step(textures, uv_maps, global_step)

            # train generator for a couple of steps
            for _ in range(self._hyper_params['GeneratorSteps']):
                textures, uv_maps, true_labels, target_labels = data_generator.get_next_batch()
                generator_loss = self.generator_training_step(textures, uv_maps, target_labels)

                enemy_labels = AdvNet.inference(self.enemy(2 * self.adv_images - 1, training=False))
                tfr = np.mean(target_labels == enemy_labels.numpy())
                ufr = AdvNet.get_ufr(enemy_labels, true_labels)

                self.generator_tfr_history.append(tfr)
                print('\rGenerator => Step: {}; Loss: {}; TFR {}; UFR {}'.format(global_step, generator_loss, tfr,
                                                                                 ufr), end='')

            # evaluate every so often
            if global_step % self._hyper_params['ValidateAfter'] == 0:
                self.evaluate(data_generator)
                self.save(global_step)

            global_step += 1

    def warm_up_simulator(self, data_generator):
        """
        Warms up the simulator by training it on normal images.

        Parameters
        ----------
        data_generator : BatchGenerator
            Generator used to create new batches of data samples.
        """

        # warm up simulator to match predictions of target model on clean images
        print('Warming up. ')
        for i in range(self._hyper_params['WarmupSteps']):
            textures, uv_maps, _, _ = data_generator.get_next_batch()

            self.simulator_training_step(textures, uv_maps, i - self._hyper_params['WarmupSteps'])

        # evaluate warmed up simulator on test data
        warmup_accuracy = 0.0
        print("\nEvaluating warmed up simulator:")
        for i in range(self._hyper_params['WarmupEvaluationSteps']):
            textures, uv_maps, _, _ = data_generator.get_next_batch()
            warmup_accuracy += self.warm_up_evaluation(textures, uv_maps)

        warmup_accuracy = warmup_accuracy / self._hyper_params['WarmupEvaluationSteps']
        print('\nAverage Warmup Accuracy: ', warmup_accuracy)

        # we do not want to plot the training history of the simulator during warmup, so we discard what it recorded
        self.simulator_loss_history = []
        self.simulator_accuracy_history = []

    def warm_up_evaluation(self, textures, uv_maps):
        """
        Evaluates the warmed up simulator on normal images.

        Parameters
        ----------
        textures : Tensor
            4D tensor of shape [batch_size, 2048, 2048, 3] with the textures of the objects that will be rendered to
            creates images to evaluate the simulator on.
        uv_maps : Tensor
            4D of shape [batch_size, image_size, image_size, 3] tensor with the UV maps used to render images of the
            3D objects.

        Returns
        ----------
        numpy array
            Numpy array with one scalar value, the accuracy of the simulator predicting the same labels as the enemy
            model.
        """
        print_error_params = diff_rendering.get_print_error_args(self._hyper_params)
        photo_error_params = diff_rendering.get_photo_error_args(self._hyper_params)
        background_colours = diff_rendering.get_background_colours(self._hyper_params)

        images = diff_rendering.render(textures, uv_maps, print_error_params, photo_error_params,
                                       background_colours, self._hyper_params)
        # scale images as simulator and enemy model expect images with values between -1 and 1
        images = 2 * images - 1

        simulator_logits = self.simulator(images, training=False)
        enemy_model_labels = AdvNet.inference(self.enemy(images, training=False))

        accuracy = AdvNet.accuracy(AdvNet.inference(simulator_logits), enemy_model_labels)
        print("\rAccuracy: %.3f" % accuracy.numpy(), end='')
        return accuracy.numpy()

    def generate_adversarial_texture(self, std_textures, target_labels, is_training):
        """
        Generates a new adversarial texture.

        Parameters
        ----------
        std_textures : numpy array
            4D array of shape [batch_size, 2048, 2048, 3]. The normal textures for which adversarial noise is
            generated. Must have values between 0 and 1.
        target_labels : numpy array
            1D numpy array of ints of length batch_size. The target labels for which the adversarial noise is generated.
        is_training : bool
            Whether this method is called when training the generator, or not.

        Returns
        ----------
        tensor
            4D tensor of shape [batch_size, 2048, 2048, 3]. The adversarial textures, with values between 0 and 1.
        """
        # Textures must have values between -1 and 1 for the generator
        adversarial_noises = self.generator([2.0 * std_textures - 1.0, target_labels], training=is_training)

        adversarial_textures = adversarial_noises + std_textures
        adversarial_textures = tf.clip_by_value(adversarial_textures, 0, 1)

        return adversarial_textures

    def simulator_training_step(self, textures, uv_maps, step):
        """
        Performs one training step for the simulator.

        Parameters
        ----------
        textures : numpy array or tensor
            4D array or tensor of shape [batch_size, 2048, 2048, 3]. The textures used for the rendered objects on
            which the simulator will be trained. Must have values between 0 and 1.
        uv_maps : numpy array
            1D numpy array of ints of length batch_size. The target labels for which the adversarial noise is generated.
        step : int
            The current training step.
        """
        # create rendering params and then render image. We do not need to differentiate through the rendering
        # for the simulator, therefore this can be done outside of the gradient tape.
        print_error_params = diff_rendering.get_print_error_args(self._hyper_params)
        photo_error_params = diff_rendering.get_photo_error_args(self._hyper_params)
        background_colours = diff_rendering.get_background_colours(self._hyper_params)

        images = diff_rendering.render(textures, uv_maps, print_error_params, photo_error_params,
                                       background_colours, self._hyper_params)
        images = 2 * images - 1

        with tf.GradientTape() as simulator_tape:
            sim_loss = self.simulator_loss(images, step)

        simulator_gradients = simulator_tape.gradient(sim_loss, self.simulator.trainable_variables)
        self.simulator_optimiser.apply_gradients(zip(simulator_gradients, self.simulator.trainable_variables))

    def generator_training_step(self, std_textures, uv_maps, target_labels):
        """
        Performs one training step for the generator.

        Parameters
        ----------
        std_textures : numpy array
            4D array of shape [batch_size, 2048, 2048, 3]. The normal textures for which adversarial noise is
            generated. Must have values between 0 and 1.
        uv_maps : numpy array
            4D array of shape [batch_sisze, image_size, image_size, 2]. The UV maps used to create rendered images with
            the adversarial texture, which are then meant to fool the simulator.
        target_labels : numpy array
            1D numpy array of ints of length batch_size. The target labels for which the adversarial noise is generated.

        Returns
        ----------
        numpy array
            Numpy array with scalar values. Is the mean loss of the generator.
        """
        with tf.GradientTape() as generator_tape:
            gen_loss = self.generator_loss(std_textures, uv_maps, target_labels)

        generator_gradients = generator_tape.gradient(gen_loss, self.generator.trainable_variables)
        # clip generator gradients
        generator_gradients = [tf.clip_by_value(grad, -1.0, 1.0) for grad in generator_gradients]

        self.generator_optimiser.apply_gradients(zip(generator_gradients, self.generator.trainable_variables))
        return gen_loss.numpy()

    def generator_loss(self, textures, uv_maps, target_labels):
        """
        Calculates the generator loss.

        Parameters
        ----------
        textures : numpy array or tensor
            4D array/tensor of shape [batch_size, 2048, 2048, 3]. The textures for which the generator needs to create
            adversarial noise. Must have values between 0 and 1.
        uv_maps : numpy array
            4D array of shape [batch_size, image_size, image_size, 2]. The UV maps used to create rendered images with
            the adversarial texture.
        target_labels : numpy array
            1D numpy array of ints of length batch_size. The target labels for which the adversarial noise is generated.

        Returns
        ----------
        tensor
            1D tensor with the mean value of the loss across the batch.
        """
        adv_textures = self.generate_adversarial_texture(textures, target_labels, is_training=True)

        # generate rendering params common to both the standard and adversarial images
        print_error_params = diff_rendering.get_print_error_args(self._hyper_params)
        photo_error_params = diff_rendering.get_photo_error_args(self._hyper_params)
        background_colour = diff_rendering.get_background_colours(self._hyper_params)

        # render standard and adversarial images. They will have the same pose and params, just the texture will be
        # different
        std_images = diff_rendering.render(textures, uv_maps, print_error_params, photo_error_params,
                                           background_colour, self._hyper_params)
        self.adv_images = diff_rendering.render(adv_textures, uv_maps, print_error_params, photo_error_params,
                                                background_colour, self._hyper_params)

        # calculate main term of loss, to see if generator fools the simulator
        simulator_logits = self.simulator(2 * self.adv_images - 1, training=False)
        main_loss = cross_entropy(logits=simulator_logits, labels=target_labels)
        main_loss = tf.reduce_mean(main_loss)

        # convert images to lab space, to be used in the penalty loss term
        # std_images = AdvNet.get_normalised_lab_image(std_images)
        # self.adv_images = AdvNet.get_normalised_lab_image(self.adv_images)

        # calculate l2 norm of difference between LAB standard and adversarial images
        l2_penalty = tf.sqrt(tf.reduce_sum(tf.square(tf.subtract(std_images, self.adv_images)), axis=[1, 2, 3]))
        l2_penalty = self._hyper_params['PenaltyWeight'] * tf.reduce_mean(l2_penalty)

        self.generator_loss_history.append(main_loss.numpy())
        self.generator_l2_loss_history.append(l2_penalty.numpy())

        loss = main_loss + l2_penalty
        loss += tf.add_n(self.generator.losses)

        return loss

    # images must have pixel values between -1 and 1
    def simulator_loss(self, images, step):
        """
        Calculates the simulator loss.

        Parameters
        ----------
        images : tensor
            4D array/tensor of shape [batch_size, 2048, 2048, 3]. The images that the simulator is trained on. Must
            have values between 0 and 1.
        step : int
            The current training step. Used for printing information to the command line.

        Returns
        ----------
        tensor
            1D tensor with the mean value of the loss across the batch.
        """
        simulator_logits = self.simulator(images, training=True)
        enemy_model_labels = AdvNet.inference(self.enemy(images, training=False))

        loss = cross_entropy(logits=simulator_logits, labels=enemy_model_labels)
        loss = tf.reduce_mean(loss)
        loss += tf.add_n(self.simulator.losses)

        self.simulator_loss_history.append(loss.numpy())

        accuracy = AdvNet.accuracy(AdvNet.inference(simulator_logits), enemy_model_labels)
        self.simulator_accuracy_history.append(accuracy.numpy())
        print('\rSimulator => Step: {}; Loss: {}; Accuracy: {}'.format(step, loss.numpy(), accuracy.numpy()), end='')
        return loss

    # useful for testing if model.losses actually has all the correct losses
    @staticmethod
    def add_model_regularizer_loss(model):
        loss = 0
        for l in model.layers:
            if hasattr(l, 'kernel_regularizer') and l.kernel_regularizer and hasattr(l, 'kernel'):
                loss += l.kernel_regularizer(l.kernel)
            if hasattr(l, 'depthwise_regularizer') and l.depthwise_regularizer and hasattr(l, 'depthwise_kernel'):
                loss += l.depthwise_regularizer(l.depthwise_kernel)
            if hasattr(l, 'pointwise_regularizer') and l.pointwise_regularizer and hasattr(l, 'pointwise_kernel'):
                loss += l.pointwise_regularizer(l.pointwise_kernel)
        return loss

    @staticmethod
    def get_normalised_lab_image(rgb_images):
        """
        Turn a tensor representing a batch of normalised RGB images into equivalent normalised images in the LAB
        colour space.

        Parameters
        ----------
        rgb_images : numpy array
            The image which we want to convert to LAB space. Each value in it must be between 0 and 1. Is a 4D numpy
            array of size batch_size x 299 x 299 x 3
        Returns
        -------
        tensor
            A 4-D numpy array with shape [batch_size, 299, 299, 3] and with values between 0 and 1.
        """
        assert rgb_images.shape[1] == 299
        assert rgb_images.shape[2] == 299
        assert rgb_images.shape[3] == 3

        lab_images = tfio.experimental.color.rgb_to_lab(rgb_images)
        # separate the three colour channels
        lab_images = tf.unstack(lab_images, axis=-1)

        # normalise the lightness channel, which has values between 0 and 100
        lab_images[0] = lab_images[0] / 100.0
        # normalise the greeness-redness and blueness-yellowness channels, which normally are between -128 and 127
        lab_images[1] = (lab_images[1] + 128) / 255.0
        lab_images[2] = (lab_images[2] + 128) / 255.0

        lab_images = tf.stack(lab_images, axis=-1)
        return lab_images

    def evaluate(self, test_data_generator):
        """
        Evaluates trained (or in training) model across several minibatches of never before seen data. The number of
        batches is a hyper param of the Net subclass.

        Parameters
        ----------
        test_data_generator : BatchGenerator
            Generator which returns each step a tuple with four values: the textures of different 3D objects, UV maps
            for transforming those textures into rendered images of those objects, the correct labels for each sampled
            object, and a randomly chosen target label for each sample.
        """
        total_loss = 0.0
        total_tfr = 0.0
        total_ufr = 0.0

        for _ in range(self._hyper_params['TestSteps']):
            textures, uv_maps, true_labels, target_labels = test_data_generator.get_next_batch()
            # create adv image by adding the generated adversarial noise
            textures = self.generate_adversarial_texture(textures, target_labels, is_training=False)

            # use adversarial textures to render adversarial images
            print_error_params = diff_rendering.get_print_error_args(self._hyper_params)
            photo_error_params = diff_rendering.get_photo_error_args(self._hyper_params)
            background_colours = diff_rendering.get_background_colours(self._hyper_params)
            images = diff_rendering.render(textures, uv_maps, print_error_params, photo_error_params,
                                           background_colours, self._hyper_params)
            # scale images to -1 to 1, as the victim model expects
            images = 2 * images - 1

            # evaluate adversarial images on target model
            enemy_model_logits = self.enemy(images, training=False)
            enemy_labels = AdvNet.inference(enemy_model_logits)

            main_loss = cross_entropy(logits=enemy_model_logits, labels=target_labels)
            main_loss = tf.reduce_mean(main_loss)

            tfr = np.mean(target_labels == enemy_labels.numpy())
            ufr = AdvNet.get_ufr(enemy_labels, true_labels)

            total_loss += main_loss.numpy()
            total_tfr += tfr
            total_ufr += ufr

        total_loss /= self._hyper_params['TestSteps']
        total_tfr /= self._hyper_params['TestSteps']
        total_ufr /= self._hyper_params['TestSteps']

        self.test_loss_history.append(total_loss)
        self.test_tfr_history.append(total_tfr)
        print('\nTest: Loss: ', total_loss, '; TFR: ', total_tfr, '; UFR: ', total_ufr)

    def save(self, step):
        """
        Save model parameters and the training history.

        Parameters
        ----------
        step : int
            The current training step.
        """
        self.simulator.save_weights('./simulator/simulator_checkpoint')
        self.generator.save_weights('./generator/generator_checkpoint')
        np.savez('training_history', self.simulator_loss_history, self.simulator_accuracy_history,
                 self.generator_loss_history, self.generator_tfr_history, self.generator_l2_loss_history,
                 self.test_loss_history, self.test_tfr_history, [step])

    def load_model(self):
        """
        Restores model parameters and the training history.

        Returns
        -------
        int
            The number of steps saved in the files, when training was previously stopped. This should be used to resume
            training.
        """
        self.simulator.load_weights('./simulator/simulator_checkpoint')
        self.generator.load_weights('./generator/generator_checkpoint')
        old_global_step = self.load_training_history("./training_history.npz")

        return old_global_step

    def load_training_history(self, path):
        """
        Load training history from .npz file.

        Parameters
        ----------
        path : str
            Absolute path to th .npz file.
        Returns
        -------
        int
            The number of training steps when the training history was saved.
        """
        assert type(path) is str

        if os.path.exists(path):
            array_dict = np.load(path)

            self.simulator_loss_history = array_dict['arr_0'].tolist()
            self.simulator_accuracy_history = array_dict['arr_1'].tolist()
            self.generator_loss_history = array_dict['arr_2'].tolist()
            self.generator_l2_loss_history = array_dict['arr_3'].tolist()
            self.generator_tfr_history = array_dict['arr_4'].tolist()
            self.test_loss_history = array_dict['arr_5'].tolist()
            self.test_tfr_history = array_dict['arr_6'].tolist()
            step = array_dict['arr_7'][0]
            print("Training history restored.")

            return step

    def plot_training_history(self):
        """
        Plot training history.
        """
        plt.plot(self.simulator_loss_history, label="Simulator")
        plt.plot(self.generator_loss_history, label="Generator Main loss")
        plt.plot(self.generator_l2_loss_history, label='Generator L2 loss')
        test_steps = list(range(0, len(self.simulator_loss_history) + 1, self._hyper_params['ValidateAfter']))
        plt.plot(test_steps, self.test_loss_history, label="Generator Test")
        plt.xlabel("Steps")
        plt.ylabel("Loss")
        plt.title("G-EOT loss history")
        plt.legend()
        plt.show()

        plt.plot(self.simulator_accuracy_history, label="Simulator")
        plt.plot(self.generator_tfr_history, label="Generator")
        test_steps = list(range(0, len(self.simulator_accuracy_history) + 1, self._hyper_params['ValidateAfter']))
        plt.plot(test_steps, self.test_tfr_history, label="Generator Test")
        plt.xlabel("Steps")
        plt.ylabel("TFR")
        plt.title("G-EOT TFR/Accuracy history")
        plt.legend()
        plt.show()


if __name__ == '__main__':
    with tf.device("/GPU:0"):
        net = AdvNet("SimpleNet")
        model_3d_data_generator = data.BatchGenerator()

        net.train(model_3d_data_generator)
        net.plot_training_history()
