# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import tensorflow as tf
import tensorflow.contrib.slim as slim
import tensorflow.contrib.slim.nets as nets
from cleverhans.attacks import Model
from tensorflow.contrib.slim.nets import inception
import os
import csv
from scipy.misc import imread
from scipy.misc import imresize
from PIL import Image
import numpy as np
tf.logging.set_verbosity(tf.logging.ERROR)


tf.flags.DEFINE_string(
    'checkpoint_path', '', 'Path to checkpoint for inception network.')
tf.flags.DEFINE_string(
    'input_dir', '', 'Input directory with images.')
tf.flags.DEFINE_string(
    'output_dir', '', 'Output directory with images.')
tf.flags.DEFINE_integer(
    'image_width', 224, 'Width of each input images.')
tf.flags.DEFINE_integer(
    'image_height', 224, 'Height of each input images.')
tf.flags.DEFINE_integer(
    'batch_size', 8, 'How many images process at one time.')
tf.flags.DEFINE_integer(
    'num_classes', 110, 'Number of Classes')
FLAGS = tf.flags.FLAGS

def load_images(input_dir, batch_shape):
    images = np.zeros(batch_shape)
    labels = np.zeros(batch_shape[0], dtype=np.int32)
    filenames = []
    idx = 0
    batch_size = batch_shape[0]
    with open(os.path.join(input_dir, 'dev.csv'), 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            filepath = os.path.join(input_dir, row['filename'])
            with open(filepath,'rb') as f:
                raw_image = imread(f, mode='RGB').astype(np.float)
                image = imresize(raw_image, [FLAGS.image_height, FLAGS.image_width]) / 255.0
            # Images for inception classifier are normalized to be in [-1, 1] interval.
            images[idx, :, :, :] = image * 2.0 - 1.0
            labels[idx] = int(row['targetedLabel'])
            filenames.append(os.path.basename(filepath))
            idx += 1
            if idx == batch_size:
                yield filenames, images, labels
                filenames = []
                images = np.zeros(batch_shape)
                labels = np.zeros(batch_shape[0], dtype=np.int32)
                idx = 0
        if idx > 0:
            yield filenames, images, labels


def save_images(images, filenames, output_dir):
    for i, filename in enumerate(filenames):
        # Images for inception classifier are normalized to be in [-1, 1] interval,
        # so rescale them back to [0, 1].
        with open(os.path.join(output_dir, filename), 'wb') as f:
            img = (((images[i, :, :, :] + 1.0) * 0.5) * 255.0).astype(np.uint8)
            # resize back to [299, 299]
            r_img = imresize(img, [299, 299])
            Image.fromarray(r_img).save(f, format='PNG')


#saver=tf.train.import_meta_graph('resnet_v1_50/model.ckpt-49800.meta')
class InceptionModel(Model):
    """Model class for CleverHans library."""
    def __init__(self, nb_classes):
        super(InceptionModel, self).__init__(nb_classes=nb_classes,
                                             needs_dummy_fprop=True)
        self.built = False

    def __call__(self, x_input, return_logits=False):
        """Constructs model and return probabilities for given input."""
        reuse = True if self.built else None
        with slim.arg_scope(inception.inception_v1_arg_scope()):
            _, end_points = inception.inception_v1(
                x_input, num_classes=self.nb_classes, is_training=False,
                reuse=reuse)
        self.built = True
        self.logits = end_points['Logits']
        # Strip off the extra reshape op at the output
        self.probs = end_points['Predictions'].op.inputs[0]
        if return_logits:
            return self.logits
        else:
            return self.probs

    def get_logits(self, x_input):
        return self(x_input, return_logits=True)

    def get_probs(self, x_input):
        return self(x_input)

def main(_):

    sess=tf.InteractiveSession(config=tf.ConfigProto(allow_soft_placement=True))

    batch_shape= [FLAGS.batch_size, FLAGS.image_height, FLAGS.image_width, 3]
    batch_size=FLAGS.batch_size
    nb_classes = FLAGS.num_classes

    tf.logging.set_verbosity(tf.logging.INFO)
    
    image=tf.Variable(tf.zeros(batch_shape))
    model=InceptionModel(nb_classes)
    logits,probs=model.get_logits(image),model.get_probs(image)
    saver = tf.train.Saver(slim.get_model_variables())
    saver.restore(sess, FLAGS.checkpoint_path)

    x = tf.placeholder(tf.float32, batch_shape)

    x_hat = image # our trainable adversarial input
    assign_op = tf.assign(x_hat, x)

    learning_rate = tf.placeholder(tf.float32, ())
    y_hat = tf.placeholder(tf.int32, (batch_size,))

    labels = tf.one_hot(y_hat, nb_classes)
    #对图片进行旋转 每张图生成5张旋转图，求平局的loss
    num_samples = 5
    average_loss = 0
    for i in range(num_samples):
        rotated = tf.contrib.image.rotate(
            image, tf.random_uniform((), minval=-np.pi/4, maxval=np.pi/4))
        rotated_logits=model.get_logits(rotated)
        average_loss += tf.nn.softmax_cross_entropy_with_logits(logits=rotated_logits, labels=labels) / num_samples
    #loss = tf.nn.softmax_cross_entropy_with_logits(logits=logits, labels=[labels])
    optim_step = tf.train.GradientDescentOptimizer(learning_rate).minimize(average_loss, var_list=[x_hat])

    epsilon = tf.placeholder(tf.float32, ())

    below = x - epsilon
    above = x + epsilon
    projected = tf.clip_by_value(tf.clip_by_value(x_hat, below, above), -1, 1)#clip x_hat,并将其约束到0，1之间作为输入。
    with tf.control_dependencies([projected]):#此函数指定某些操作执行的依赖关系，即执行完projected,才能再执行project_step
        project_step = tf.assign(x_hat, projected)


    demo_epsilon = 16.0/255.0 # 一个很小的扰动
    demo_lr = 2e-1
    demo_steps = 20

    for filenames, images, tlabels in load_images(FLAGS.input_dir, batch_shape):
        # initialization step #先初始化x_hat为x
        sess.run(assign_op, feed_dict={x: images})
        # projected gradient descent
        for i in range(demo_steps):
            # gradient descent step
            _, loss_value = sess.run(
                [optim_step, average_loss],
                feed_dict={learning_rate: demo_lr, y_hat: tlabels})
            # project step
            sess.run(project_step, feed_dict={x: images, epsilon: demo_epsilon})
        adv = x_hat.eval() # retrieve the adversarial example
        #classify(adv,tlabels)
        save_images(adv,filenames,FLAGS.output_dir)

if __name__=='__main__':
    tf.app.run()

