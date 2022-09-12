import matplotlib.pyplot as plt
import tensorflow as tf
import tensorflow_io as tfio
# gpu = tf.config.list_physical_devices('GPU')[0]
# tf.config.experimental.set_memory_growth(gpu, True)
# tf.config.set_logical_device_configuration(
#     gpu,
#     [tf.config.LogicalDeviceConfiguration(memory_limit=3800)])

import numpy as np

import data
import nets
import differentiable_rendering as diff_rendering
from generator import create_generator
import config

NoiseRange = 10.0
cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits
relu = tf.keras.activations.relu


class AdvNet(nets.Net):

    def __init__(self, architecture, hyper_params=None):
        nets.Net.__init__(self)

        if hyper_params is None:
            hyper_params = config.hyper_params
        self._hyper_params = hyper_params

        self.enemy = tf.keras.applications.xception.Xception(
            include_top=True,
            weights='imagenet',
            classifier_activation=None
        )
        self.enemy.trainable = False

        # Inputs
        self.adv_images = tf.zeros(shape=[self._hyper_params['BatchSize']] + self._hyper_params['ImageShape'] + [3],
                                   dtype=tf.float32, name="adversarial images")

        # input to generator must be textures with values normalised to -1 and 1
        self.generator = create_generator(self._hyper_params['NumSubnets'])
        self.generator.summary()
        # define simulator
        self.simulator = self.create_simulator(architecture)
        self.simulator.summary()

        learning_rate_schedule = tf.keras.optimizers.schedules.ExponentialDecay(
            initial_learning_rate=self._hyper_params['LearningRate'],
            decay_steps=self._hyper_params['DecayAfter'],
            decay_rate=self._hyper_params['DecayRate'])

        self.generator_optimiser = tf.keras.optimizers.Adam(learning_rate_schedule, epsilon=1e-8)
        self.simulator_optimiser = tf.keras.optimizers.Adam(learning_rate_schedule, epsilon=1e-8)

    # define inference as hard label prediction of simulator on natural images
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
    def accuracy(predictions, labels):
        return tf.reduce_mean(input_tensor=tf.cast(tf.equal(predictions, labels), dtype=tf.float32))

    @staticmethod
    def get_ufr(true_labels, predictions):
        are_predictions_correct = [data.is_prediction_true(ground_truth, prediction) for ground_truth, prediction in
                                   zip(true_labels, predictions)]
        # negate accuracy of each prediction, because we want to measure ufr, not accuracy
        ufr = np.mean([not correct for correct in are_predictions_correct])

        return ufr

    def train(self, data_generator):
        print("\n Begin Training: \n")

        self.warm_up_simulator()
        self.evaluate(data_generator)

        globalStep = 1
        # main training loop
        while globalStep <= self._hyper_params['TotalSteps']:
            # train simulator for a couple of steps
            for _ in range(self._hyper_params['SimulatorSteps']):
                # ground truth labels are not needed for training the simulator
                textures, uv_maps, _, target_labels = next(data_generator)

                # perform one optimisation step to train simulator so it has the same predictions as the target
                # model does on normal images
                print('\rSimulator => Step: {}'.format(globalStep), end='')
                self.simulator_training_step(textures, uv_maps)

                # perform one optimisation step to train simulator so it has the same predictions as the target
                # model does on adversarial images.
                textures = self.generate_adversarial_texture(textures, target_labels, is_training=False)
                print('\rSimulator => Step: {}'.format(globalStep), end='')
                self.simulator_training_step(textures, uv_maps)

            # train generator for a couple of steps
            for _ in range(self._hyper_params['GeneratorSteps']):
                textures, uv_maps, true_labels, target_labels = next(data_generator)
                print('\rGenerator => Step: {}'.format(globalStep), end='')
                self.generator_training_step(textures, uv_maps, target_labels)

                enemy_labels = AdvNet.inference(self.enemy(2 * self.adv_images - 1, training=False))
                tfr = np.mean(target_labels == enemy_labels.numpy())
                ufr = AdvNet.get_ufr(true_labels, enemy_labels)

                self.generator_tfr_history.append(tfr)
                print('; TFR: %.3f' % tfr, '; UFR: %.3f' % ufr, end='')

            # evaluate on test every so often
            if globalStep % self._hyper_params['ValidateAfter'] == 0:
                self.evaluate(data_generator)

            globalStep += 1

    def warm_up_simulator(self):
        # warm up simulator to match predictions of target model on clean images
        print('Warming up. ')
        for i in range(self._hyper_params['WarmupSteps']):
            textures, uv_maps, _, _ = next(data_generator)
            print("Generated batch")

            print('Simulator => Step: ', i - self._hyper_params['WarmupSteps'])
            self.simulator_training_step(textures, uv_maps)

        # evaluate warmed up simulator on test data
        warmup_accuracy = 0.0
        print("\nEvaluating warmed up simulator:")
        for i in range(50):
            textures, uv_maps, _, _ = next(data_generator)
            warmup_accuracy += self.warm_up_evaluation(textures, uv_maps)

        warmup_accuracy = warmup_accuracy / 50
        print('\nAverage Warmup Accuracy: ', warmup_accuracy)

    def warm_up_evaluation(self, textures, uv_maps):
        print_error_params = diff_rendering.get_print_error_args(self._hyper_params)
        photo_error_params = diff_rendering.get_photo_error_args(self._hyper_params)
        background_colours = diff_rendering.get_background_colours(self._hyper_params)

        images = diff_rendering.render(textures, uv_maps, print_error_params, photo_error_params,
                                       background_colours, self._hyper_params)

        simulator_logits = self.simulator(images, training=False)
        enemy_model_labels = AdvNet.inference(self.enemy(images, training=False))

        accuracy = AdvNet.accuracy(AdvNet.inference(simulator_logits), enemy_model_labels)
        print("\rAccuracy: %.3f" % accuracy.numpy(), end='')
        return accuracy

    def get_uniform_image_noise(self):
        uniform_noise = np.random.rand(self._hyper_params['BatchSize'], 299, 299, 3)
        # scale noise from 0 to 1 to between -1 and 1
        uniform_noise = (uniform_noise - 0.5) * 2.0
        # scale noise up based on hyper param, then divide it so it can be added to an image with pixel values
        # between 0 and 1
        uniform_noise = (uniform_noise * NoiseRange) / 255.0

        return uniform_noise

    def generate_adversarial_texture(self, std_textures, target_labels, is_training):
        # Textures must have values between -1 and 1 for the generator
        adversarial_noises = self.generator([2.0 * std_textures - 1.0, target_labels], training=is_training)
        # noise is currently between -NoiseRange and Noise Rage, we scale it down so it can be directly applied
        # to textures with pixel values between 0 and 1
        adversarial_noises = tf.divide(adversarial_noises, 255.0)

        adversarial_textures = adversarial_noises + std_textures
        # normalise adversarial texture to values between 0 and 1
        adversarial_textures = diff_rendering.general_normalisation(adversarial_textures)

        return adversarial_textures

    def simulator_training_step(self, textures, uv_maps):
        print("Start training step")
        # create rendering params and then render image. We do not need to differentiate through the rendering
        # for the simulator, therefore this can be done outside of the gradient tape.
        print_error_params = diff_rendering.get_print_error_args(self._hyper_params)
        photo_error_params = diff_rendering.get_photo_error_args(self._hyper_params)
        background_colours = diff_rendering.get_background_colours(self._hyper_params)

        images = diff_rendering.render(textures, uv_maps, print_error_params, photo_error_params,
                                       background_colours, self._hyper_params)
        print("Rendered images")

        with tf.GradientTape() as simulator_tape:
            sim_loss = self.simulator_loss(images)

        print("Caclulating gradients")
        simulator_gradients = simulator_tape.gradient(sim_loss, self.simulator.trainable_variables)
        print("Apply gradients")
        self.simulator_optimiser.apply_gradients(zip(simulator_gradients, self.simulator.trainable_variables))
        print("Finished training step")

    def generator_training_step(self, std_textures, uv_maps, target_labels):
        with tf.GradientTape() as generator_tape:
            gen_loss = self.generator_loss(std_textures, uv_maps, target_labels)

        generator_gradients = generator_tape.gradient(gen_loss, self.generator.trainable_variables)
        # clip generator gradients
        generator_gradients = [tf.clip_by_value(grad, -1.0, 1.0) for grad in generator_gradients]

        self.generator_optimiser.apply_gradients(zip(generator_gradients, self.generator.trainable_variables))

    # generator trains to produce perturbations that make the simulator produce the desired target label
    def generator_loss(self, textures, uv_maps, target_labels):
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
        simulator_logits = self.simulator(self.adv_images, training=False)
        main_loss = cross_entropy(logits=simulator_logits, labels=target_labels)
        main_loss = tf.reduce_mean(main_loss)

        # convert images to lab space, to be used in the penalty loss term
        std_images = AdvNet.get_normalised_lab_image(std_images)
        self.adv_images = AdvNet.get_normalised_lab_image(self.adv_images)

        # calculate l2 norm of difference between LAB standard and adversarial images
        l2_penalty = tf.sqrt(tf.reduce_sum(tf.square(tf.subtract(std_images, self.adv_images)), axis=[1, 2, 3]))
        l2_penalty = self._hyper_params['PenaltyWeight'] * tf.reduce_mean(l2_penalty)

        self.generator_loss_history.append(main_loss.numpy())
        self.generator_l2_loss_history.append(l2_penalty.numpy())

        loss = main_loss + l2_penalty
        loss += tf.add_n(self.generator.losses)
        print("; Loss: %.3f" % loss.numpy(), end='')

        return loss

    # images must have pixel values between 0 and 1
    def simulator_loss(self, images):
        print("Start simulator loss")
        simulator_logits = self.simulator(images, training=True)
        print("Start enemy inference")
        enemy_model_labels = AdvNet.inference(self.enemy(images, training=False))

        "Start calculate loss"
        loss = cross_entropy(logits=simulator_logits, labels=enemy_model_labels)
        loss = tf.reduce_mean(loss)
        loss += tf.add_n(self.simulator.losses)

        self.simulator_loss_history.append(loss.numpy())
        print("; Loss: %.3f" % loss.numpy())

        accuracy = AdvNet.accuracy(AdvNet.inference(simulator_logits), enemy_model_labels)
        self.simulator_accuracy_history.append(accuracy.numpy())
        print("; Accuracy: %.3f" % accuracy.numpy())
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
        """Turn a tensor representing a batch of normalised RGB images into equivalent normalised images in the LAB
        colour space.

        Parameters
        ----------
        rgb_images : 4D numpy array of size batch_size x 299 x 299 x 3
            The image which we want to convert to LAB space. Each value in it must be between 0 and 1.
        Returns
        -------
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
        total_loss = 0.0
        total_tfr = 0.0
        total_ufr = 0.0

        for _ in range(self._hyper_params['TestSteps']):
            textures, uv_maps, true_labels, target_labels = next(test_data_generator)
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
            ufr = AdvNet.get_ufr(true_labels, enemy_labels)

            total_loss += main_loss
            total_tfr += tfr
            total_ufr += ufr

        total_loss /= self._hyper_params['TestSteps']
        total_tfr /= self._hyper_params['TestSteps']
        total_ufr /= self._hyper_params['TestSteps']

        self.test_loss_history.append(total_loss)
        self.test_accuracy_history.append(total_tfr)
        print('\nTest: Loss: ', total_loss, '; TFR: ', total_tfr, '; UFR: ', total_ufr)


if __name__ == '__main__':
    with tf.device("/device:CPU:0"):
        net = AdvNet("SimpleNet")
        data_generator = data.get_adversarial_data_generators(batch_size=config.hyper_params['BatchSize'])

        net.train(data_generator)
        net.plot_training_history("Adversarial CIFAR10", net._hyper_params['ValidateAfter'])
