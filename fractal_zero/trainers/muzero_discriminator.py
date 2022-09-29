import gym
import torch
import torch.nn.functional as F
import numpy as np

from typing import Callable, Union
from fractal_zero.data.expert_dataset import ExpertDataset
from fractal_zero.models.joint_model import JointModel
from fractal_zero.search.fmc import FMC

from fractal_zero.vectorized_environment import VectorizedDynamicsModelEnvironment, VectorizedEnvironment, load_environment


class FMZGModel(VectorizedEnvironment):
    action_space: gym.Space

    # TODO: actual docstring:
        # the representation model takes the raw obesrvation and puts it into an embedding. this representation model MAY be
        #   a transformer or some sort of recurrent model, in the future.
        # the dynamics model is given the state and action embeddings and returns a new state embedding
        # the discriminator model is given the embedding of the new state (from the dynamics model) and returns 
        #   a float reward between 0 and 1 (0=agent, 1=expert)

    def __init__(
        self, 
        representation_model: torch.nn.Module, 
        dynamics_model: torch.nn.Module,
        discriminator_model: torch.nn.Module,
        num_walkers: int,
        action_vectorizer: Callable,
    ):
        self.representation = representation_model
        self.dynamics = dynamics_model
        self.discriminator = discriminator_model

        self.n = num_walkers

        # TODO: refac?
        self.action_vectorizer = action_vectorizer

        self.initial_states = None
        self.states = None
        self.current_reward = None
        self.dones = None

    def _check_states(self):
        if self.initial_states is None:
            raise ValueError("Must call \"set_all_states\" before stepping.")

    def batch_reset(self):
        # NOTE: does nothing...?

        self._check_states()
        return self.states

    def set_all_states(self, observation):
        if isinstance(observation, np.ndarray):
            observation = torch.tensor(observation, dtype=float)

        self.representation.eval()
        self.initial_states = self.representation.forward(observation.float())

        # duplicate initial state representation to all walkers
        self.states = torch.zeros((self.n, *self.initial_states.shape))
        self.states[:] = self.initial_states

    def batch_step(self, embedded_actions):
        self._check_states()

        self.dynamics.eval()
        self.discriminator.eval()

        # update to new state
        x = torch.cat((self.states.float(), embedded_actions.float()), dim=-1)
        self.states = self.dynamics.forward(x)

        self.current_reward = self.discriminator.forward(x)  # NOTE: `x` IS THE PREVIOUS STATE!
        self.dones = torch.zeros(x.shape[0], dtype=bool)

        infos = None
        return self.states, self.current_reward, self.dones, infos
    
    def batched_action_space_sample(self):
        action_list = super().batched_action_space_sample()

        # TODO: more general vectorization of actions
        return torch.tensor(action_list, dtype=float).unsqueeze(-1)

    def clone(self, partners, clone_mask):
        self.states[clone_mask] = self.states[partners[clone_mask]]


class FractalMuZeroDiscriminatorTrainer:

    def __init__(
        self, 
        env: Union[str, gym.Env],
        model_environment: FMZGModel,
        expert_dataset: ExpertDataset,
        discriminator_optimizer: torch.optim.Optimizer,
    ):
        # TODO: vectorize the actual environment?
        self.actual_environment = load_environment(env)
        self.model_environment = model_environment

        self.discriminator_optimizer = discriminator_optimizer

        # TODO: refac somehow...?
        self.model_environment.action_space = self.actual_environment.action_space

        self.expert_dataset = expert_dataset

    @property
    def discriminator(self):
        return self.model_environment.discriminator

    @property
    def representation(self):
        return self.model_environment.representation

    def _get_agent_trajectory(self, max_steps: int):
        obs = self.actual_environment.reset()
        self.model_environment.set_all_states(obs)

        # TODO: maybe incorporate policy model? or maybe we can just use FMC to search?
        self.fmc = FMC(self.model_environment)

        lookahead_steps = 16

        observations = []
        actions = []

        for _ in range(max_steps):
            self.fmc.reset()

            observations.append(torch.tensor(obs, dtype=float))

            action = self.fmc.simulate(lookahead_steps)
            action = self.model_environment.action_vectorizer(action)

            actions.append(action)

            obs, reward, done, info = self.actual_environment.step(action)
            self.model_environment.set_all_states(obs)

            if done:
                break

        x = torch.stack(observations)
        y = torch.tensor(actions)

        return x, y

    def _get_expert_batch(self):
        # TODO
        raise NotImplementedError

    def _discriminator_train_step(self):
        # TODO
        raise NotImplementedError

    def train_step(self, max_steps: int):
        self.discriminator.train()
        self.representation.train()
        self.discriminator_optimizer.zero_grad()

        # TODO: simplify this, lots of copies!
        # get batch
        agent_x, agent_y = self._get_agent_trajectory(max_steps)
        expert_x, expert_y = self.expert_dataset.sample_trajectory(max_steps)

        # get hidden representation of the observations as states
        agent_states = self.representation.forward(agent_x.float())
        expert_states = self.representation.forward(expert_x.float())

        # add the hidden representations with the action embeddings (TODO: de-duplciate this code, it exists
        # within the FMZG model too.)
        agent_samples = torch.cat((agent_states, agent_y.unsqueeze(-1)), dim=-1)
        expert_samples = torch.cat((expert_states, expert_y.unsqueeze(-1)), dim=-1)
        x = torch.cat((agent_samples, expert_samples)).float()
        t = torch.tensor(([0] * agent_samples.shape[0]) + [1] * expert_samples.shape[0]).float()

        discriminator_confusions = self.discriminator.forward(x).squeeze(-1)
        loss = F.mse_loss(discriminator_confusions, t)
        
        loss.backward()
        self.discriminator_optimizer.step()

        return loss.item()