
# tensorboard --logdir logs
from SAC.SAC_Agent.Agent import Agent
import datetime
from contextlib import redirect_stdout
from IPython.utils.io import Tee
import os
import sys
import notice
import time
import shutil

#SACに与える値はここで変更する
totaleps=5000  #実行時間に寄与、基本長い方がスコアもいい
batchsize=64  #大小あんまり関係ない
maxmemlength=1e4
useloadmemory=False #Trueで既存メモリー使う、Falseで新規作成、各環境に対して最初はFalseで行う
descript="feedopt_total_eps3000" #実行の説明
COCONUM=0
noise=True



f=Tee("result.txt") #実行ログをresult.txtというファイルに書きこむ、以下のprint文はそのためのもの
start = datetime.datetime.now()
print("start:",start)
print("totaleps:",totaleps)
print("batchsize:",batchsize)
print("maxmemlength:",maxmemlength)
print("useloadmemory:",useloadmemory)
print("description:",descript)
print("COCONUM:",COCONUM)
print("noise:",noise)

#ここからメインの実行
#新たにエージェントクラスのオブジェクトを作る
SAC = Agent(total_eps=totaleps,
        batch_size=batchsize,
        max_mem_length=maxmemlength,
        use_load_memory=useloadmemory,
        description=descript,
        COCO_flowsheet_number=COCONUM,
        extra_explore_noise=noise)
#SAC = Agent(total_eps=2, batch_size=2,use_load_memory=True)

#学習の実行
SAC.run()
SAC.test_run()

#終了したらその時刻を記入
fin = datetime.datetime.now()
print("finish:",fin)
f.close()
del f

#実行終了をLINEで通知する
message="実行終了\n対象:"+str(SAC.description)+\
        "\nパラメータ:\ntotal_eps:"+str(SAC.total_eps)+\
        "\nbatch_size:"+str(SAC.batch_size)+\
        "\nmaxmemlength:"+str(maxmemlength)+\
        "\nmax_score"+str(round(max(SAC.total_scores),2))+\
        "\n開始時刻:"+start.strftime('%Y-%m-%d %H:%M:%S')+\
        "\n終了時刻"+fin.strftime('%Y-%m-%d %H:%M:%S')
notice.send_line_notify(message)