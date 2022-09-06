import gym

import torch
from data.data_handler import DataHandler
from fmc import FMC

from models.dynamics import FullyConnectedDynamicsModel
from models.joint_model import JointModel
from models.prediction import FullyConnectedPredictionModel
from models.representation import FullyConnectedRepresentationModel
from data.replay_buffer import GameHistory, ReplayBuffer
from trainer import Trainer

from tqdm import tqdm


def play_game(env, model: JointModel) -> GameHistory:
    obs = env.reset()
    game_history = GameHistory(obs)

    num_walkers = 16
    lookahead_steps = 10
    steps = 10
    for _ in range(steps):
        obs = torch.tensor(obs, device=model.device)

        state = model.representation_model.forward(obs)  # TODO: move this into FMC

        # action = lookahead(state, dynamics_model, lookahead_steps)
        fmc = FMC(num_walkers, model, state)
        action = fmc.simulate(lookahead_steps)

        obs, reward, done, info = env.step(action)

        game_history.append(action, obs, reward, fmc.root_value)

        # print("reward", reward)
        if done:
            # print('done')
            break

        # env.render()
        # fmc.render_best_walker_path()

    return game_history


if __name__ == "__main__":
    # device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    device = torch.device("cpu")

    env = gym.make("CartPole-v0")

    max_replay_buffer_size = 128

    embedding_size = 16
    out_features = 1

    representation_model = FullyConnectedRepresentationModel(env, embedding_size)
    dynamics_model = FullyConnectedDynamicsModel(
        env, embedding_size, out_features=out_features
    )
    prediction_model = FullyConnectedPredictionModel(env, embedding_size)
    joint_model = JointModel(representation_model, dynamics_model, prediction_model).to(device)

    replay_buffer = ReplayBuffer(max_replay_buffer_size)
    data_handler = DataHandler(env, replay_buffer, device=device)
    trainer = Trainer(data_handler, joint_model, use_wandb=True)

    num_games = 1024
    train_every = 32

    for i in tqdm(range(num_games), desc="Playing games and training", total=num_games):
        game_history = play_game(env, joint_model)
        # print(game_history)
        replay_buffer.append(game_history)

        if i % train_every == 0:
            trainer.train_step()
