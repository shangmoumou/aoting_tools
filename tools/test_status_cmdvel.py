# -*- coding: utf-8 -*-
"""v1.0.31 采集端测试：导航状态 /cmd_vel（线速度 / 角速度）

这个话题的难点不在解析，而在【它静止时根本不发布】——
所以测试重点放在三件事上：
  1) 两个值确实取对了（linear.x / angular.z）
  2) "正在发布" 与 "已停止发布" 判得对
  3) 话题后来才出现 / 中途消失，能自动订阅 / 退订
第 3 条是最容易漏的：一次性等待失败后再也不看，
机器动起来就永远捕捉不到了。
"""
import os, sys, time, threading
sys.path.insert(0, '/data/workspace/aoting_tools/apps/statusweb')

PASS = FAIL = 0
def ck(cond, name, extra=''):
    global PASS, FAIL
    if cond: PASS += 1; print('PASS  ' + name)
    else: FAIL += 1; print('FAIL  ' + name + ('  | ' + str(extra) if extra else ''))

import stat_agent as A

# ── 假 rospy ──
class FakeSub(object):
    def __init__(self, ros, topic):
        self.ros = ros
        self.topic = topic
    def unregister(self):
        self.ros.unregistered.append(self.topic)
        self.ros.subs.pop(self.topic, None)

class FakeRos(object):
    def __init__(self, topics=None):
        self.topics = list(topics or [])
        self.subs = {}
        self.unregistered = []
        self.qsize = None
    def get_published_topics(self):
        return [(n, t) for (n, t) in self.topics]
    def Subscriber(self, topic, cls, cb, queue_size=None):
        self.qsize = queue_size
        s = FakeSub(self, topic)
        self.subs[topic] = {'cls': cls, 'cb': cb, 'sub': s}
        return s

# ── 假消息：geometry_msgs/Twist 的形状 ──
class Vec(object):
    __slots__ = ('x', 'y', 'z')
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z

class Twist(object):
    __slots__ = ('linear', 'angular')
    def __init__(self, lx=0.0, az=0.0):
        self.linear = Vec(lx, 0.0, 0.0)
        self.angular = Vec(0.0, 0.0, az)

# ── _num：NaN / 无穷大 / None 挡掉 ──
ck(A._num(0.499769168169) == 0.499769168169, '正常值原样返回')
ck(A._num(0) == 0.0, '0 保留（是有效值，不能当缺失）')
ck(A._num(None) is None, 'None -> None')
ck(A._num('abc') is None, '非数字 -> None')
ck(A._num(float('nan')) is None, 'NaN 被挡掉（否则页面显示 nan m/s）')
ck(A._num(float('inf')) is None, 'inf 被挡掉')
ck(A._num(float('-inf')) is None, '-inf 被挡掉')

# ── 回调：只取 linear.x 与 angular.z ──
A._cmdvel_cb(Twist(0.499769168169, -2.51382537867e-06))
snap = A.cmdvel_snapshot()
ck(abs(snap['lin_x'] - 0.499769168169) < 1e-12,
   'linear.x 取对: %s' % snap['lin_x'])
ck(abs(snap['ang_z'] + 2.51382537867e-06) < 1e-20,
   'angular.z 取对: %s' % snap['ang_z'])
ck(snap['live'] is True, '刚收到消息 -> live=True')
ck(snap['topic'] == '/cmd_vel', '快照带话题名')
ck(snap['err'] == '', '正常解析无 err')

# ── 停止发布：值要留着，live 要翻过来 ──
A.CMDVEL_STATE['at'] = time.time() - (A.CMDVEL_STALE + 0.5)
snap2 = A.cmdvel_snapshot()
ck(snap2['live'] is False, '超过 STALE 没消息 -> live=False')
ck(snap2['lin_x'] == snap['lin_x'], '停发后仍保留最后一次的线速度')
ck(snap2['ang_z'] == snap['ang_z'], '停发后仍保留最后一次的角速度')
ck(snap2['age'] is not None and snap2['age'] > A.CMDVEL_STALE,
   'age = 距上次消息的秒数: %s' % snap2['age'])

# ── 缺字段不炸 ──
class Half(object):
    __slots__ = ('linear',)
    def __init__(self):
        self.linear = Vec(1.0, 0.0, 0.0)
A._cmdvel_cb(Half())
ck(A.cmdvel_snapshot()['lin_x'] == 1.0, '缺 angular 时 linear 仍取到')
ck(A.cmdvel_snapshot()['ang_z'] is None, '缺 angular -> None（不显示）')

# ── 轮询线程：话题消失 -> 退订并清空 ──
A._load_msg_class = lambda typ: Twist        # 免得去 import 真消息
ros = FakeRos()
t = threading.Thread(target=A._cmdvel_watch, args=(ros,), daemon=True)
t.start()
time.sleep(1.4)
s0 = A.cmdvel_snapshot()
ck(s0['ok'] is False, '话题不在列表 -> ok=False')
ck('无控制指令' in s0['err'], '话题不在列表 -> 明说当前无控制指令: %s' % s0['err'])
ck(s0['lin_x'] is None, '话题消失后不再留旧值（避免显示几秒前的速度）')

# ── 话题出现 -> 自动订阅 ──
ros.topics = [('/cmd_vel', 'geometry_msgs/Twist')]
time.sleep(1.4)
s1 = A.cmdvel_snapshot()
ck(s1['ok'] is True, '话题出现 -> 自动订阅成功')
ck('/cmd_vel' in ros.subs, '确实建了订阅')
ck(ros.qsize == 1, 'queue_size=1（高频话题只留最新一条），实际 %s' % ros.qsize)
cb = ros.subs['/cmd_vel']['cb']
cb(Twist(0.25, 0.5))
s2 = A.cmdvel_snapshot()
ck(s2['lin_x'] == 0.25 and s2['ang_z'] == 0.5, '回调能收到新值')
ck(s2['live'] is True, '有消息 -> live=True')

# ── 话题又消失 -> 退订 ──
ros.topics = []
time.sleep(1.4)
ck('/cmd_vel' in ros.unregistered, '话题消失 -> 主动退订')
ck(A.cmdvel_snapshot()['ok'] is False, '退订后 ok=False')

# ── 消息类加载失败要给出原因，而不是静默 ──
ros2 = FakeRos([('/cmd_vel', 'geometry_msgs/Twist')])
def boom(typ):
    raise ImportError('No module named some_msgs')
A._load_msg_class = boom
t2 = threading.Thread(target=A._cmdvel_watch, args=(ros2,), daemon=True)
t2.start()
time.sleep(1.4)
s3 = A.cmdvel_snapshot()
ck('无法加载' in s3['err'], '消息类加载失败 -> 给出原因: %s' % s3['err'])

# ── rospy 不可用时也要有说法 ──
A._set_err(A.CMDVEL_LOCK, A.CMDVEL_STATE, 'rospy 不可用（测试）')
ck('rospy 不可用' in A.cmdvel_snapshot()['err'], 'rospy 不可用 -> err 带到页面')

# ── 阈值合理 ──
ck(0 < A.CMDVEL_STALE <= 3.0, '停止判定阈值在 0~3 秒之间: %s' % A.CMDVEL_STALE)

# ── 输出协议里有 nav ──
src = open('/data/workspace/aoting_tools/apps/statusweb/stat_agent.py',
           encoding='utf-8').read()
ck('"nav": cmdvel_snapshot()' in src, '主循环输出里带 nav 字段')
ck('_cmdvel_watch, args=(rospy,)' in src, 'ROS 线程里启动了轮询')
ck('_set_err(CMDVEL_LOCK, CMDVEL_STATE, msg)' in src,
   'rospy/master 不可用时也会写 cmd_vel 的原因')
# 单引号禁令：本脚本会被 shell_quote 包住送到远端 bash
ck("'" not in src, '采集脚本全文无单引号（shell 多层引用会变形）')

print('\n通过 %d / 失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
