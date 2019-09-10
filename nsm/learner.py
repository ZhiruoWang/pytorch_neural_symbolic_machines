import ctypes
import heapq
import json
import os
import random
import time
from itertools import chain
from pathlib import Path
from typing import List, Dict

import numpy as np
import sys

import torch.multiprocessing as torch_mp
import multiprocessing

from pytorch_pretrained_bert import BertAdam

from nsm import nn_util
from nsm.parser_module import get_parser_agent_by_name
from nsm.parser_module.agent import PGAgent
from nsm.consistency_utils import ConsistencyModel, QuestionSimilarityModel
from nsm.retrainer import Retrainer, load_nearest_neighbors
from nsm.evaluator import Evaluation
from nsm.program_cache import SharedProgramCache

import torch
from tensorboardX import SummaryWriter

from nsm.dist_util import STOP_SIGNAL
from nsm.sketch.sketch_predictor import SketchPredictor, SketchManagerTrainer


class Learner(torch_mp.Process):
    def __init__(self, config: Dict, device: torch.device, shared_program_cache: SharedProgramCache = None):
        super(Learner, self).__init__(daemon=True)

        self.train_queue = multiprocessing.Queue()
        self.checkpoint_queue = multiprocessing.Queue()
        self.config = config
        self.device = device
        self.actor_message_vars = []
        self.current_model_path = None
        self.shared_program_cache = shared_program_cache

        self.actor_num = 0

    def run(self):
        # initialize cuda context
        self.device = torch.device(self.device)

        if 'cuda' in self.device.type:
            torch.cuda.set_device(self.device)

        # seed the random number generators
        nn_util.init_random_seed(self.config['seed'], self.device)

        agent_name = self.config.get('parser', 'vanilla')
        self.agent = get_parser_agent_by_name(agent_name).build(self.config).to(self.device).train()

        self.train()

    def train(self):
        model = self.agent
        config = self.config
        work_dir = Path(config['work_dir'])
        train_iter = 0
        save_every_niter = config['save_every_niter']
        entropy_reg_weight = config['entropy_reg_weight']
        summary_writer = SummaryWriter(os.path.join(config['work_dir'], 'tb_log/train'))
        max_train_step = config['max_train_step']
        save_program_cache_niter = config.get('save_program_cache_niter', 0)
        freeze_bert_for_niter = config.get('freeze_bert_niter', 0)
        gradient_accumulation_niter = config.get('gradient_accumulation_niter', 1)

        bert_params = [
            (p_name, p)
            for (p_name, p) in model.named_parameters()
            if 'bert_model' in p_name and p.requires_grad
        ]

        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        bert_grouped_parameters = [
            {'params': [p for n, p in bert_params if not any(nd in n for nd in no_decay)],
             'weight_decay': 0.01},
            {'params': [p for n, p in bert_params if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

        bert_optimizer = BertAdam(
            bert_grouped_parameters,
            lr=self.config['bert_learning_rate'],
            warmup=0.1,
            t_total=max_train_step)

        other_params = [
            p
            for n, p
            in model.named_parameters()
            if 'bert_model' not in n and p.requires_grad
        ]

        other_optimizer = torch.optim.Adam(other_params, lr=0.001)

        cum_loss = cum_examples = 0.
        t1 = time.time()

        while train_iter < max_train_step:
            train_iter += 1
            other_optimizer.zero_grad()
            bert_optimizer.zero_grad()

            train_samples, samples_info = self.train_queue.get()
            try:
                queue_size = self.train_queue.qsize()
                # print(f'[Learner] train_iter={train_iter} train queue size={queue_size}', file=sys.stderr)
                summary_writer.add_scalar('train_queue_size', queue_size, train_iter)
            except NotImplementedError:
                pass

            train_trajectories = [sample.trajectory for sample in train_samples]

            # (batch_size)
            batch_log_prob, meta_info = self.agent(train_trajectories, return_info=True)

            train_sample_weights = batch_log_prob.new_tensor([s.weight for s in train_samples])
            batch_log_prob = batch_log_prob * train_sample_weights

            loss = -batch_log_prob.mean()

            if gradient_accumulation_niter > 1:
                loss /= gradient_accumulation_niter

            summary_writer.add_scalar('parser_loss', loss.item(), train_iter)
            # loss = -batch_log_prob.sum() / max_batch_size

            if entropy_reg_weight != 0.:
                entropy = entropy.mean()
                ent_reg_loss = - entropy_reg_weight * entropy  # maximize entropy
                loss = loss + ent_reg_loss

                summary_writer.add_scalar('entropy', entropy.item(), train_iter)
                summary_writer.add_scalar('entropy_reg_loss', ent_reg_loss.item(), train_iter)

            loss.backward()
            loss_val = loss.item()

            # clip gradient
            grad_norm = torch.nn.utils.clip_grad_norm_(other_params, 5.)

            if train_iter % gradient_accumulation_niter == 0:
                other_optimizer.step()

                if train_iter > freeze_bert_for_niter:
                    bert_optimizer.step()
                elif train_iter == freeze_bert_for_niter:
                    print(f'[Learner] train_iter={train_iter} reset Adam optimizer and start fine-tuning BERT')
                    other_optimizer = torch.optim.Adam(other_params, lr=0.001)

            # print(f'[Learner] train_iter={train_iter} loss={loss_val}', file=sys.stderr)
            del loss
            if entropy_reg_weight != 0.: del entropy

            if 'clip_frac' in samples_info:
                summary_writer.add_scalar('sample_clip_frac', samples_info['clip_frac'], train_iter)

            cum_loss += loss_val * len(train_samples)
            cum_examples += len(train_samples)

            if train_iter % save_every_niter == 0:
                print(f'[Learner] train_iter={train_iter} avg. loss={cum_loss / cum_examples}, '
                      f'{cum_examples} examples ({cum_examples / (time.time() - t1)} examples/s)', file=sys.stderr)
                cum_loss = cum_examples = 0.
                t1 = time.time()

                self.update_model_to_actors(train_iter)

                # log stats of the program cache
                program_cache_stat = self.shared_program_cache.stat()
                summary_writer.add_scalar(
                    'avg_num_programs_in_cache',
                    program_cache_stat['num_entries'] / program_cache_stat['num_envs'],
                    train_iter
                )
                summary_writer.add_scalar(
                    'num_programs_in_cache',
                    program_cache_stat['num_entries'],
                    train_iter
                )
            else:
                self.push_new_model(self.current_model_path)

            if save_program_cache_niter > 0 and train_iter % save_program_cache_niter == 0:
                program_cache_file = work_dir / 'log' / f'program_cache.iter{train_iter}.json'
                program_cache = self.shared_program_cache.all_programs()
                json.dump(
                    program_cache,
                    program_cache_file.open('w'),
                    indent=2
                )
        # for i in range(self.actor_num):
        #     self.checkpoint_queue.put(STOP_SIGNAL)
        # self.eval_msg_val.value = STOP_SIGNAL.encode()

    def update_model_to_actors(self, train_iter):
        t1 = time.time()
        model_state = self.agent.state_dict()
        model_save_path = os.path.join(self.config['work_dir'], 'agent_state.iter%d.bin' % train_iter)
        torch.save(model_state, model_save_path)

        self.push_new_model(model_save_path)
        print(f'[Learner] pushed model [{model_save_path}] (took {time.time() - t1}s)', file=sys.stderr)

        if self.current_model_path:
            os.remove(self.current_model_path)
        self.current_model_path = model_save_path

    def push_new_model(self, model_path):
        self.checkpoint_queue.put(model_path)
        if model_path:
            self.eval_msg_val.value = model_path.encode()

    def register_actor(self, actor):
        actor.checkpoint_queue = self.checkpoint_queue
        actor.train_queue = self.train_queue
        self.actor_num += 1

    def register_evaluator(self, evaluator):
        msg_var = multiprocessing.Array(ctypes.c_char, 4096)
        self.eval_msg_val = msg_var
        evaluator.message_var = msg_var
