import torch
import numpy as np
import networkx as nx

import wandb

import matplotlib.pyplot as plt
from fractal_zero.config import FractalZeroConfig

from fractal_zero.models.joint_model import JointModel
from fractal_zero.utils import mean_min_max_dict


@torch.no_grad()
def _relativize_vector(vector):
    std = vector.std()
    if std == 0:
        return torch.ones(len(vector))
    standard = (vector - vector.mean()) / std
    standard[standard > 0] = torch.log(1 + standard[standard > 0]) + 1
    standard[standard <= 0] = torch.exp(standard[standard <= 0])
    return standard


class FMC:
    """Fractal Monte Carlo is a collaborative cellular automata based tree search algorithm. This version is special, because instead of having a gym
    environment maintain the state for each walker during the search process, each walker's state is represented inside of a batched hidden
    state variable inside of a dynamics model. Basically, the dynamics model's hidden state is of shape (num_walkers, *embedding_shape).

    This is inspired by Muzero's technique to have a dynamics model be learned such that the tree search need not interact with the environment
    itself. With FMC, it is much more natural than with MCTS, mostly because of the cloning phase being contrastive. As an added benefit of this
    approach, it's natively vectorized so it can be put onto the GPU.
    """

    def __init__(
        self,
        config: FractalZeroConfig,
        verbose: bool = False,
    ):
        self.config = config

        self.model = config.joint_model

        self.verbose = verbose

    def set_state(self, state: torch.Tensor):
        # set the initial states for all walkers
        batched_initial_state = torch.zeros(
            (self.num_walkers, *state.shape), device=self.config.device
        )
        batched_initial_state[:] = state
        self.dynamics_model.set_state(batched_initial_state)

    @property
    def num_walkers(self) -> int:
        return self.config.num_walkers

    @property
    def device(self):
        return self.model.device

    @property
    def state(self):
        return self.dynamics_model.state

    @property
    def dynamics_model(self):
        return self.model.dynamics_model

    @property
    def prediction_model(self):
        return self.model.prediction_model

    @torch.no_grad()
    def _perturbate(self):
        """Advance the state of each walker."""

        self._assign_actions()
        self.rewards = self.dynamics_model.forward(self.actions)
        _, self.predicted_values = self.prediction_model.forward(self.state)

        self.reward_buffer[:, self.simulation_iteration] = self.rewards

    @torch.no_grad()
    def simulate(self, k: int, greedy_action: bool = True):
        """Run FMC for k iterations, returning the best action that was taken at the root/initial state."""

        self.k = k
        assert self.k > 0

        # TODO: explain all these variables
        # NOTE: they should exist on the CPU.
        self.reward_buffer = torch.zeros(
            size=(self.num_walkers, self.k, 1),
            dtype=float,
        )
        self.value_sum_buffer = torch.zeros(
            size=(self.num_walkers, 1),
            dtype=float,
        )
        self.visit_buffer = torch.zeros(
            size=(self.num_walkers, 1),
            dtype=int,
        )
        self.clone_receives = torch.zeros(
            size=(self.num_walkers, 1),
            dtype=int,
        )

        self.root_actions = None
        self.root_value_sum = 0
        self.root_visits = 0

        for self.simulation_iteration in range(self.k):
            self._perturbate()
            self._prepare_clone_variables()
            self._backpropagate_reward_buffer()
            self._execute_cloning()

        # sanity check
        assert self.state.shape == (
            self.num_walkers,
            self.dynamics_model.embedding_size,
        )

        # TODO: try to convert the root action distribution into a policy distribution? this may get hard in continuous action spaces. https://arxiv.org/pdf/1805.09613.pdf

        if self.config.use_wandb:
            wandb.log(
                {
                    **mean_min_max_dict("fmc/visit_buffer", self.visit_buffer.float()),
                    **mean_min_max_dict("fmc/value_sum_buffer", self.value_sum_buffer),
                    **mean_min_max_dict(
                        "fmc/average_value_buffer",
                        self.value_sum_buffer / self.visit_buffer.float(),
                    ),
                    **mean_min_max_dict(
                        "fmc/clone_receives", self.clone_receives.float()
                    ),
                },
                commit=False,
            )

        # TODO: experiment with these
        if greedy_action:
            return self._get_highest_value_action()
        return self._get_action_with_highest_clone_receives()

    @torch.no_grad()
    def _assign_actions(self):
        """Each walker picks an action to advance it's state."""

        # TODO: use the policy function for action selection.
        actions = []
        for _ in range(self.num_walkers):
            action = self.dynamics_model.action_space.sample()
            actions.append(action)
        self.actions = torch.tensor(actions, device=self.config.device).unsqueeze(-1)

        if self.root_actions is None:
            self.root_actions = self.actions.cpu().detach().clone()

    @torch.no_grad()
    def _assign_clone_partners(self):
        """For the cloning phase, walkers need a partner to determine if they should be sent as reinforcements to their partner's state."""

        choices = np.random.choice(np.arange(self.num_walkers), size=self.num_walkers)
        self.clone_partners = torch.tensor(choices, dtype=int)

    @torch.no_grad()
    def _calculate_distances(self):
        """For the cloning phase, we calculate the distances between each walker and their partner for balancing exploration."""

        self.distances = torch.linalg.norm(
            self.state - self.state[self.clone_partners], dim=1
        )

    @torch.no_grad()
    def _calculate_virtual_rewards(self):
        """For the cloning phase, we calculate a virtual reward that is the composite of each walker's distance to their partner weighted with
        their rewards. This is used to determine the probability to clone and is used to balance exploration and exploitation.

        Both the reward and distance vectors are "relativized". This keeps all of the values in each vector contextually scaled with each step.
        The authors of Fractal Monte Carlo claim this is a method of shaping a "universal reward function". Without relativization, the
        vectors may have drastically different ranges, causing more volatility in how many walkers are cloned at each step. If the reward or distance
        ranges were too high, it's likely no cloning would occur at all. If either were too small, then it's likely all walkers would be cloned.
        """

        rel_values = _relativize_vector(self.predicted_values).squeeze(-1)
        rel_distances = _relativize_vector(self.distances)
        self.virtual_rewards = (rel_values**self.config.balance) * rel_distances

        if self.config.use_wandb:
            wandb.log(
                {
                    **mean_min_max_dict("fmc/virtual_rewards", self.virtual_rewards),
                    **mean_min_max_dict("fmc/predicted_values", self.predicted_values),
                    **mean_min_max_dict("fmc/distances", self.distances),
                    **mean_min_max_dict("fmc/auxiliaries", self.rewards),
                },
                commit=False,
            )

    @torch.no_grad()
    def _determine_clone_mask(self):
        """The clone mask is based on the virtual rewards of each walker and their clone partner. If a walker is selected to clone, their
        state will be replaced with their partner's state.
        """

        vr = self.virtual_rewards
        pair_vr = vr[self.clone_partners]

        self.clone_probabilities = (pair_vr - vr) / torch.where(vr > 0, vr, 1e-8)
        r = np.random.uniform()
        self.clone_mask = (self.clone_probabilities >= r).cpu()

        if self.config.use_wandb:
            wandb.log(
                {
                    "fmc/num_cloned": self.clone_mask.sum(),
                },
                commit=False,
            )

    @torch.no_grad()
    def _prepare_clone_variables(self):
        # TODO: docstring

        # prepare virtual rewards and partner virtual rewards
        self._assign_clone_partners()
        self._calculate_distances()
        self._calculate_virtual_rewards()
        self._determine_clone_mask()

    @torch.no_grad()
    def _execute_cloning(self):
        """The cloning phase is where the collaboration of the cellular automata comes from. Using the virtual rewards calculated for
        each walker and clone partners that are randomly assigned, there is a probability that some walkers will be sent as reinforcements
        to their randomly assigned clone partner.

        The goal of the clone phase is to maintain a balanced density over state occupations with respect to exploration and exploitation.
        """

        # TODO: don't clone best walker (?)
        if self.verbose:
            print()
            print()
            print("clone stats:")
            print("state order", self.clone_partners)
            print("distances", self.distances)
            print("virtual rewards", self.virtual_rewards)
            print("clone probabilities", self.clone_probabilities)
            print("clone mask", self.clone_mask)
            print("state before", self.state)

        # keep track of which walkers received clones and how many.
        clones_received_per_walker = torch.bincount(
            self.clone_partners[self.clone_mask]
        ).unsqueeze(-1)
        self.clone_receives[
            : len(clones_received_per_walker)
        ] += clones_received_per_walker

        # execute clones
        self._clone_vector(self.state)
        self._clone_vector(self.actions)
        self._clone_vector(self.root_actions)
        self._clone_vector(self.reward_buffer)
        self._clone_vector(self.value_sum_buffer)
        self._clone_vector(self.visit_buffer)
        self._clone_vector(self.clone_receives)  # yes... clone clone receives lol.

        if self.verbose:
            print("state after", self.state)

    def _backpropagate_reward_buffer(self):
        """This essentially does the backpropagate step that MCTS does, although instead of maintaining an entire tree, it maintains
        value sums and visit counts for each walker. These values may be subsequently cloned. There is some information loss
        during this clone, but it should be minimally impactful.
        """

        # usually, we only backpropagate the walkers who are about to clone away. However, at the very end of the simulation, we want
        # to backpropagate the value regardless of if they are cloning or not.
        # TODO: experiment with this, i'm not sure if it's better to always backpropagate all or only at the end. it's an open question.
        backpropagate_all = self.simulation_iteration == self.k - 1

        mask = (
            torch.ones_like(self.clone_mask) if backpropagate_all else self.clone_mask
        )

        current_value_buffer = torch.zeros_like(self.value_sum_buffer)
        for i in reversed(range(self.simulation_iteration)):
            current_value_buffer[mask] = (
                self.reward_buffer[mask, i]
                + current_value_buffer[mask] * self.config.gamma
            )

        self.value_sum_buffer += current_value_buffer
        self.visit_buffer += mask.unsqueeze(-1)

        self.root_value_sum += current_value_buffer.sum()
        self.root_visits += mask.sum()

    @property
    def root_value(self):
        """Kind of equivalent to the MCTS root value."""

        return (self.root_value_sum / self.root_visits).item()

    @torch.no_grad()
    def _get_highest_value_action(self):
        """The highest value action corresponds to the walker whom has the highest average estimated value."""

        self.walker_values = self.value_sum_buffer / self.visit_buffer
        highest_value_walker_index = torch.argmax(self.walker_values)
        highest_value_action = (
            self.root_actions[highest_value_walker_index, 0].cpu().numpy()
        )

        return highest_value_action

    @torch.no_grad()
    def _get_action_with_highest_clone_receives(self):
        # TODO: docstring
        most_cloned_to_walker = torch.argmax(self.clone_receives)
        return self.root_actions[most_cloned_to_walker, 0].cpu().numpy()

    def _clone_vector(self, vector: torch.Tensor):
        vector[self.clone_mask] = vector[self.clone_partners[self.clone_mask]]