from SAC.Nets.Actor import GaussianPolicy as Actor
from SAC.Nets.Critic import Critic
from Utils.memory import Memory
import tensorflow as tf
import numpy as np
import time
import pickle
from Env.DC_gym import DC_Gym
from Utils.BFD_maker import Visualiser
from Env.STANDARD_CONFIG import CONFIG
import os
import datetime

physical_devices = tf.config.list_physical_devices('GPU')
tf.config.experimental.set_memory_growth(physical_devices[0], True)

tf.keras.backend.set_floatx('float32')

#エージェントの定義
"""
total_eps:総繰り返し回数
batch_size:1回のミニバッチ学習に使うデータの数
max_mem_length:リプレイバッファのサイズ
tau:Double deep q networkの学習率
Q_lr:許容誤差、他も同じ
gamma:報酬の割引率
use_load_memory:すでに昔作ったメモリーを使うか
reward_scaling:報酬のスケーリングのサイズ
COCO_flowsheet_number:使うファイルの番号
discrete_explore,extra_explore_noise:探索力強化用
"""
class Agent:
    def __init__(self, total_eps=2e2, batch_size=64, max_mem_length=1e4, min_mem_length=1e3, tau=0.005,
                 Q_lr=3e-4, policy_lr=3e-4, alpha_lr=3e-3, gamma=0.99, description="", use_load_memory=False,
                 reward_scaling=1, COCO_flowsheet_number=1, discrete_explore=True, extra_explore_noise=False):
        self.extra_explore_noise = extra_explore_noise
        self.discrete_explore = discrete_explore  # add randomness to seperate/submit action
        env_config = CONFIG(COCO_flowsheet_number).get_config()
        self.env = DC_Gym(*env_config, simple_state=True)
        self.total_eps = int(total_eps)
        self.eps_greedy_stop_step = int(total_eps*3/4)
        self.steps = 0
        self.total_scores = []
        self.batch_size = batch_size
        self.tau = tau
        self.memory = Memory(int(max_mem_length))
        self.min_mem_length = min_mem_length
        self.gamma = gamma

        #Actor, Criticのニューラルネットワークを設定
        self.Actor = Actor(self.env.real_continuous_action_space.shape[0])
        self.Q1 = Critic()
        self.Q2 = Critic()
        self.target_Q1 = Critic()
        self.target_Q1.set_weights(self.Q1.get_weights())
        self.target_Q2 = Critic()
        self.target_Q2.set_weights(self.Q2.get_weights())
        self.log_alpha = tf.Variable(-1, dtype=tf.float32)   # -3.5
        self.alpha = tf.Variable(0, dtype=tf.float32)
        self.alpha.assign(tf.exp(self.log_alpha))
        self.entropy_target = tf.constant(-np.prod(self.env.real_continuous_action_space.shape), dtype=tf.float32)

        #方策勾配法の計算アルゴリズムを設定
        self.Q1_optimizer = tf.keras.optimizers.Adam(Q_lr)
        self.Q2_optimizer = tf.keras.optimizers.Adam(Q_lr)
        self.Actor_optimizer = tf.keras.optimizers.Adam(policy_lr)
        self.alpha_optimizer = tf.keras.optimizers.Adam(alpha_lr)  #tf.keras.optimizers.Nadam(alpha_lr)  #
        self.reward_scaling = reward_scaling

        description = 'SAC_' + f"CONFIG_{COCO_flowsheet_number}___" + description
        self.description = description
        log_dir = './logs/' + description + \
                  time.asctime(time.localtime(time.time())).replace(" ", "_").replace(":", "-")
        self.memory_dir = "./SAC/memory_data/" + f"CONFIG_{COCO_flowsheet_number}___"

        self.summary_writer = tf.summary.create_file_writer(log_dir)
        self.use_load_memory = use_load_memory


    def run(self):
        if self.use_load_memory is True:
            self.load_memory()
        else:
            fill_memory_start=datetime.datetime.now()
            print("memory作成を開始します:", fill_memory_start.strftime('%Y-%m-%d %H:%M:%S'))
            self.fill_memory()
            fill_memory_fin=datetime.datetime.now()
            print("memory作成を終了しました:", fill_memory_fin.strftime('%Y-%m-%d %H:%M:%S'))
        for ep in range(self.total_eps):
            total_score = 0
            done = False
            state = self.env.reset()
            current_step = 0
            while not done:
                current_step += 1
                self.steps += 1
                state = self.env.State.state.copy()
                action_continuous, log_pi = self.Actor.sample_action(state)
                if self.extra_explore_noise is True and ep/self.total_eps < 0.5:
                    noise = 0.3 * np.random.normal(0, 1, size=action_continuous.shape)
                    action_continuous = np.clip(action_continuous + noise, a_min=-1, a_max=1)
                Q_value = tf.minimum(self.Q1([state, action_continuous]), self.Q2([state, action_continuous]))
                action_discrete = self.get_discrete_action(Q_value, ep)
                action_continuous = np.squeeze(action_continuous, axis=0)
                action = action_continuous, action_discrete
                next_state, annual_revenue, TAC, done, info = self.env.step(action)
                tops_state, bottoms_state = next_state
                reward = annual_revenue + TAC  # TAC's sign is included in the env
                total_score += reward

                if action_discrete == 0:
                        if len(info) == 2:  # this means we didn't have a failed solve
                            # note we scale the reward here
                            self.memory.add((state, action_continuous, reward*self.reward_scaling, tops_state, bottoms_state,
                                            1.0 - info[0], 1.0 - info[1]))
                            with self.summary_writer.as_default():
                                tf.summary.scalar('n_stages', action_continuous[0], step=self.steps)
                                tf.summary.scalar('feed_stage', action_continuous[1], step=self.steps)
                                tf.summary.scalar('reflux', action_continuous[2], step=self.steps)
                                tf.summary.scalar('reboil', action_continuous[3], step=self.steps)
                                tf.summary.scalar('log_pi', log_pi[0][0], step=self.steps)
                                tf.summary.scalar('pressure drop ratio', action_continuous[4], step=self.steps)
                                tf.summary.scalar('TAC', TAC, step=self.steps)
                                tf.summary.scalar('revenue', annual_revenue, step=self.steps)
                self.learn()

            if not (total_score == 0 and current_step == 1):  # fail solve from the start
                with self.summary_writer.as_default():
                    tf.summary.scalar("total score", total_score, ep)
                    tf.summary.scalar("episode length", current_step, ep)
                if len(self.total_scores) > 0 and total_score > max(self.total_scores):
                    try:
                        dt_today=datetime.datetime.today()
                        folder="./SAC/BFDs/"+self.description+dt_today.strftime("%m%d")
                        folder=folder.replace(" ", "_").replace(":", "-")
                        if not os.path.exists(folder):
                            os.mkdir(folder)
                        Visualise = Visualiser(self.env)
                        G = Visualise.visualise()
                        dt_now=datetime.datetime.now()
                        print("BFDを出力しました。時刻:", dt_now.strftime('%Y-%m-%d %H:%M:%S'))
                        G.write_png(folder+"/" + self.description + str(time.time()) + "_ep_" + str(ep) + "_score_" + str(round(total_score, 2)) + ".png")
                    except Exception as ex:
                        print(ex)
                        print(f"Couldnt draw BFD for total score of value {total_score}")
                self.total_scores.append(total_score)
        self.save_memory()

    def fill_memory(self):
        while len(self.memory.buffer) < self.min_mem_length:
            total_score = 0
            done = False
            state = self.env.reset()
            current_step = 0
            while not done:
                current_step += 1
                state = self.env.State.state.copy()
                action_continuous, _ = self.env.sample()
                action_discrete = 0  # always seperate until episode is over
                """
                if current_step == 0:
                    action_discrete = 0
                else:
                    action_discrete = np.random.choice([0, 1], p=[0.95, 0.05]) # strong bias to seperating
                """
                action = action_continuous, action_discrete
                next_state, annual_revenue, TAC, done, info = self.env.step(action)
                tops_state, bottoms_state = next_state
                reward = annual_revenue + TAC  # TAC's sign is included in the env
                total_score += reward

                if action_discrete == 0:
                        if len(info) == 2:  # this means we didn't have a failed solve
                            # note we scale the reward here
                            self.memory.add((state, action_continuous, reward*self.reward_scaling, tops_state, bottoms_state,
                                            1 - info[0], 1 - info[1]))
                            if len(self.memory.buffer) % (self.min_mem_length/10) == 0:
                                print(f"memory {len(self.memory.buffer)}/{self.min_mem_length}")
                                print(f"current score is {total_score}, on step {current_step}")
        pickle.dump(self.memory, open(self.memory_dir + "random_memory.obj", "wb"))

    #@tf.function
    def learn(self):
        """
        # Once we refresh load mems we should be able to run this without tf.cast
        batch = self.memory.sample(self.batch_size)
        states = np.squeeze(np.array([each[0] for each in batch]))
        actions = np.array([each[1] for each in batch])
        rewards = np.array([each[2] for each in batch]).reshape(self.batch_size, 1)
        tops_states = np.squeeze(np.array([each[3] for each in batch]))
        bottoms_states = np.squeeze(np.array([each[4] for each in batch]))
        tops_dones = np.array([each[5] for each in batch]).reshape(self.batch_size, 1)
        bottoms_dones = np.array([each[6] for each in batch]).reshape(self.batch_size, 1)
        """

        batch = self.memory.sample(self.batch_size)
        states = tf.cast(np.squeeze(np.array([each[0] for each in batch])), dtype=tf.float32)
        actions = tf.cast(np.array([each[1] for each in batch]), dtype=tf.float32)
        rewards = tf.cast(np.array([each[2] for each in batch]).reshape(self.batch_size, 1), dtype=tf.float32)
        tops_states = tf.cast(np.squeeze(np.array([each[3] for each in batch])), dtype=tf.float32)
        bottoms_states = tf.cast(np.squeeze(np.array([each[4] for each in batch])), dtype=tf.float32)
        tops_dones = tf.cast(np.array([each[5] for each in batch]).reshape(self.batch_size, 1), dtype=tf.float32)
        bottoms_dones = tf.cast(np.array([each[6] for each in batch]).reshape(self.batch_size, 1), dtype=tf.float32)

        self.critic_learn(states, actions, rewards, tops_states, bottoms_states, tops_dones, bottoms_dones)
        self.actor_learn(states)
        self.alpha_learn(states)
        self.update_targets()


    #@tf.function
    def critic_learn(self, states, actions, rewards, tops_states, bottoms_states, tops_dones, bottoms_dones):
        tops_actions, tops_log_pi = self.Actor.sample_action(tops_states)
        bottoms_actions, bottoms_log_pi = self.Actor.sample_action(bottoms_states)
        tops_hardQ = tf.minimum(self.target_Q1([tops_states, tops_actions]), self.target_Q2([tops_states, tops_actions]))
        bottoms_hardQ = tf.minimum(self.target_Q1([bottoms_states, bottoms_actions]), self.target_Q2([bottoms_states, bottoms_actions]))
        # sum over new generated states to get value
        # neither can be 0 because then we would stop seperating, but bounding probs has no effect (Q generally > 0)
        next_Q_target = tops_dones * tf.maximum(tops_hardQ - self.alpha * tops_log_pi, 0) + \
                        bottoms_dones * tf.maximum(bottoms_hardQ - self.alpha*bottoms_log_pi, 0)  # soft target
        Q_expected = tf.stop_gradient(rewards + self.gamma * next_Q_target)  # cannot be negative as then would separate
        #assert Q_expected.shape == (self.batch_size, 1)
        with tf.GradientTape() as tape1, tf.GradientTape() as tape2:
            tape1.watch(self.Q1.trainable_variables)
            tape2.watch(self.Q2.trainable_variables)
            Q1_predict = self.Q1([states, actions])
            Q2_predict = self.Q2([states, actions])
            loss_Q1 = tf.reduce_mean((Q_expected - Q1_predict)**2)
            loss_Q2 = tf.reduce_mean((Q_expected - Q2_predict)**2)
        Q1_gradient = tape1.gradient(loss_Q1, self.Q1.trainable_variables)
        Q2_gradient = tape2.gradient(loss_Q2, self.Q2.trainable_variables)
        self.Q1_optimizer.apply_gradients(zip(Q1_gradient, self.Q1.trainable_variables))
        self.Q2_optimizer.apply_gradients(zip(Q2_gradient, self.Q2.trainable_variables))

        with self.summary_writer.as_default():
            tf.summary.scalar("Q1 loss", loss_Q1, self.steps)
            tf.summary.scalar("Q2 loss", loss_Q2, self.steps)

    #@tf.function
    def actor_learn(self, states):
        with tf.GradientTape() as actor_tape:
            actor_tape.watch(self.Actor.trainable_variables)
            predicted_actions, predicted_log_pi = self.Actor.sample_action(states)
            Q1_predicted = self.Q1([states, predicted_actions])
            Q2_predicted = self.Q2([states, predicted_actions])
            Q_target = tf.minimum(Q1_predicted, Q2_predicted)
            actor_loss = tf.reduce_mean(self.alpha * predicted_log_pi - Q_target)
        actor_gradient = actor_tape.gradient(actor_loss, self.Actor.trainable_variables)
        self.Actor_optimizer.apply_gradients(zip(actor_gradient, self.Actor.trainable_variables))

        with self.summary_writer.as_default():
            tf.summary.scalar("Actor loss", actor_loss, self.steps)
            tf.summary.scalar("actor_Q_target", tf.reduce_mean(Q_target), self.steps)

    #@tf.function
    def alpha_learn(self, states):
        predicted_actions, predicted_log_pi = self.Actor.sample_action(states)
        with tf.GradientTape() as alpha_tape:
            alpha_tape.watch(self.alpha)
            alpha_loss = tf.reduce_mean(self.log_alpha * tf.stop_gradient(-predicted_log_pi - self.entropy_target))
        alpha_gradient = alpha_tape.gradient(alpha_loss, [self.log_alpha]) # seems like other people all use log_alpha here
        self.alpha_optimizer.apply_gradients(zip(alpha_gradient, [self.log_alpha]))
        self.alpha.assign(tf.exp(self.log_alpha))

        with self.summary_writer.as_default():
            tf.summary.scalar("alpha loss", alpha_loss, self.steps)
            tf.summary.scalar("alpha value", self.alpha, self.steps)

    @tf.function
    def update_targets(self):
        for local_weight, target_weight in zip(self.Q1.trainable_variables, self.target_Q1.trainable_variables):
            target_weight.assign(self.tau*local_weight + (1.0 - self.tau) * target_weight)
        for local_weight, target_weight in zip(self.Q2.trainable_variables, self.target_Q2.trainable_variables):
            target_weight.assign(self.tau*local_weight + (1.0 - self.tau) * target_weight)

    def get_discrete_action(self, Q_value, ep):
        if self.env.current_step == 0:  # must at least separate first stream
            return 0
        if ep/self.total_eps > 0.5 or self.discrete_explore is False:
            if Q_value > 0:  # separate
                action_discrete = 0
            else:  # submit
                action_discrete = 1
        else:
            # for first portion of training always seperate (this can never create errors to early episode Q-values,
            # because negative Q values don't effect early ones due to the max(0, Qnext)
            action_discrete = 0
            """
            if Q_value > 0:  # separate
                action_discrete = 0
            else:  # submit
                explore_probability = 0.6 - (ep) / (self.total_eps) * 0.5  # start at 60/40 and lower until
                action_discrete = np.random.choice([0,1], p=[explore_probability, 1-explore_probability])
            """
        return action_discrete


    def save_memory(self):
        pickle.dump(self.memory, open(self.memory_dir + str(time.time()) + ".obj", "wb"))
        pickle.dump(self.memory, open(self.memory_dir + "memory.obj", "wb"))

    def load_memory(self):
        """
        currently just always expanding memory.obj
        """
        old_memory = pickle.load(open(self.memory_dir + "random_memory.obj", "rb"))
        self.memory.buffer += old_memory.buffer

    def test_run(self):
        state = self.env.reset()
        done = False
        total_score = 0
        i = 0
        while not done:
            i +=1
            state = self.env.State.state.copy()
            mean, std = self.Actor(state)
            action_continuous = tf.tanh(mean)
            Q_value = tf.minimum(self.Q1([state, action_continuous]), self.Q2([state, action_continuous]))
            if Q_value > 0:
                action_discrete = 0  # seperate if positive reward predicted
            else:
                action_discrete = 1
            action_continuous = np.squeeze(action_continuous, axis=0)
            action = action_continuous, action_discrete
            next_state, annual_revenue, TAC, done, info = self.env.step(action)
            reward = annual_revenue + TAC  # TAC's sign is included in the env
            total_score += reward
            print(f"step {i}: \n annual_revenue: {annual_revenue}, TAC: {TAC} \n Q_values {Q_value}, reward {reward}")
        print(total_score)
        print(f"\nTotal revenue is ${self.env.total_revenue/1e6} M \n Total TAC is ${self.env.total_TAC/1e6} \n")
        Visualise = Visualiser(self.env)
        G = Visualise.visualise()
        G.write_png("./SAC/final" + self.description + str(time.time()) + ".png")
