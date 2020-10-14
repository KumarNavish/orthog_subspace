# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Training script for split miniImageNET 100 experiment.
"""
from __future__ import print_function

import argparse
import os
import sys
import math
import time
import random 

import datetime
import collections
import numpy as np
import tensorflow as tf
from copy import deepcopy
from six.moves import cPickle as pickle

from utils.data_utils import construct_split_miniImagenet
from utils.utils import get_sample_weights, sample_from_dataset, update_episodic_memory, concatenate_datasets, samples_for_each_class, sample_from_dataset_icarl, compute_fgt, load_task_specific_data, grad_check
from utils.utils import average_acc_stats_across_runs, average_fgt_stats_across_runs, update_reservior, update_fifo_buffer, generate_projection_matrix, unit_test_projection_matrices
from utils.vis_utils import plot_acc_multiple_runs, plot_histogram, snapshot_experiment_meta_data, snapshot_experiment_eval, snapshot_task_labels
from model import Model

###############################################################
################ Some definitions #############################
### These will be edited by the command line options ##########
###############################################################

## Training Options
NUM_RUNS = 5           # Number of experiments to average over
TRAIN_ITERS = 2000      # Number of training iterations per task
BATCH_SIZE = 16
LEARNING_RATE = 0.1    
RANDOM_SEED = 1234
VALID_OPTIMS = ['SGD', 'MOMENTUM', 'ADAM']
OPTIM = 'SGD'
OPT_MOMENTUM = 0.9
OPT_POWER = 0.9
VALID_ARCHS = ['CNN', 'RESNET-S', 'RESNET-B', 'VGG']
ARCH = 'RESNET-S'

## Model options
MODELS = ['VAN', 'PI', 'EWC', 'MAS', 'RWALK', 'M-EWC', 'S-GEM', 'A-GEM', 'FTR_EXT', 'PNN', 'ER-Reservoir', 'ER-Ringbuffer', 'SUBSPACE-PROJ', 'ER-SUBSPACE', 'PROJ-SUBSPACE-GP', 'ER-SUBSPACE-GP'] #List of valid models 
IMP_METHOD = 'EWC'
SYNAP_STGTH = 75000
FISHER_EMA_DECAY = 0.9          # Exponential moving average decay factor for Fisher computation (online Fisher)
FISHER_UPDATE_AFTER = 50        # Number of training iterations for which the F_{\theta}^t is computed (see Eq. 10 in RWalk paper) 
SAMPLES_PER_CLASS = 13
IMG_HEIGHT = 84
IMG_WIDTH = 84
IMG_CHANNELS = 3
TOTAL_CLASSES = 100          # Total number of classes in the dataset 
VISUALIZE_IMPORTANCE_MEASURE = False
MEASURE_CONVERGENCE_AFTER = 0.9
EPS_MEM_BATCH_SIZE = 256
DEBUG_EPISODIC_MEMORY = False
K_FOR_CROSS_VAL = 3
TIME_MY_METHOD = False
COUNT_VIOLATONS = False
MEASURE_PERF_ON_EPS_MEMORY = False

## Logging, saving and testing options
LOG_DIR = './split_miniImagenet_results'
RESNET18_miniImageNET10_CHECKPOINT = './resnet-18-pretrained-miniImagenet10/model.ckpt-19999'
DATA_FILE = 'miniImageNet_Dataset/miniImageNet_full.pickle'

## Evaluation options

## Task split
NUM_TASKS = 10
MULTI_TASK = False
PROJECTION_RANK = 50
GRAD_CHECK = False
QR = False
SVB = False

# Define function to load/ store training weights. We will use ImageNet initialization later on
def save(saver, sess, logdir, step):
   '''Save weights.

   Args:
     saver: TensorFlow Saver object.
     sess: TensorFlow session.
     logdir: path to the snapshots directory.
     step: current training step.
   '''
   model_name = 'model.ckpt'
   checkpoint_path = os.path.join(logdir, model_name)

   if not os.path.exists(logdir):
      os.makedirs(logdir)
   saver.save(sess, checkpoint_path, global_step=step)
   print('The checkpoint has been created.')

def load(saver, sess, ckpt_path):
    '''Load trained weights.

    Args:
        saver: TensorFlow Saver object.
        sess: TensorFlow session.
        ckpt_path: path to checkpoint file with parameters.
    '''
    saver.restore(sess, ckpt_path)
    print("Restored model parameters from {}".format(ckpt_path))


def get_arguments():
    """Parse all the arguments provided from the CLI.

    Returns:
      A list of parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Script for split miniImagenet experiment.")
    parser.add_argument("--cross-validate-mode", action="store_true",
            help="If option is chosen then snapshoting after each batch is disabled")
    parser.add_argument("--online-cross-val", action="store_true",
            help="If option is chosen then enable the online cross validation of the learning rate")
    parser.add_argument("--train-single-epoch", action="store_true", 
            help="If option is chosen then train for single epoch")
    parser.add_argument("--eval-single-head", action="store_true",
            help="If option is chosen then evaluate on a single head setting.")
    parser.add_argument("--maintain-orthogonality", action="store_true",
                        help="If option is chosen then weights will be projected to Steifel manifold.")
    parser.add_argument("--arch", type=str, default=ARCH,
                        help="Network Architecture for the experiment.\
                                \n \nSupported values: %s"%(VALID_ARCHS))
    parser.add_argument("--num-runs", type=int, default=NUM_RUNS,
                       help="Total runs/ experiments over which accuracy is averaged.")
    parser.add_argument("--train-iters", type=int, default=TRAIN_ITERS,
                       help="Number of training iterations for each task.")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                       help="Mini-batch size for each task.")
    parser.add_argument("--random-seed", type=int, default=RANDOM_SEED,
                       help="Random Seed.")
    parser.add_argument("--learning-rate", type=float, default=LEARNING_RATE,
                       help="Starting Learning rate for each task.")
    parser.add_argument("--optim", type=str, default=OPTIM,
                        help="Optimizer for the experiment. \
                                \n \nSupported values: %s"%(VALID_OPTIMS))
    parser.add_argument("--imp-method", type=str, default=IMP_METHOD,
                       help="Model to be used for LLL. \
                        \n \nSupported values: %s"%(MODELS))
    parser.add_argument("--synap-stgth", type=float, default=SYNAP_STGTH,
                       help="Synaptic strength for the regularization.")
    parser.add_argument("--fisher-ema-decay", type=float, default=FISHER_EMA_DECAY,
                       help="Exponential moving average decay for Fisher calculation at each step.")
    parser.add_argument("--fisher-update-after", type=int, default=FISHER_UPDATE_AFTER,
                       help="Number of training iterations after which the Fisher will be updated.")
    parser.add_argument("--mem-size", type=int, default=SAMPLES_PER_CLASS,
                       help="Total size of episodic memory.")
    parser.add_argument("--eps-mem-batch", type=int, default=EPS_MEM_BATCH_SIZE,
                       help="Number of samples per class from previous tasks.")
    parser.add_argument("--num-tasks", type=int, default=NUM_TASKS,
                       help="Number of tasks.")
    parser.add_argument("--subspace-share-dims", type=int, default=0,
                       help="Number of dimensions to share across tasks.")
    parser.add_argument("--data-file", type=str, default=DATA_FILE,
                       help="miniImageNet data file.")
    parser.add_argument("--log-dir", type=str, default=LOG_DIR,
                       help="Directory where the plots and model accuracies will be stored.")
    return parser.parse_args()

def train_task_sequence(model, sess, datasets, args):
    """
    Train and evaluate LLL system such that we only see a example once
    Args:
    Returns:
        dict    A dictionary containing mean and stds for the experiment
    """
    # List to store accuracy for each run
    runs = []
    task_labels_dataset = []

    if model.imp_method in {'A-GEM', 'ER-Ringbuffer', 'ER-Reservoir', 'ER-SUBSPACE', 'ER-SUBSPACE-GP'}:
        use_episodic_memory = True
    else:
        use_episodic_memory = False

    batch_size = args.batch_size
    # Loop over number of runs to average over
    for runid in range(args.num_runs):
        print('\t\tRun %d:'%(runid))

        # Initialize the random seeds
        np.random.seed(args.random_seed+runid)
        random.seed(args.random_seed+runid)

        # Get the task labels from the total number of tasks and full label space
        task_labels = []
        classes_per_task = TOTAL_CLASSES// args.num_tasks
        total_classes = classes_per_task * model.num_tasks
        if args.online_cross_val:
            label_array = np.arange(total_classes)
        else:
            class_label_offset = K_FOR_CROSS_VAL * classes_per_task
            label_array = np.arange(class_label_offset, total_classes+class_label_offset)

        np.random.shuffle(label_array)
        for tt in range(model.num_tasks):
            tt_offset = tt*classes_per_task
            task_labels.append(list(label_array[tt_offset:tt_offset+classes_per_task]))
            print('Task: {}, Labels:{}'.format(tt, task_labels[tt]))

        # Store the task labels
        task_labels_dataset.append(task_labels)

        # Set episodic memory size
        episodic_mem_size = args.mem_size * total_classes

        # Initialize all the variables in the model
        sess.run(tf.global_variables_initializer())

        # Run the init ops
        model.init_updates(sess)

        # List to store accuracies for a run
        evals = []

        # List to store the classes that we have so far - used at test time
        test_labels = []

        if use_episodic_memory:
            # Reserve a space for episodic memory
            episodic_images = np.zeros([episodic_mem_size, IMG_HEIGHT, IMG_WIDTH, IMG_CHANNELS])
            episodic_labels = np.zeros([episodic_mem_size, TOTAL_CLASSES])
            count_cls = np.zeros(TOTAL_CLASSES, dtype=np.int32)
            episodic_filled_counter = 0
            examples_seen_so_far = 0

        # Mask for softmax 
        logit_mask = np.zeros(TOTAL_CLASSES)
        nd_logit_mask = np.zeros([model.num_tasks, TOTAL_CLASSES])

        if COUNT_VIOLATONS:
            violation_count = np.zeros(model.num_tasks)
            vc = 0

        proj_matrices = generate_projection_matrix(model.num_tasks, feature_dim=model.subspace_proj.get_shape()[0], share_dims=args.subspace_share_dims, qr=QR)
        # Check the sanity of the generated matrices
        unit_test_projection_matrices(proj_matrices)

        # TODO: Temp for gradients check
        prev_task_grads = []
        # Training loop for all the tasks
        for task in range(len(task_labels)):
            print('\t\tTask %d:'%(task))
        
            # If not the first task then restore weights from previous task
            if(task > 0 and model.imp_method != 'PNN'):
                model.restore(sess)

            if model.imp_method == 'PNN':
                pnn_train_phase = np.array(np.zeros(model.num_tasks), dtype=np.bool)
                pnn_train_phase[task] = True
                pnn_logit_mask = np.zeros([model.num_tasks, TOTAL_CLASSES])

            # If not in the cross validation mode then concatenate the train and validation sets
            task_train_images, task_train_labels = load_task_specific_data(datasets[0]['train'], task_labels[task])

            # If multi_task is set then train using all the datasets of all the tasks
            if MULTI_TASK:
                if task == 0:
                    for t_ in range(1, len(task_labels)):
                        task_tr_images, task_tr_labels = load_task_specific_data(datasets[0]['train'], task_labels[t_])
                        task_train_images = np.concatenate((task_train_images, task_tr_images), axis=0)
                        task_train_labels = np.concatenate((task_train_labels, task_tr_labels), axis=0)

                else:
                    # Skip training for this task
                    continue

            print('Received {} images, {} labels at task {}'.format(task_train_images.shape[0], task_train_labels.shape[0], task))
            print('Unique labels in the task: {}'.format(np.unique(np.nonzero(task_train_labels)[1])))

            # Test for the tasks that we've seen so far
            test_labels += task_labels[task]

            # Assign equal weights to all the examples
            task_sample_weights = np.ones([task_train_labels.shape[0]], dtype=np.float32)

            num_train_examples = task_train_images.shape[0]

            logit_mask[:] = 0
            # Train a task observing sequence of data
            if args.train_single_epoch:
                # Ceiling operation
                num_iters = (num_train_examples + batch_size - 1) // batch_size
                if args.cross_validate_mode:
                    logit_mask[task_labels[task]] = 1.0
            else:
                num_iters = args.train_iters
                # Set the mask only once before starting the training for the task
                logit_mask[task_labels[task]] = 1.0

            if MULTI_TASK:
                logit_mask[:] = 1.0

            # Randomly suffle the training examples
            perm = np.arange(num_train_examples)
            np.random.shuffle(perm)
            train_x = task_train_images[perm]
            train_y = task_train_labels[perm]
            task_sample_weights = task_sample_weights[perm]

            # Array to store accuracies when training for task T
            ftask = []

            # Number of iterations after which convergence is checked
            convergence_iters = int(num_iters * MEASURE_CONVERGENCE_AFTER)

            # Training loop for task T
            for iters in range(num_iters):

                if args.train_single_epoch and not args.cross_validate_mode and not MULTI_TASK:
                    if (iters <= 20) or (iters > 20 and iters % 50 == 0):
                        # Snapshot the current performance across all tasks after each mini-batch
                        fbatch = test_task_sequence(model, sess, datasets[0]['test'], task_labels, task, projection_matrices=proj_matrices)
                        ftask.append(fbatch)
                        if model.imp_method == 'PNN':
                            pnn_train_phase[:] = False
                            pnn_train_phase[task] = True
                            pnn_logit_mask[:] = 0
                            pnn_logit_mask[task][task_labels[task]] = 1.0
                        elif model.imp_method in {'A-GEM', 'ER-Ringbuffer'}:
                            nd_logit_mask[:] = 0
                            nd_logit_mask[task][task_labels[task]] = 1.0
                        else:
                            # Set the output labels over which the model needs to be trained 
                            logit_mask[:] = 0
                            logit_mask[task_labels[task]] = 1.0

                if args.train_single_epoch:
                    offset = iters * batch_size
                    if (offset+batch_size <= num_train_examples):
                        residual = batch_size
                    else:
                        residual = num_train_examples - offset

                    if model.imp_method == 'PNN':
                        feed_dict = {model.x: train_x[offset:offset+residual], model.y_[task]: train_y[offset:offset+residual], 
                                     model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 0.5, model.learning_rate: args.learning_rate}
                        train_phase_dict = {m_t: i_t for (m_t, i_t) in zip(model.train_phase, pnn_train_phase)}
                        logit_mask_dict = {m_t: i_t for (m_t, i_t) in zip(model.output_mask, pnn_logit_mask)}
                        feed_dict.update(train_phase_dict)
                        feed_dict.update(logit_mask_dict)
                    else:
                        feed_dict = {model.x: train_x[offset:offset+residual], model.y_: train_y[offset:offset+residual], 
                                model.sample_weights: task_sample_weights[offset:offset+residual],
                                model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 0.5, 
                                     model.train_phase: True, model.learning_rate: args.learning_rate}
                else:
                    offset = (iters * batch_size) % (num_train_examples - batch_size)
                    residual = batch_size
                    if model.imp_method == 'PNN':
                        feed_dict = {model.x: train_x[offset:offset+batch_size], model.y_[task]: train_y[offset:offset+batch_size], 
                                     model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 0.5, model.learning_rate: args.learning_rate}
                        train_phase_dict = {m_t: i_t for (m_t, i_t) in zip(model.train_phase, pnn_train_phase)}
                        logit_mask_dict = {m_t: i_t for (m_t, i_t) in zip(model.output_mask, pnn_logit_mask)}
                        feed_dict.update(train_phase_dict)
                        feed_dict.update(logit_mask_dict)
                    else:
                        feed_dict = {model.x: train_x[offset:offset+batch_size], model.y_: train_y[offset:offset+batch_size], 
                                model.sample_weights: task_sample_weights[offset:offset+batch_size],
                                model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 0.5, 
                                     model.train_phase: True, model.learning_rate: args.learning_rate}
                
                if model.imp_method == 'VAN':
                    feed_dict[model.output_mask] = logit_mask
                    _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)
               
                elif model.imp_method == 'PROJ-SUBSPACE-GP':
                    if task == 0:
                        feed_dict[model.output_mask] = logit_mask
                        feed_dict[model.subspace_proj] = proj_matrices[task]
                        _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)
                    else:
                        # Compute gradient in \perp space
                        logit_mask[:] = 0
                        for tt in range(task):
                            logit_mask[task_labels[tt]] = 1.0
                        feed_dict[model.output_mask] = logit_mask
                        feed_dict[model.train_phase] = False
                        feed_dict[model.subspace_proj] = np.eye(proj_matrices[task].shape[0]) - proj_matrices[task]
                        sess.run(model.store_ref_grads, feed_dict=feed_dict)
                        # Compute gradient in P space and train
                        logit_mask[task_labels[task]] = 1.0
                        feed_dict[model.output_mask] = logit_mask
                        feed_dict[model.train_phase] = True
                        feed_dict[model.subspace_proj] = proj_matrices[task]
                        _, loss = sess.run([model.train_gp, model.gp_total_loss], feed_dict=feed_dict)
                    reg = 0.0

                elif model.imp_method == 'SUBSPACE-PROJ':
                    feed_dict[model.output_mask] = logit_mask
                    feed_dict[model.subspace_proj] = proj_matrices[task]
                    if args.maintain_orthogonality:
                        _, loss = sess.run([model.train_stiefel, model.reg_loss], feed_dict=feed_dict)
                    else:
                        _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'PNN':
                    _, loss = sess.run([model.train[task], model.unweighted_entropy[task]], feed_dict=feed_dict)

                elif model.imp_method == 'FTR_EXT':
                    feed_dict[model.output_mask] = logit_mask
                    if task == 0:
                        _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)
                    else:
                        _, loss = sess.run([model.train_classifier, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'EWC' or model.imp_method == 'M-EWC':
                    feed_dict[model.output_mask] = logit_mask
                    # If first iteration of the first task then set the initial value of the running fisher
                    if task == 0 and iters == 0:
                        sess.run([model.set_initial_running_fisher], feed_dict=feed_dict)
                    # Update fisher after every few iterations
                    if (iters + 1) % model.fisher_update_after == 0:
                        sess.run(model.set_running_fisher)
                        sess.run(model.reset_tmp_fisher)
                    
                    if (iters >= convergence_iters) and (model.imp_method == 'M-EWC'):
                        _, _, _, _, loss = sess.run([model.weights_old_ops_grouped, model.set_tmp_fisher, model.train, model.update_small_omega, 
                                              model.reg_loss], feed_dict=feed_dict)
                    else:
                        _, _, loss = sess.run([model.set_tmp_fisher, model.train, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'PI':
                    feed_dict[model.output_mask] = logit_mask
                    _, _, _, loss = sess.run([model.weights_old_ops_grouped, model.train, model.update_small_omega, 
                                              model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'MAS':
                    feed_dict[model.output_mask] = logit_mask
                    _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'A-GEM':
                    if task == 0:
                        nd_logit_mask[:] = 0
                        nd_logit_mask[task][task_labels[task]] = 1.0
                        logit_mask_dict = {m_t: i_t for (m_t, i_t) in zip(model.output_mask, nd_logit_mask)}
                        feed_dict.update(logit_mask_dict)
                        feed_dict[model.mem_batch_size] = batch_size
                        # Normal application of gradients
                        _, loss = sess.run([model.train_first_task, model.agem_loss], feed_dict=feed_dict)
                    else:
                        ## Compute and store the reference gradients on the previous tasks
                        # Set the mask for all the previous tasks so far
                        nd_logit_mask[:] = 0
                        for tt in range(task):
                            nd_logit_mask[tt][task_labels[tt]] = 1.0

                        if episodic_filled_counter <= args.eps_mem_batch:
                            mem_sample_mask = np.arange(episodic_filled_counter)
                        else:
                            # Sample a random subset from episodic memory buffer
                            mem_sample_mask = np.random.choice(episodic_filled_counter, args.eps_mem_batch, replace=False) # Sample without replacement so that we don't sample an example more than once
                        # Store the reference gradient
                        ref_feed_dict = {model.x: episodic_images[mem_sample_mask], model.y_: episodic_labels[mem_sample_mask], 
                                         model.keep_prob: 1.0, model.train_phase: True, model.learning_rate: args.learning_rate}
                        logit_mask_dict = {m_t: i_t for (m_t, i_t) in zip(model.output_mask, nd_logit_mask)}
                        ref_feed_dict.update(logit_mask_dict)
                        ref_feed_dict[model.mem_batch_size] = float(len(mem_sample_mask))
                        sess.run(model.store_ref_grads, feed_dict=ref_feed_dict)

                        # Compute the gradient for current task and project if need be
                        nd_logit_mask[:] = 0
                        nd_logit_mask[task][task_labels[task]] = 1.0
                        logit_mask_dict = {m_t: i_t for (m_t, i_t) in zip(model.output_mask, nd_logit_mask)}
                        feed_dict.update(logit_mask_dict)
                        feed_dict[model.mem_batch_size] = batch_size
                        if COUNT_VIOLATONS:
                            vc, _, loss = sess.run([model.violation_count, model.train_subseq_tasks, model.agem_loss], feed_dict=feed_dict)
                        else:
                            _, loss = sess.run([model.train_subseq_tasks, model.agem_loss], feed_dict=feed_dict)
                    # Put the batch in the ring buffer
                    update_fifo_buffer(train_x[offset:offset+residual], train_y[offset:offset+residual], episodic_images, episodic_labels,
                                       task_labels[task], args.mem_size, count_cls, episodic_filled_counter)

                elif model.imp_method == 'RWALK':
                    feed_dict[model.output_mask] = logit_mask
                    # If first iteration of the first task then set the initial value of the running fisher
                    if task == 0 and iters == 0:
                        sess.run([model.set_initial_running_fisher], feed_dict=feed_dict)
                        # Store the current value of the weights
                        sess.run(model.weights_delta_old_grouped)
                    # Update fisher and importance score after every few iterations
                    if (iters + 1) % model.fisher_update_after == 0:
                        # Update the importance score using distance in riemannian manifold   
                        sess.run(model.update_big_omega_riemann)
                        # Now that the score is updated, compute the new value for running Fisher
                        sess.run(model.set_running_fisher)
                        # Store the current value of the weights
                        sess.run(model.weights_delta_old_grouped)
                        # Reset the delta_L
                        sess.run([model.reset_small_omega])

                    _, _, _, _, loss = sess.run([model.set_tmp_fisher, model.weights_old_ops_grouped, 
                        model.train, model.update_small_omega, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'ER-Reservoir':
                    mem_filled_so_far = examples_seen_so_far if (examples_seen_so_far < episodic_mem_size) else episodic_mem_size
                    if mem_filled_so_far < args.eps_mem_batch:
                        er_mem_indices = np.arange(mem_filled_so_far)
                    else:
                        er_mem_indices = np.random.choice(mem_filled_so_far, args.eps_mem_batch, replace=False)
                    np.random.shuffle(er_mem_indices)
                    er_train_x_batch = np.concatenate((episodic_images[er_mem_indices], train_x[offset:offset+residual]), axis=0)
                    er_train_y_batch = np.concatenate((episodic_labels[er_mem_indices], train_y[offset:offset+residual]), axis=0)
                    labels_in_the_batch = np.unique(np.nonzero(er_train_y_batch)[1])
                    logit_mask[:] = 0
                    for tt in range(task+1):
                        if any(c_lab == t_lab for t_lab in task_labels[tt] for c_lab in labels_in_the_batch):
                            logit_mask[task_labels[tt]] = 1.0
                    feed_dict = {model.x: er_train_x_batch, model.y_: er_train_y_batch,
                        model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                                 model.train_phase: True, model.learning_rate: args.learning_rate}
                    feed_dict[model.output_mask] = logit_mask
                    _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)

                    # Reservoir update
                    for er_x, er_y_ in zip(train_x[offset:offset+residual], train_y[offset:offset+residual]):
                        update_reservior(er_x, er_y_, episodic_images, episodic_labels, episodic_mem_size, examples_seen_so_far)
                        examples_seen_so_far += 1

                elif model.imp_method == 'ER-Ringbuffer':
                    # Sample Bn U Bm
                    mem_filled_so_far = episodic_filled_counter if (episodic_filled_counter <= episodic_mem_size) else episodic_mem_size
                    er_mem_indices = np.arange(mem_filled_so_far) if (mem_filled_so_far <= args.eps_mem_batch) else np.random.choice(mem_filled_so_far, args.eps_mem_batch, replace=False)
                    np.random.shuffle(er_mem_indices)
                    er_train_x_batch = np.concatenate((episodic_images[er_mem_indices], train_x[offset:offset+residual]), axis=0) # TODO: Check if for task 0 the first arg is empty
                    er_train_y_batch = np.concatenate((episodic_labels[er_mem_indices], train_y[offset:offset+residual]), axis=0)
                    # Set the logit masks
                    nd_logit_mask[:] = 0
                    for tt in range(task+1):
                        nd_logit_mask[tt][task_labels[tt]] = 1.0
                    logit_mask_dict = {m_t: i_t for (m_t, i_t) in zip(model.output_mask, nd_logit_mask)}
                    feed_dict = {model.x: er_train_x_batch, model.y_: er_train_y_batch,
                        model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                                 model.train_phase: True, model.learning_rate: args.learning_rate}
                    feed_dict.update(logit_mask_dict)
                    feed_dict[model.mem_batch_size] = float(er_train_x_batch.shape[0])
                    _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)

                    # Put the batch in the FIFO ring buffer
                    update_fifo_buffer(train_x[offset:offset+residual], train_y[offset:offset+residual], episodic_images, episodic_labels,
                                       task_labels[task], args.mem_size, count_cls, episodic_filled_counter)

                elif model.imp_method == 'ER-SUBSPACE':
                    # Zero out all the grads
                    sess.run([model.reset_er_subspace_grads])
                    if task > 0:
                        # Randomly pick a task to replay
                        tt = np.squeeze(np.random.choice(np.arange(task), 1, replace=False))
                        mem_offset = tt*args.mem_size*classes_per_task 
                        er_mem_indices = np.arange(mem_offset, mem_offset+args.mem_size*classes_per_task)
                        np.random.shuffle(er_mem_indices)
                        er_train_x_batch = episodic_images[er_mem_indices]
                        er_train_y_batch = episodic_labels[er_mem_indices]
                        feed_dict = {model.x: er_train_x_batch, model.y_: er_train_y_batch,
                                     model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                                     model.train_phase: True, model.task_id: task+1, model.learning_rate: args.learning_rate}
                        logit_mask[:] = 0
                        logit_mask[task_labels[tt]] = 1.0
                        feed_dict[model.output_mask] = logit_mask
                        feed_dict[model.subspace_proj] = proj_matrices[tt]
                        sess.run(model.accum_er_subspace_grads, feed_dict=feed_dict)
                    # Train on the current task
                    feed_dict = {model.x: train_x[offset:offset+residual], model.y_: train_y[offset:offset+residual],
                                model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                                 model.train_phase: True, model.task_id: task+1, model.learning_rate: args.learning_rate}
                    logit_mask[:] = 0
                    logit_mask[task_labels[task]] = 1.0
                    feed_dict[model.output_mask] = logit_mask
                    feed_dict[model.subspace_proj] = proj_matrices[task]
                    if args.maintain_orthogonality:
                        if SVB:
                            _, _, loss = sess.run([model.train_er_subspace, model.accum_er_subspace_grads, model.reg_loss], feed_dict=feed_dict)
                            # Every few iterations bound the singular values
                            if iters % 20 == 0:
                                sess.run(model.update_weights_svb)
                        else:
                            _, loss = sess.run([model.accum_er_subspace_grads, model.reg_loss], feed_dict=feed_dict)
                            sess.run(model.train_stiefel, feed_dict={model.learning_rate: args.learning_rate})
                    else:
                        _, _, loss = sess.run([model.train_er_subspace, model.accum_er_subspace_grads, model.reg_loss], feed_dict=feed_dict)
                        
                    # Put the batch in the FIFO ring buffer
                    update_fifo_buffer(train_x[offset:offset+residual], train_y[offset:offset+residual], episodic_images, episodic_labels,
                                       task_labels[task], args.mem_size, count_cls, episodic_filled_counter)
               
                elif model.imp_method == 'ER-SUBSPACE-GP':
                    # Zero out all the grads
                    sess.run([model.reset_er_subspace_gp_grads])
                    feed_dict = {model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                                model.task_id: task+1, model.learning_rate: args.learning_rate, model.train_phase: True}
                    if task > 0:
                        # Randomly pick a task to replay
                        tt = np.squeeze(np.random.choice(np.arange(task), 1, replace=False))
                        mem_offset = tt*args.mem_size*classes_per_task 
                        er_mem_indices = np.arange(mem_offset, mem_offset+args.mem_size*classes_per_task)
                        np.random.shuffle(er_mem_indices)
                        er_train_x_batch = episodic_images[er_mem_indices]
                        er_train_y_batch = episodic_labels[er_mem_indices]
                        logit_mask[:] = 0
                        logit_mask[task_labels[tt]] = 1.0
                        feed_dict[model.output_mask] = logit_mask
                        feed_dict[model.x] = er_train_x_batch
                        feed_dict[model.y_] = er_train_y_batch
                        # Compute the gradient in the \perp space 
                        #feed_dict[model.subspace_proj] = np.eye(proj_matrices[tt].shape[0]) - proj_matrices[tt]
                        #sess.run(model.store_ref_grads, feed_dict=feed_dict)
                        # Compute the gradient in P space and store the gradient
                        feed_dict[model.subspace_proj] = proj_matrices[tt]
                        sess.run(model.accum_er_subspace_grads, feed_dict=feed_dict)
                    # Train on the current task
                    logit_mask[:] = 0
                    logit_mask[task_labels[task]] = 1.0
                    feed_dict[model.output_mask] = logit_mask
                    if task == 0:
                        feed_dict[model.x] = train_x[offset:offset+residual]
                        feed_dict[model.y_] = train_y[offset:offset+residual]
                    else:
                        # Sample Bn U Bm
                        mem_filled_so_far = episodic_filled_counter if (episodic_filled_counter <= episodic_mem_size) else episodic_mem_size
                        er_mem_indices = np.arange(mem_filled_so_far) if (mem_filled_so_far <= args.eps_mem_batch) else np.random.choice(mem_filled_so_far, args.eps_mem_batch, replace=False)
                        np.random.shuffle(er_mem_indices)
                        er_train_x_batch = np.concatenate((episodic_images[er_mem_indices], train_x[offset:offset+residual]), axis=0) # TODO: Check if for task 0 the first arg is empty
                        er_train_y_batch = np.concatenate((episodic_labels[er_mem_indices], train_y[offset:offset+residual]), axis=0)
                        feed_dict[model.x] = er_train_x_batch
                        feed_dict[model.y_] = er_train_y_batch
                    # Compute the gradient in the \perp space 
                    feed_dict[model.subspace_proj] = np.eye(proj_matrices[tt].shape[0]) - proj_matrices[task]
                    sess.run(model.store_ref_grads, feed_dict=feed_dict)
                    # Compute the gradient in P space and store the gradient
                    feed_dict[model.x] = train_x[offset:offset+residual]
                    feed_dict[model.y_] = train_y[offset:offset+residual]
                    feed_dict[model.subspace_proj] = proj_matrices[task]
                    _, loss = sess.run([model.train_er_gp, model.gp_total_loss], feed_dict=feed_dict)
                    
                    # Put the batch in the FIFO ring buffer
                    update_fifo_buffer(train_x[offset:offset+residual], train_y[offset:offset+residual], episodic_images, episodic_labels,
                                       task_labels[task], args.mem_size, count_cls, episodic_filled_counter)

                if (iters % 100 == 0):
                    print('Step {:d} {:.3f}'.format(iters, loss))
                    #print('Step {:d}\t CE: {:.3f}\t Reg: {:.9f}\t TL: {:.3f}'.format(iters, entropy, reg, loss))
                    #print('Step {:d}\t Reg: {:.9f}\t TL: {:.3f}'.format(iters, reg, loss))

                if (math.isnan(loss)):
                    print('ERROR: NaNs NaNs NaNs!!!')
                    sys.exit(0)

            print('\t\t\t\tTraining for Task%d done!'%(task))
           
            if model.imp_method == 'SUBSPACE-PROJ' and GRAD_CHECK:
                # TODO: Compute the average gradient of the task at \theta^*: Could be done as running average (no need for extra passes?)
                bbatch_size = 100
                grad_sum = []
                for iiters in range(train_x.shape[0] // bbatch_size):
                    offset = iiters * bbatch_size
                    feed_dict = {model.x: train_x[offset:offset+bbatch_size], model.y_: train_y[offset:offset+bbatch_size], 
                                 model.keep_prob: 1.0, model.train_phase: False, model.learning_rate: args.learning_rate}
                    nd_logit_mask[:] = 0
                    nd_logit_mask[task][task_labels[task]] = 1.0
                    logit_mask_dict = {m_t: i_t for (m_t, i_t) in zip(model.output_mask, nd_logit_mask)}
                    feed_dict.update(logit_mask_dict)
                    #feed_dict[model.subspace_proj] = proj_matrices[task]
                    projection_dict = {proj: proj_matrices[proj.get_shape()[0]][task] for proj in model.subspace_proj}
                    feed_dict.update(projection_dict)
                    feed_dict[model.mem_batch_size] = residual
                    grad_vars, train_vars = sess.run([model.reg_gradients_vars, model.trainable_vars], feed_dict=feed_dict)
                    for v in range(len(train_vars)):
                        if iiters == 0:
                            grad_sum.append(grad_vars[v][0])
                        else:
                            grad_sum[v] += (grad_vars[v][0] - grad_sum[v])/ iiters

                prev_task_grads.append(grad_sum)


            if use_episodic_memory:
                episodic_filled_counter += args.mem_size * classes_per_task

            if model.imp_method == 'A-GEM':
                if COUNT_VIOLATONS:
                    violation_count[task] = vc
                    print('Task {}: Violation Count: {}'.format(task, violation_count))
                    sess.run(model.reset_violation_count, feed_dict=feed_dict)

            # Compute the inter-task updates, Fisher/ importance scores etc
            # Don't calculate the task updates for the last task
            if (task < (len(task_labels) - 1)) or MEASURE_PERF_ON_EPS_MEMORY:
                model.task_updates(sess, task, task_train_images, task_labels[task]) # TODO: For MAS, should the gradients be for current task or all the previous tasks
                print('\t\t\t\tTask updates after Task%d done!'%(task))

            if args.train_single_epoch and not args.cross_validate_mode: 
                fbatch = test_task_sequence(model, sess, datasets[0]['test'], task_labels, task, projection_matrices=proj_matrices)
                print('Task: {}, Acc: {}'.format(task, fbatch))
                ftask.append(fbatch)
                ftask = np.array(ftask)
                if model.imp_method == 'PNN':
                    pnn_train_phase[:] = False
                    pnn_train_phase[task] = True
                    pnn_logit_mask[:] = 0
                    pnn_logit_mask[task][task_labels[task]] = 1.0
            else:
                if MEASURE_PERF_ON_EPS_MEMORY:
                    eps_mem = {
                            'images': episodic_images, 
                            'labels': episodic_labels,
                            }
                    # Measure perf on episodic memory
                    ftask = test_task_sequence(model, sess, eps_mem, task_labels, task, classes_per_task=classes_per_task, projection_matrices=proj_matrices)
                else:
                    # List to store accuracy for all the tasks for the current trained model
                    ftask = test_task_sequence(model, sess, datasets[0]['test'], task_labels, task, projection_matrices=proj_matrices)
                    print('Task: {}, Acc: {}'.format(task, ftask))
           
            # Store the accuracies computed at task T in a list
            evals.append(ftask)

            # Reset the optimizer
            model.reset_optimizer(sess)

            #-> End for loop task

        runs.append(np.array(evals))
        # End for loop runid

    runs = np.array(runs)

    return runs, task_labels_dataset

def test_task_sequence(model, sess, test_data, test_tasks, task, classes_per_task=0, projection_matrices=None):
    """
    Snapshot the current performance
    """
    if TIME_MY_METHOD:
        # Only compute the training time
        return np.zeros(model.num_tasks)

    final_acc = np.zeros(model.num_tasks)
    if model.imp_method in {'PNN', 'A-GEM', 'ER-Ringbuffer'}:
        logit_mask = np.zeros([model.num_tasks, TOTAL_CLASSES])
    else:
        logit_mask = np.zeros(TOTAL_CLASSES)

    if MEASURE_PERF_ON_EPS_MEMORY:
        for tt, labels in enumerate(test_tasks):
            # Multi-head evaluation setting
            logit_mask[:] = 0
            logit_mask[labels] = 1.0
            mem_offset = tt*SAMPLES_PER_CLASS*classes_per_task
            feed_dict = {model.x: test_data['images'][mem_offset:mem_offset+SAMPLES_PER_CLASS*classes_per_task], 
                    model.y_: test_data['labels'][mem_offset:mem_offset+SAMPLES_PER_CLASS*classes_per_task], model.keep_prob: 1.0, model.train_phase: False, model.output_mask: logit_mask}
            acc = model.accuracy.eval(feed_dict = feed_dict)
            final_acc[tt] = acc
        return final_acc

    for tt, labels in enumerate(test_tasks):

        if not MULTI_TASK:
            if tt > task:
                return final_acc

        task_test_images, task_test_labels = load_task_specific_data(test_data, labels)
        if model.imp_method == 'PNN':
            pnn_train_phase = np.array(np.zeros(model.num_tasks), dtype=np.bool)
            logit_mask[:] = 0
            logit_mask[tt][labels] = 1.0
            feed_dict = {model.x: task_test_images, 
                    model.y_[tt]: task_test_labels, model.keep_prob: 1.0}
            train_phase_dict = {m_t: i_t for (m_t, i_t) in zip(model.train_phase, pnn_train_phase)}
            logit_mask_dict = {m_t: i_t for (m_t, i_t) in zip(model.output_mask, logit_mask)}
            feed_dict.update(train_phase_dict)
            feed_dict.update(logit_mask_dict)
            acc = model.accuracy[tt].eval(feed_dict = feed_dict)

        elif model.imp_method in {'A-GEM', 'ER-Ringbuffer'}:
            logit_mask[:] = 0
            logit_mask[tt][labels] = 1.0
            feed_dict = {model.x: task_test_images, 
                    model.y_: task_test_labels, model.keep_prob: 1.0, model.train_phase: False}
            logit_mask_dict = {m_t: i_t for (m_t, i_t) in zip(model.output_mask, logit_mask)}
            feed_dict.update(logit_mask_dict)
            #if model.imp_method in {'SUBSPACE-PROJ', 'ER-SUBSPACE', 'PROJ-SUBSPACE-GP'}:
            if False:
                feed_dict[model.subspace_proj] = projection_matrices[tt]
                #feed_dict[model.subspace_proj] = np.eye(projection_matrices[tt].shape[0])
                #projection_dict = {proj: projection_matrices[proj.get_shape()[0]][tt] for proj in model.subspace_proj}
                #feed_dict.update(projection_dict)
            acc = model.accuracy[tt].eval(feed_dict = feed_dict)

        else:
            logit_mask[:] = 0
            logit_mask[labels] = 1.0
            #for ttt in range(task+1):
            #    logit_mask[test_tasks[ttt]] = 1.0
            feed_dict = {model.x: task_test_images, 
                    model.y_: task_test_labels, model.keep_prob: 1.0, model.train_phase: False, model.output_mask: logit_mask}
            if model.imp_method in {'SUBSPACE-PROJ', 'ER-SUBSPACE', 'PROJ-SUBSPACE-GP', 'ER-SUBSPACE-GP'}:
                feed_dict[model.subspace_proj] = projection_matrices[tt]

            acc = model.accuracy.eval(feed_dict = feed_dict)

        final_acc[tt] = acc

    return final_acc

def main():
    """
    Create the model and start the training
    """

    # Get the CL arguments
    args = get_arguments()

    # Check if the network architecture is valid
    if args.arch not in VALID_ARCHS:
        raise ValueError("Network architecture %s is not supported!"%(args.arch))

    # Check if the method to compute importance is valid
    if args.imp_method not in MODELS:
        raise ValueError("Importance measure %s is undefined!"%(args.imp_method))
    
    # Check if the optimizer is valid
    if args.optim not in VALID_OPTIMS:
        raise ValueError("Optimizer %s is undefined!"%(args.optim))

    # Create log directories to store the results
    if not os.path.exists(args.log_dir):
        print('Log directory %s created!'%(args.log_dir))
        os.makedirs(args.log_dir)

    # Generate the experiment key and store the meta data in a file
    exper_meta_data = {'ARCH': args.arch,
            'DATASET': 'SPLIT_miniImageNET',
            'NUM_RUNS': args.num_runs,
            'TRAIN_SINGLE_EPOCH': args.train_single_epoch, 
            'IMP_METHOD': args.imp_method, 
            'SYNAP_STGTH': args.synap_stgth,
            'FISHER_EMA_DECAY': args.fisher_ema_decay,
            'FISHER_UPDATE_AFTER': args.fisher_update_after,
            'OPTIM': args.optim, 
            'LR': args.learning_rate, 
            'BATCH_SIZE': args.batch_size, 
            'MEM_SIZE': args.mem_size}
    experiment_id = "SPLIT_miniImageNET_META_%s_%s_%r_%s-"%(args.imp_method, str(args.synap_stgth).replace('.', '_'),
            str(args.batch_size), str(args.mem_size)) + datetime.datetime.now().strftime("%y-%m-%d-%H-%M")
    snapshot_experiment_meta_data(args.log_dir, experiment_id, exper_meta_data)

    # Get the task labels from the total number of tasks and full label space
    if args.online_cross_val:
        num_tasks = K_FOR_CROSS_VAL
    else:
        num_tasks = args.num_tasks - K_FOR_CROSS_VAL

    # Load the split miniImagenet dataset
    data_labs = [np.arange(TOTAL_CLASSES)]
    datasets = construct_split_miniImagenet(data_labs, args.data_file)

    # Variables to store the accuracies and standard deviations of the experiment
    acc_mean = dict()
    acc_std = dict()

    # Reset the default graph
    tf.reset_default_graph()
    graph  = tf.Graph()
    with graph.as_default():

        # Set the random seed
        tf.set_random_seed(args.random_seed)
        np.random.seed(args.random_seed)
        random.seed(args.random_seed)

        # Define Input and Output of the model
        x = tf.placeholder(tf.float32, shape=[None, IMG_HEIGHT, IMG_WIDTH, IMG_CHANNELS])
        learning_rate = tf.placeholder(dtype=tf.float32, shape=())
        if args.imp_method == 'PNN':
            y_ = []
            for i in range(num_tasks):
                y_.append(tf.placeholder(tf.float32, shape=[None, TOTAL_CLASSES]))
        else:
            y_ = tf.placeholder(tf.float32, shape=[None, TOTAL_CLASSES])

        # Define the optimizer
        if args.optim == 'ADAM':
            opt = tf.train.AdamOptimizer(learning_rate=learning_rate)

        elif args.optim == 'SGD':
            opt = tf.train.GradientDescentOptimizer(learning_rate=learning_rate)

        elif args.optim == 'MOMENTUM':
            #base_lr = tf.constant(args.learning_rate)
            #learning_rate = tf.scalar_mul(base_lr, tf.pow((1 - train_step / training_iters), OPT_POWER))
            opt = tf.train.MomentumOptimizer(learning_rate, OPT_MOMENTUM)

        # Create the Model/ contruct the graph
        model = Model(x, y_, num_tasks, opt, args.imp_method, args.synap_stgth, args.fisher_update_after, 
                args.fisher_ema_decay, learning_rate, network_arch=args.arch)

        # Set up tf session and initialize variables.
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True

        time_start = time.time()
        with tf.Session(config=config, graph=graph) as sess:
            runs, task_labels_dataset = train_task_sequence(model, sess, datasets, args)
            # Close the session
            sess.close()
        time_end = time.time()
        time_spent = time_end - time_start

    # Store all the results in one dictionary to process later
    exper_acc = dict(mean=runs)
    exper_labels = dict(labels=task_labels_dataset)

    # If cross-validation flag is enabled, store the stuff in a text file
    if args.cross_validate_mode:
        acc_mean, acc_std = average_acc_stats_across_runs(runs, model.imp_method)
        fgt_mean, fgt_std = average_fgt_stats_across_runs(runs, model.imp_method)
        cross_validate_dump_file = args.log_dir + '/' + 'SPLIT_miniImageNET_%s_%s'%(args.imp_method, args.optim) + '.txt'
        with open(cross_validate_dump_file, 'a') as f:
            if MULTI_TASK:
                f.write('HERDING: {} \t ARCH: {} \t LR:{} \t LAMBDA: {} \t ACC: {}\n'.format(args.arch, args.learning_rate, args.synap_stgth, acc_mean[-1,:].mean()))
            else:
                f.write('ORTHO:{} \t SVB:{}\t NUM_TASKS: {} \t MEM_SIZE: {} \t ARCH: {} \t LR:{} \t LAMBDA: {} \t SHARED_SUBSPACE:{}, \t ACC: {} (+-{})\t Fgt: {} (+-{})\t QR:{}\t Time: {}\n'.format(args.maintain_orthogonality, SVB, args.num_tasks, args.mem_size, args.arch, args.learning_rate,
                    args.synap_stgth, args.subspace_share_dims, acc_mean, acc_std, fgt_mean, fgt_std, QR, str(time_spent)))

    # Store the experiment output to a file
    snapshot_experiment_eval(args.log_dir, experiment_id, exper_acc)
    snapshot_task_labels(args.log_dir, experiment_id, exper_labels)

if __name__ == '__main__':
    main()
