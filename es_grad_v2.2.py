from copy import deepcopy
import argparse

import torch
import torch.nn as nn
import torch.multiprocessing as mp
import torch.nn.functional as F
import cma
import pandas as pd

import gym
import gym.spaces
import numpy as np
from tqdm import tqdm

from ES import sepCMAES, sepCEM
from models import RLNN
from random_process import GaussianNoise
from memory import Memory
from util import *

USE_CUDA = torch.cuda.is_available()
if USE_CUDA:
    FloatTensor = torch.cuda.FloatTensor
else:
    FloatTensor = torch.FloatTensor


class Actor(RLNN):

    def __init__(self, state_dim, action_dim, max_action, args):
        super(Actor, self).__init__(state_dim, action_dim, max_action)

        self.l1 = nn.Linear(state_dim, 400)
        self.l2 = nn.Linear(400, 300)
        self.l3 = nn.Linear(300, action_dim)

        if args.layer_norm:
            self.n1 = nn.LayerNorm(400)
            self.n2 = nn.LayerNorm(300)
        self.layer_norm = args.layer_norm

        self.optimizer = torch.optim.Adam(self.parameters(), lr=args.actor_lr)
        self.tau = args.tau
        self.discount = args.discount
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.max_action = max_action

    def forward(self, x):

        if not self.layer_norm:
            x = F.relu(self.l1(x))
            x = F.relu(self.l2(x))
            x = self.max_action * F.tanh(self.l3(x))

        else:
            x = F.relu(self.n1(self.l1(x)))
            x = F.relu(self.n2(self.l2(x)))
            x = self.max_action * F.tanh(self.l3(x))

        return x

    def update(self, memory, batch_size, critic, actor_t):

        # Sample replay buffer
        states, _, _, _, _ = memory.sample(batch_size)

        # Compute actor loss
        actor_loss = -critic(states, self(states)).mean()

        # Optimize the actor
        self.optimizer.zero_grad()
        actor_loss.backward()
        self.optimizer.step()

        # Update the frozen target models
        for param, target_param in zip(self.parameters(), actor_t.parameters()):
            target_param.data.copy_(
                self.tau * param.data + (1 - self.tau) * target_param.data)


def evaluate(actor, env, memory=None, n_episodes=1, random=False, noise=None, render=False):
    """
    Computes the score of an actor on a given number of runs
    """

    if not random:
        def policy(state):
            state = FloatTensor(state.reshape(-1))
            action = actor(state).cpu().data.numpy().flatten()

            if noise is not None:
                action += noise.sample()

            return np.clip(action, -max_action, max_action)

    else:
        def policy(state):
            return env.action_space.sample()

    scores = []
    steps = 0

    for _ in range(n_episodes):

        score = 0
        obs = deepcopy(env.reset())
        done = False

        while not done:

            # get next action and act
            action = policy(obs)
            n_obs, reward, done, _ = env.step(action)
            done_bool = 0 if steps + \
                1 == env._max_episode_steps else float(done)
            score += reward
            steps += 1

            # adding in memory
            if memory is not None:
                memory.add((obs, n_obs, action, reward, done_bool))
            obs = n_obs

            # render if needed
            if render:
                env.render()

            # reset when done
            if done:
                env.reset()

        scores.append(score)

    return np.mean(scores), steps


class Critic(RLNN):
    def __init__(self, state_dim, action_dim, max_action, args):
        super(Critic, self).__init__(state_dim, action_dim, 1)

        self.l1 = nn.Linear(state_dim + action_dim, 400)
        self.l2 = nn.Linear(400, 300)
        self.l3 = nn.Linear(300, 1)

        if args.layer_norm:
            self.n1 = nn.LayerNorm(400)
            self.n2 = nn.LayerNorm(300)
        self.layer_norm = args.layer_norm

        self.optimizer = torch.optim.Adam(self.parameters(), lr=args.critic_lr)
        self.tau = args.tau
        self.discount = args.discount
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.max_action = max_action

    def forward(self, x, u):

        if not self.layer_norm:
            x = F.relu(self.l1(torch.cat([x, u], 1)))
            x = F.relu(self.l2(x))
            x = self.l3(x)

        else:
            x = F.relu(self.n1(self.l1(torch.cat([x, u], 1))))
            x = F.relu(self.n2(self.l2(x)))
            x = self.l3(x)

        return x

    def update(self, memory, batch_size, actor_t, critic_t):

        # Sample replay buffer
        states, n_states, actions, rewards, dones = memory.sample(batch_size)

        # Q target = reward + discount * Q(next_state, pi(next_state))
        with torch.no_grad():
            target_Q = critic_t(n_states, actor_t(n_states))
            target_Q = rewards + (1 - dones) * self.discount * target_Q

        # Get current Q estimate
        current_Q = self(states, actions)

        # Compute critic loss
        critic_loss = nn.MSELoss()(current_Q, target_Q)

        # Optimize the critic
        self.optimizer.zero_grad()
        critic_loss.backward()
        self.optimizer.step()

        # Update the frozen target models
        for param, target_param in zip(self.parameters(), critic_t.parameters()):
            target_param.data.copy_(
                self.tau * param.data + (1 - self.tau) * target_param.data)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument('--mode', default='train', type=str,)
    parser.add_argument('--env', default='HalfCheetah-v2', type=str)
    parser.add_argument('--start_steps', default=10000, type=int)

    # DDPG parameters
    parser.add_argument('--actor_lr', default=0.001, type=float)
    parser.add_argument('--critic_lr', default=0.001, type=float)
    parser.add_argument('--batch_size', default=100, type=int)
    parser.add_argument('--discount', default=0.99, type=float)
    parser.add_argument('--reward_scale', default=1., type=float)
    parser.add_argument('--tau', default=0.005, type=float)
    parser.add_argument('--layer_norm', dest='layer_norm', action='store_true')

    # TD3 parameters
    parser.add_argument('--use_td3', dest='use_td3', action='store_true')
    parser.add_argument('--policy_noise', default=0.2, type=float)
    parser.add_argument('--noise_clip', default=0.5, type=float)
    parser.add_argument('--policy_freq', default=2, type=int)

    # Gaussian noise parameters
    parser.add_argument('--gauss_sigma', default=0.1, type=float)

    # OU process parameters
    parser.add_argument('--ou_noise', dest='ou_noise', action='store_true')
    parser.add_argument('--ou_theta', default=0.15, type=float)
    parser.add_argument('--ou_sigma', default=0.2, type=float)
    parser.add_argument('--ou_mu', default=0.0, type=float)

    # ES parameters
    parser.add_argument('--pop_size', default=10, type=int)
    parser.add_argument('--n_grad', default=1, type=int)
    parser.add_argument('--sigma_init', default=0.05, type=float)
    parser.add_argument('--damp', default=0.001, type=float)

    # Training parameters
    parser.add_argument('--n_episodes', default=1, type=int)
    parser.add_argument('--max_steps', default=1000000, type=int)
    parser.add_argument('--mem_size', default=1000000, type=int)

    # Testing parameters
    parser.add_argument('--filename', default="", type=str)
    parser.add_argument('--n_test', default=1, type=int)

    # misc
    parser.add_argument('--output', default='results', type=str)
    parser.add_argument('--period', default=5000, type=int)
    parser.add_argument('--debug', dest='debug', action='store_true')
    parser.add_argument('--seed', default=-1, type=int)
    parser.add_argument('--render', dest='render', action='store_true')

    args = parser.parse_args()
    args.output = get_output_folder(args.output, args.env)
    with open(args.output + "/parameters.txt", 'w') as file:
        for key, value in vars(args).items():
            file.write("{} = {}\n".format(key, value))

    # environment
    env = gym.make(args.env)
    state_dim = env.observation_space.shape[0]
    action_dim = env.action_space.shape[0]
    max_action = int(env.action_space.high[0])

    # memory
    memory = Memory(args.mem_size, state_dim, action_dim)

    # critic
    critic = Critic(state_dim, action_dim, max_action, args)
    # critic.load_model(
    #     "results/ddpg_layer/hc/HalfCheetah-v2-run1/1000000_steps", "critic")
    critic_t = Critic(state_dim, action_dim, max_action, args)
    critic_t.load_state_dict(critic.state_dict())

    # actor
    actors = [Actor(state_dim, action_dim, max_action, args)
              for _ in range(args.n_grad)]
    actors_t = [Actor(state_dim, action_dim, max_action, args)
                for _ in range(args.n_grad)]
    actor_ea = Actor(state_dim, action_dim, max_action, args)
    for i in range(args.n_grad):
        actors_t[i].load_state_dict(actors[i].state_dict())
    a_noise = GaussianNoise(action_dim, sigma=args.gauss_sigma)

    if USE_CUDA:
        critic.cuda()
        critic_t.cuda()
        for i in range(args.n_grad):
            actors[i].cuda()
            actors_t[i].cuda()

    # CEM
    es = sepCEM(actors[0].get_size(), sigma_init=args.sigma_init, damp=args.damp,
                pop_size=args.pop_size, antithetic=not args.pop_size % 2, parents=args.n_grad)

    # training
    total_steps = 0
    actor_steps = 0
    step_cpt = 0
    df = pd.DataFrame(columns=["total_steps", "average_score",
                               "average_score_rl", "average_score_ea", "best_score"])
    while total_steps < args.max_steps:

        fitness_rl = []
        fitness_ea = []
        rl_params = []
        ea_params = es.ask(args.pop_size)

        # udpate the rl actors and the critic
        if total_steps > args.start_steps:

            for i in range(args.n_grad):

                # actor update
                for _ in range(1):  # actor_steps):
                    actors[i].update(memory, args.batch_size,
                                     critic, actors_t[i])

                # critic update
                for _ in range(1):  # actor_steps // args.n_grad):
                    critic.update(memory, args.batch_size, actors[i], critic_t)

                # evaluate
                f, steps = evaluate(actors[i], env, memory=memory, n_episodes=args.n_episodes,
                                    render=args.render)
                actor_steps += steps
                rl_params.append(actors[i].get_params())
                fitness_rl.append(f)

            # print scores
            prRed('RL actor fitness:{}'.format(f))

        # evaluate all actors
        actor_steps = 0
        for params in ea_params:

            actor_ea.set_params(params)
            f, steps = evaluate(actor_ea, env, memory=memory, n_episodes=args.n_episodes,
                                render=args.render)
            actor_steps += steps
            fitness_ea.append(f)

            # print scores
            prLightPurple('EA actor fitness:{}'.format(f))

        # update step counts
        total_steps += actor_steps
        step_cpt += actor_steps

        # combine rl and ea and update ea
        fitness = np.array(fitness_ea + fitness_rl)
        params = ea_params if len(rl_params) == 0 else np.concatenate(
            (ea_params, rl_params), axis=0)
        es.tell(params, fitness)

        # save stuff
        if step_cpt >= args.period:

            df.to_pickle(args.output + "/log.pkl")
            res = {"total_steps": total_steps,
                   "average_score_ea": np.mean(fitness_ea),
                   "average_score_rl": np.mean(fitness_rl),
                   "average_score": np.mean(fitness),
                   "best_score": np.max(fitness)}
            os.makedirs(args.output + "/{}_steps".format(total_steps),
                        exist_ok=True)
            critic.save_model(
                args.output + "/{}_steps".format(total_steps), "critic")
            for i in range(args.n_grad):
                actors[i].save_model(
                    args.output + "/{}_steps".format(total_steps), "actor_{}".format(i))
            actor_ea.set_params(es.mu)
            actor_ea.save_model(
                args.output + "/{}_steps".format(total_steps), "actor_mu")
            df = df.append(res, ignore_index=True)
            step_cpt = 0
            print(res)

        print("Total steps", total_steps)
