import numpy as np
from Env.ClassDefinitions import Stream, State
from gym import spaces

from Env.DC_class import SimulatorDC

#学習環境DC_Gymの設定
class DC_Gym(SimulatorDC):
    """
    Flowsheet needs to be configured to expose editable unit parameters, and standardise unit naming
    This version of the gym only has a single stream as the state. Discrete actions are just to seperate or not
    """
    def __init__(self, document_path, sales_prices, fail_solve_punishment, required_purity=0.95,
                 annual_operating_hours=8000, simple_state=True, auto_submit=True):
        super().__init__(document_path)
        self.fail_solve_punishment = fail_solve_punishment  # has to be configured for specific envs #計算失敗のペナルティ
        self.simple_state = simple_state
        self.auto_submit = auto_submit
        self.sales_prices = sales_prices #製品価格
        self.required_purity = required_purity #製品仕様
        self.annual_operating_seconds = annual_operating_hours*3600 #運転時間

        feed_conditions = self.get_stream_conditions() #フィード
        self.original_feed = Stream(1, feed_conditions["flows"],
                                    feed_conditions["temperature"],
                                    feed_conditions["pressure"]
                                    )
        self.n_components = len(self.original_feed.flows) #成分数
        # now am pretty flexible in number of max streams, to prevent simulation going for long set maximum to 10
        self.max_outlet_streams = self.n_components #最終的に許容するプロセスの大きさ, 元は3倍
        self.stream_table = [self.original_feed]

        #状態をつくる
        self.State = State(self.original_feed, self.max_outlet_streams, simple=simple_state)
        self.min_recovery_flow = self.original_feed.flows/100  # definately aren't interested in streams with 1/100th recovery

        if simple_state:
            # Now configure action space
            self.discrete_action_names = ['seperate_yes_or_no']
            self.discrete_action_space = spaces.Discrete(2)
        else:
            self.discrete_action_names = ['stream selection']
            self.discrete_action_space = spaces.Discrete(self.max_outlet_streams + 1)

        #行動空間の定義
        # number of stages will currently be rounded off
        # pressure drop is as a fraction of the current pressure
        #行動空間の変数の種類
        self.continuous_action_names = ['number of stages', 'feed stage', 'reflux ratio', 'reboil ratio', 'pressure drop ratio']
        # these will get converted to numbers between -1 and 1
        #取り得る値の範囲
        self.real_continuous_action_space = spaces.Box(low=np.array([5, 1, 0.1, 0.1, 0]), high=np.array([100, 100, 10, 10, 0.9]),
                                                        shape=(5,))
        self.real_continuous_action_space_alt = spaces.Box(low=np.array([5, 1, 0.1, 0.1, 0]), high=np.array([100, 100, 10, 10, 0.9]),
                                                        shape=(5,))
        self.continuous_action_space = spaces.Box(low=-1, high=1, shape=(5,))
        # define gym space objects
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=self.State.state.shape)
        self.failed_solves = 0
        self.error_counter = {"total_solves": 0,
                                "error_solves": 0}  # to get a general idea of how many solves are going wrong
        self.current_step = 0
        self.total_revenue = 0
        self.total_TAC = 0

        self.reward_norm = 0
        self.max_total_revenue = 0
        #フローの流れのうち最大価格のものを標準化につかう
        for i, flow in enumerate(feed_conditions["flows"]):
            stream = np.zeros(self.n_components)
            stream[i] = flow
            stream_revenue = self.stream_value(stream)
            if stream_revenue > self.reward_norm:
                self.reward_norm = stream_revenue  # set reward normalisation to the max possible single reward
            self.max_total_revenue += stream_revenue

    #stepが学習の最小単位(相互作用1回)、これを繰り返す
    def step(self, action):
        continuous_actions, discrete_action = action
        if discrete_action == self.discrete_action_space.n - 1:  # submit
            self.current_step += 1
            done = self.State.submit_stream()  # if this results in 0 outlet streams then done
            info = {}
            revenue = 0.0
            TAC = 0.0
            next_state = self.State.state
            if self.simple_state:
                return ("NA", "NA"), revenue, TAC, done, info
            else:
                return next_state, revenue, TAC, done, info

        #環境ファイルを読み込む
        self.import_file()  # current workaround is to reset the file before each solve

        selected_stream = self.State.streams[discrete_action]
        assert selected_stream.flows.max() > self.min_recovery_flow[selected_stream.flows.argmax()]

        # put the selected stream (flows, temperature, pressure) as the input to a new column
        #行動を選択
        real_continuous_actions = self.get_real_continuous_actions(continuous_actions)
        self.set_inlet_stream(selected_stream.flows, selected_stream.temperature, selected_stream.pressure)

        #行動の値を数値に丸めたり
        n_stages = int(real_continuous_actions[0])
        feed_stage = int(real_continuous_actions[1])
        reflux_ratio = round(real_continuous_actions[2], 2)
        reboil_ratio = round(real_continuous_actions[3], 2)
        pressure_drop_ratio = round(real_continuous_actions[4], 2)

        #蒸留塔にさっきの行動の値を入力
        self.set_unit_inputs(n_stages, feed_stage, reflux_ratio, reboil_ratio, pressure_drop_ratio)
        #実行する
        sucessful_solve = self.solve()
        self.error_counter["total_solves"] += 1
        #実行を評価
        if sucessful_solve is False:
            self.failed_solves += 1
            self.error_counter["error_solves"] += 1
            TAC = 0.0
            revenue = 0.0
            #失敗が続いたら失敗試行として評価し、各種値を返す
            if self.failed_solves >= 3:  # reset if we fail 3 times
                # discourage these actions with negative reward + ending that streams seperation tree (done, done)
                print("3 failed solves")
                done = self.State.submit_stream()  # make stream a product
                TAC = self.fail_solve_punishment  # have to configure this for each environment
                info = [True, True]
            else:
                done = False
                info = {"failed solve": 1}
            next_state = self.State.state
            if self.simple_state:
                # basically like returning state as tops, 0 as bottoms because nothing has happened in the seperation
                tops = next_state.copy()
                bottoms = np.zeros(tops.shape)
                return (tops, bottoms), revenue, -TAC, done, info
            else:
                return next_state, revenue, -TAC, done, info

        #ステップ数を足す
        self.current_step += 1  # if there is a sucessful solve then step the counter
        # TAC includes operating costs so we actually don't need these duties
        TAC, condenser_duty, reboiler_duty = self.get_outputs()

        #蒸留塔出口の上下の出力の値を読み込む
        tops_info, bottoms_info = self.get_outlet_info()
        tops_flow, tops_temperature, tops_pressure = tops_info
        bottoms_flow, bottoms_temperature, bottoms_pressure = bottoms_info
        tops = Stream(self.State.n_total_streams + 1, tops_flow, tops_temperature, tops_pressure)
        bottoms = Stream(self.State.n_total_streams + 2, bottoms_flow, bottoms_temperature, bottoms_pressure)

        #蒸留塔からの出力を評価
        if self.auto_submit is True:
            # always working with this option for now
            tops_revenue = self.stream_value(tops_flow)
            bottoms_revenue = self.stream_value(bottoms_flow)
            annual_revenue = tops_revenue + bottoms_revenue
            if self.State.n_total_streams < self.max_outlet_streams:
                # if max streams not yet reached then only streams with revenue or not enough flow are product
                is_product = [False, False]
                if tops_revenue > 0 or tops_flow.max() <= self.min_recovery_flow[tops_flow.argmax()]:
                    is_product[0] = True
                if bottoms_revenue > 0 or bottoms_flow.max() <= self.min_recovery_flow[bottoms_flow.argmax()]:
                    is_product[1] = True
            else:
                is_product = [True, True]  # once max streams reached then each columns new streams have to be product
            self.State.update_state([tops, bottoms], is_product)
            info = is_product
        else:
            annual_revenue = self.stream_value(tops_flow) + self.stream_value(bottoms_flow) - self.stream_value(selected_stream.flows)
            self.State.update_state([tops, bottoms])
            info = {}

        if self.simple_state is True:
            # always working with this for now, will have to do checks if we want to change to complex state
            next_state = self.State.get_next_state(tops, bottoms)
        else:
            next_state = self.State.state

        #物質収支成立を評価
        mass_balance_rel_error = np.absolute(
            (selected_stream.flows-(tops.flows+bottoms.flows)) / np.maximum(selected_stream.flows, 0.01)) # max to prevent divide by 0
        if mass_balance_rel_error.max() >= 0.05:
            print("MB error!!!")

        #状態を更新
        if self.State.n_streams == 0:
            # episode done when no streams left
            done = True
        else:
            done = False
        column_inlet_conditions = self.get_stream_conditions('4')
        self.State.add_column_data(in_number=selected_stream.number, tops_number=tops.number,
                                    bottoms_number=bottoms.number, n_stages=n_stages, feed_stage=feed_stage, reflux_ratio=reflux_ratio,
                                    reboil_ratio=reboil_ratio, OperatingPressure=column_inlet_conditions["pressure"],
                                    InletTemperature=column_inlet_conditions["temperature"], TAC=TAC)
        self.total_revenue += annual_revenue
        self.total_TAC += TAC
        return next_state, annual_revenue/self.reward_norm, -TAC/self.reward_norm, done, info

    #行動を決めるための関数
    def get_real_continuous_actions(self, continuous_actions):
        # interpolation
        #連続行動空間の範囲に-1から1までの連続値の積を取って内挿して行う
        real_continuous_actions = self.real_continuous_action_space.low + \
                            (continuous_actions - self.continuous_action_space.low)/\
                            (self.continuous_action_space.high - self.continuous_action_space.low) *\
                            (self.real_continuous_action_space.high - self.real_continuous_action_space.low)
        #フィード位置最適化ようにフィード位置の値が総段数を超えたらやり直す
        if int(real_continuous_actions[0]) < int(real_continuous_actions[1]):
            self.real_continuous_action_space_alt = spaces.Box(low=np.array([5, 1, 0.1, 0.1, 0]), high=np.array([100, real_continuous_actions[0], 10, 10, 0.9]),
                                                        shape=(5,))
            real_continuous_actions_alt = self.real_continuous_action_space_alt.low + \
                                (continuous_actions - self.continuous_action_space.low)/\
                                (self.continuous_action_space.high - self.continuous_action_space.low) *\
                                (self.real_continuous_action_space_alt.high - self.real_continuous_action_space_alt.low)
            return real_continuous_actions_alt
        else:
            return real_continuous_actions

    @property
    def legal_discrete_actions(self):
        """
        Illegal actions:
        - Choose Null Stream in stream table
        """
        legal_actions = [i for i in range(0, self.State.n_streams)]
        if self.State.n_streams > 3: # for now only let submission after at least 2 columns
            legal_actions.append(self.discrete_action_space.n - 1)
        return legal_actions

    def sample(self):
        discrete_action = self.discrete_action_space.sample()
        continuous_action = self.continuous_action_space.sample()
        return continuous_action, discrete_action

    def reset(self):
        self.reset_flowsheet()
        self.stream_table = [self.original_feed]
        self.State = State(self.original_feed, self.max_outlet_streams)
        self.column_streams = []
        self.failed_solves = 0
        self.current_step = 0
        self.total_revenue = 0
        self.total_TAC = 0
        return self.State.state.copy()

    #報酬計算
    def reward_calculator(self, inlet_flow, tops_flow, bottoms_flow, TAC):
        annual_revenue = self.stream_value(tops_flow) + self.stream_value(bottoms_flow) - self.stream_value(inlet_flow)
        reward = annual_revenue - TAC  # this represents the direct change annual profit caused by the additional column

        return reward
    #流れの価格を計算
    def stream_value(self, stream_flow):
        if max(stream_flow / sum(stream_flow)) >= self.required_purity:
            revenue_per_annum = max(stream_flow) * self.sales_prices[np.argmax(stream_flow)] * self.annual_operating_seconds
            return revenue_per_annum
        else:
            return 0