from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import train_utils
import test_utils

import random
import os
import sys
import importlib
import argparse
import time
from datetime import datetime

import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim


def snapshot(sess, saver, model_dir, model_name, step):
    # save the model checkpoint
    print('snapshot...')
    start_time = time.time()
    checkpoint_path = os.path.join(model_dir, 'model-%s.ckpt' % model_name)

    # save trainable variables of sess into file
    saver.save(sess, checkpoint_path, global_step=step, write_meta_graph=False)
    duration = time.time() - start_time
    print('snapshot finisned in %.2f seconds' % duration)

    metagraph_path = os.path.join(model_dir, 'model-%s.meta' % model_name)
    if not os.path.exists(metagraph_path):
        print('saving metagraph...')
        start_time = time.time()
        saver.export_meta_graph(metagraph_path)
        duration = time.time() - start_time
        print('metagraph saved in %.2f seconds' % duration)


def evaluate(sess, enque_op,
             imgpaths_pl, labels_pl,
             phase_train_pl, batch_size_pl,
             embeddings, labels,
             image_list, actual_issame,
             batch_size, num_folds,
             log_dir, step, summary_writer):
    print("evaluating on lfw...")
    start_time = time.time()

    # Enqueue one epoch of image paths and labels
    labels_array = np.expand_dims(np.arange(0, len(image_list)), 1)
    paths_array = np.expand_dims(np.array(image_list), 1)

    sess.run(enque_op, {
        imgpaths_pl: paths_array,
        labels_pl: labels_array
    })

    embeddings_dim = embeddings.get_shape()[1]
    num_images = len(actual_issame) * 2
    assert num_images % batch_size == 0, 'Num of sample must be even.'

    num_batches = num_images // batch_size
    emb_array = np.zeros((num_images, embeddings_dim))
    lab_array = np.zeros((num_images,))
    for i in range(num_batches):
        feed_dict = {
            phase_train_pl: False,
            batch_size_pl: batch_size
        }
        emb, lab = sess.run([embeddings, labels], feed_dict=feed_dict)
        lab_array[lab] = lab
        emb_array[lab] = emb

    assert np.array_equal(lab_array, np.arange(num_images)) == True, 'Wrong labels used for evaluation'
    _, _, acc, val, val_std, far = test_utils.evaluate(emb_array, actual_issame, num_folds=num_folds)

    print('acc: %1.3f+-%1.3f' % (np.mean(acc), np.std(acc)))
    print('vr : %2.5f+=%2.5f @ FAR=%2.5F' % (val, val_std, far))
    lfw_time = time.time() - start_time
    # Summary
    summary = tf.Summary()
    summary.value.add(tag='lfw/acc', simple_value=np.mean(acc))
    summary.value.add(tag='lfw/vr', simple_value=val)
    summary.value.add(tag='time/lfw', simple_value=lfw_time)
    summary_writer.add_summary(summary, step)
    with open(os.path.join(log_dir, 'lfw_result.txt'), 'at') as fp:
        fp.write('%d\t%.5f\t%.5f\n' % (step, np.mean(acc), val))


def run_epoch(args, sess, epoch,
              image_list, label_list,
              deque_op, enque_op,
              imgpaths_pl, labels_pl, lr_pl,
              phase_train_pl, batch_size_pl,
              global_step, total_loss, reg_loss,
              train_op, summary_op, summary_writer):
    batch_num = 0
    if args.lr > 0.0:
        lr = args.lr
    else:
        lr = train_utils.get_learning_rate_from_file(args.lr_schedule_file, epoch)

    index_epoch = sess.run(deque_op)
    label_epoch = np.array(label_list)[index_epoch]
    image_epoch = np.array(image_list)[index_epoch]

    # Enqueue one epoch of image paths and labels
    # [notice: expand_dims: (n,) -> (n,1)]
    labels_array = np.expand_dims(label_epoch, 1)
    paths_array = np.expand_dims(image_epoch, 1)
    sess.run(enque_op, {
        imgpaths_pl: paths_array,
        labels_pl: labels_array
    })

    train_time = 0
    while batch_num < args.epoch_size:
        start_time = time.time()
        feed_dict = {
            lr_pl: lr,
            # [notice: 'phase_train_pl' is required by dropout]
            phase_train_pl: True,
            batch_size_pl: args.batch_size
        }
        # [notice: summary every 100 step]
        if batch_num == 0 or (batch_num + 1) % 100 == 0:
            err, _, step, reg, summary_str = sess.run([total_loss,
                                                       train_op, global_step, reg_loss, summary_op],
                                                      feed_dict=feed_dict)
            summary_writer.add_summary(summary_str, step)
        else:
            err, _, step, reg = sess.run([total_loss,
                                          train_op, global_step, reg_loss],
                                         feed_dict=feed_dict)
        duration = time.time() - start_time
        print('Epoch: [%d][%d/%d]\tTime: %.3f\tTotal Loss: %2.3f\tRegLoss: %2.3f' %
              (epoch, batch_num + 1, args.epoch_size, duration, err, np.sum(reg)))
        batch_num += 1
        train_time += duration

    summary = tf.Summary()
    summary.value.add(tag='time/total', simple_value=train_time)
    summary_writer.add_summary(summary, step)
    return step


def main(args):
    model_module = importlib.import_module(args.model_def)
    subdir = datetime.strftime(datetime.now(), '%Y%m%d-%H%M%S')
    log_dir = os.path.join(args.logs_base_dir, subdir)
    model_dir = os.path.join(args.models_base_dir, subdir)
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir)
    if not os.path.isdir(model_dir):
        os.makedirs(model_dir)
    print('log   dir: %s' % log_dir)
    print('model dir: %s' % model_dir)

    if args.lfw_dir:
        print('LFW directory: %s' % args.lfw_dir)
        pairs = test_utils.read_pairs(args.lfw_pairs)
        lfw_paths, actual_issame = test_utils.get_paths(args.lfw_dir, pairs, args.lfw_file_ext)

    with tf.Graph().as_default():
        # ---- data prepration ---- #
        image_list, label_list, num_classes = train_utils.get_datasets(args.data_dir, args.imglist_path)
        range_size = len(image_list)
        assert range_size > 0, 'The dataset should not be empty.'
        # random indices producer
        indices_que = tf.train.range_input_producer(range_size)
        deque_op = indices_que.dequeue_many(args.batch_size * args.epoch_size, 'index_dequeue')

        tf.set_random_seed(args.seed)
        random.seed(args.seed)
        np.random.seed(args.seed)
        global_step = tf.Variable(0, trainable=False)
        lr_pl = tf.placeholder(tf.float32, name='learning_rate')
        batch_size_pl = tf.placeholder(tf.int32, name='batch_size')
        phase_train_pl = tf.placeholder(tf.bool, name='phase_train')
        imgpaths_pl = tf.placeholder(tf.string, name='image_paths')
        labels_pl = tf.placeholder(tf.int64, name='labels')

        # filename queue
        input_queue = tf.FIFOQueue(
            # [notice: capacity > bach_size*epoch_size]
            capacity=100000,
            dtypes=[tf.string, tf.int64],
            shapes=[(1,), (1,)],
            shared_name=None, name='input_que')
        enque_op = input_queue.enqueue_many(
            [imgpaths_pl, labels_pl],
            name='enque_op')
        # define 4 readers
        num_threads = 4
        threads_input_list = []
        for _ in range(num_threads):
            img_paths, label = input_queue.dequeue()  # [notice: 'img_pathx' and 'label' are both tensors]
            images = []
            for img_path in tf.unstack(img_paths):
                img_contents = tf.read_file(img_path)
                img = tf.image.decode_jpeg(img_contents)
                if args.random_crop:
                    img = tf.random_crop(img, [args.image_size, args.image_size, 3])
                else:
                    img = tf.image.resize_image_with_crop_or_pad(img, args.image_size, args.image_size)
                if args.random_flip:
                    img = tf.image.random_flip_left_right(img)
                img.set_shape((args.image_size, args.image_size, 3))
                images.append(tf.image.per_image_standardization(img))  # prewhitened?
            threads_input_list.append([images, label])

        # define 4 buffer queue
        image_batch, label_batch = tf.train.batch_join(
            threads_input_list,
            # [notice: here is 'batch_size_pl', not 'batch_size'!!]
            batch_size=batch_size_pl,
            shapes=[(args.image_size, args.image_size, 3), ()],
            enqueue_many=True,
            # [notice: how long the prefetching is allowed to fill the queue]
            capacity=4 * num_threads * args.batch_size,
            allow_smaller_final_batch=True)
        image_batch = tf.identity(image_batch, 'image_batch')
        image_batch = tf.identity(image_batch, 'input')
        label_batch = tf.identity(label_batch, 'label_batch')
        print('Total classes: %d' % num_classes)
        print('Total images:  %d' % range_size)
        tf.summary.image('input_images', image_batch, 10)

        # ---- build graph ---- #
        with tf.device('/gpu:%d' % args.gpu_id):
            # embeddings
            prelogits, _ = model_module.inference(
                image_batch,
                args.keep_prob,
                phase_train=phase_train_pl,
                weight_decay=args.weight_decay)
            # logits
            logits = slim.fully_connected(
                prelogits,
                num_classes,
                activation_fn=None,
                weights_initializer=tf.truncated_normal_initializer(stddev=0.1),
                weights_regularizer=slim.l2_regularizer(args.weight_decay),
                scope='Logits',
                reuse=False)
            # normalized features
            # [notice: used in test stage]
            embeddings = tf.nn.l2_normalize(
                prelogits, 1, 1e-10, name='embeddings')
            # ---- define loss & train op ---- #
            # center loss
            if args.center_loss_factor > 0.0:
                prelogits_center_loss, _ = train_utils.center_loss(
                    prelogits,
                    label_batch,
                    args.center_loss_alpha,
                    num_classes)
                tf.summary.scalar('center_loss',
                                  prelogits_center_loss * args.center_loss_factor)
                tf.add_to_collection(tf.GraphKeys.REGULARIZATION_LOSSES,
                                     prelogits_center_loss * args.center_loss_factor)
            # cross-entropy
            cross_entropy_mean = tf.reduce_mean(
                tf.nn.sparse_softmax_cross_entropy_with_logits(
                    labels=label_batch,
                    logits=logits),
                name='cross_entropy')
            tf.add_to_collection('losses', cross_entropy_mean)
            # regularity: weight decay
            reg_loss = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
            # total loss
            total_loss = tf.add_n([cross_entropy_mean] + reg_loss, name='total_loss')
            # [notice: here we decay manually]
            lr = tf.train.exponential_decay(lr_pl,
                                            global_step,
                                            args.lr_decay_epochs * args.epoch_size,
                                            args.lr_decay_factor,
                                            staircase=True)
            tf.summary.scalar('learning_rate', lr)
            train_op = train_utils.get_train_op(total_loss,
                                                global_step,
                                                args.optimizer,
                                                lr,
                                                args.moving_average_decay,
                                                # what is the usage of tf.global_variables()?
                                                tf.trainable_variables())

        # ---- training ---- #
        # [notice: use 'allow_growth' instead of memory_fraction]
        gpu_options = tf.GPUOptions(allow_growth=True)
        # [notice: use 'allow_soft_placement' to solve the problem of 'no supported kernel...']
        sess = tf.Session(config=tf.ConfigProto(
            gpu_options=gpu_options,
            log_device_placement=False,
            allow_soft_placement=True))
        sess.run(tf.global_variables_initializer())
        sess.run(tf.local_variables_initializer())

        # [notice: 'max_to_keep': keep at most 'max_to_keep' checkpoint files]
        saver = tf.train.Saver(tf.trainable_variables(), max_to_keep=5)
        summary_op = tf.summary.merge_all()
        summary_writer = tf.summary.FileWriter(log_dir, sess.graph)

        coord = tf.train.Coordinator()
        tf.train.start_queue_runners(coord=coord, sess=sess)

        with sess.as_default():
            if args.pretrained_model:
                print('Resume training: %s' % args.pretrained_model)
                saver.restore(sess, args.pretrained_model)
            print('Start training ...')
            epoch = 0
            while epoch < args.max_num_epochs:
                step = sess.run(global_step, feed_dict=None)  # training counter
                epoch = step // args.epoch_size

                # run epoch
                run_epoch(args, sess, epoch,
                          image_list, label_list,
                          deque_op, enque_op,
                          imgpaths_pl, labels_pl, lr_pl,
                          phase_train_pl, batch_size_pl,
                          global_step, total_loss, reg_loss,
                          train_op, summary_op, summary_writer)

                # snapshot for currently learnt weights
                snapshot(sess, saver, model_dir, subdir, step)

                # evaluate on LFW
                if args.lfw_dir:
                    evaluate(sess, enque_op, imgpaths_pl, labels_pl,
                             phase_train_pl, batch_size_pl,
                             embeddings, label_batch, lfw_paths, actual_issame,
                             args.lfw_batch_size, args.lfw_num_folds,
                             log_dir, step, summary_writer)
    sess.close()


def parse_arguments(argv):
    parser = argparse.ArgumentParser()
    # ---- file related ---- #
    parser.add_argument('--model_def', type=str,
                        help='Model definition. Points to a module containing the definition of the inference graph.',
                        default='models.nn4')
    parser.add_argument('--logs_base_dir', type=str,
                        help='Directory where to write event logs.',
                        default='logs')
    parser.add_argument('--models_base_dir', type=str,
                        help='Directory where to write trained mdels and checkpoints.',
                        default='models')
    parser.add_argument('--imglist_path', type=str,
                        help='Training images list.',
                        default='/export_data/czj/data/casia/files/train_set.txt')
    parser.add_argument('--data_dir', type=str,
                        help='Path to the data directory containing aligned faces.',
                        default='/exports_data/czj/data/lfw/lfw_aligned/')

    # ---- data related ---- #
    parser.add_argument('--image_size', type=int,
                        help='Image size (height, width) in pixels.',
                        default=96)
    parser.add_argument('--random_crop',
                        help='Performs random cropping of training images. If false, the center image_size pixels from the training images are used. ' +
                             'If the size of the images in the data directory is equal to image_size no cropping is performed',
                        action='store_true')
    parser.add_argument('--random_flip',
                        help='Performs random horizontal flipping of training images.',
                        action='store_true')
    parser.add_argument('--random_rotate',
                        help='Performs random rotations of training images.',
                        action='store_true')

    # ---- training related ---- #
    parser.add_argument('--seed', type=int,
                        help='Random seed.',
                        default=666)
    parser.add_argument('--max_num_epochs', type=int,
                        help='Number of epochs to run.',
                        default=80)
    parser.add_argument('--batch_size', type=int,
                        help='Number of images to process in a batch.',
                        default=10)
    parser.add_argument('--epoch_size', type=int,
                        help='Number of batches per epoch.',
                        default=1000)
    parser.add_argument('--keep_prob', type=float,
                        help='Keep probability of dropout for the fully connected layer(s).',
                        default=1.0)
    parser.add_argument('--lr', type=float,
                        help='Learning rate',
                        default=0.1)
    parser.add_argument('--lr_decay_epochs', type=int,
                        help='Number of epochs between learning rate decay.',
                        default=100)
    parser.add_argument('--lr_decay_factor', type=float,
                        help='Learning rate decay factor.',
                        default=1.0)
    parser.add_argument('--optimizer', type=str,
                        choices=['ADAGRAD', 'ADADELTA', 'ADAM', 'RMSPROP', 'MOM'],
                        help='The optimization algorithm to use',
                        default='ADAGRAD')
    parser.add_argument('--lr_schedule_file', type=str,
                        help='File containing the learning rate schedule that is used when learning_rate is set to to -1.',
                        default='lr_decay.txt')
    parser.add_argument('--weight_decay', type=float,
                        help='L2 weight regularization.',
                        default=0.0)
    parser.add_argument('--moving_average_decay', type=float,
                        help='Exponential decay for tracking of training parameters.',
                        default=0.9999)
    parser.add_argument('--log_histograms',
                        help='Enables logging of weight/bias histograms in tensorboard.',
                        action='store_true')
    parser.add_argument('--gpu_memory_fraction', type=float,
                        help='Upper bound on the amount of GPU memory that will be used by the process.',
                        default=0.5)
    parser.add_argument('--gpu_id', type=int,
                        help='gpu device',
                        default=0)

    # ---- model related ---- #
    parser.add_argument('--pretrained_model', type=str,
                        help='Load a pretrained model before training starts.')
    parser.add_argument('--embedding_size', type=int,
                        help='Dimensionality of the embedding.',
                        default=128)
    parser.add_argument('--center_loss_factor', type=float,
                        help='Center loss factor.',
                        default=0.0)
    parser.add_argument('--center_loss_alpha', type=float,
                        help='Center update rate for center loss.',
                        default=0.95)

    # ---- LFW related ---- #
    parser.add_argument('--lfw_pairs', type=str,
                        help='LFW pairs file.',
                        default='')
    parser.add_argument('--lfw_file_ext', type=str,
                        help='The file extension for the LFW dataset.',
                        default='_face_.jpg')
    parser.add_argument('--lfw_dir', type=str,
                        help='Path to the data directory containing aligned face patches.')
    parser.add_argument('--lfw_batch_size', type=int,
                        help='Number of images to process in a batch in the LFW test set.',
                        default=10)
    parser.add_argument('--lfw_num_folds', type=int,
                        help='Number of folds to use for cross validation. Mainly used for testing.',
                        default=10)
    return parser.parse_args(argv)


if __name__ == '__main__':
    main(parse_arguments(sys.argv[1:]))
