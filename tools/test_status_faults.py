# -*- coding: utf-8 -*-
"""
远程机器状态监控 —— 故障话题（/diagnose_faults）测试。

重点不是"能不能解析一个 dict"，而是把【订阅 → 回调 → 快照】这条链
真的跑一遍：这段跨了 rospy、线程、递归转换三个环节，任何一处坏掉的
表现都是"页面上永远显示未获取到"，静态检查看不出来。

所以这里用一个假的 rospy + 假的消息类，让采集脚本真起线程、真订阅、
真回调，再看 fault_snapshot() 拿出来的东西对不对。
"""
import io
import os
import sys
import types
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AGENT = os.path.join(ROOT, 'apps', 'statusweb', 'stat_agent.py')

PASS = FAIL = 0


def chk(n, c, e=''):
    global PASS, FAIL
    if c:
        PASS += 1
        print('  ok  ', n, e)
    else:
        FAIL += 1
        print('  FAIL', n, e)


def sec(n):
    print('\n=== %s ===' % n)


def load_agent():
    import importlib.util
    spec = importlib.util.spec_from_file_location('stat_agent_u', AGENT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── 假 rospy / 假消息 ────────────────────────────────
class TimeMsg(object):
    __slots__ = ['secs', 'nsecs']

    def __init__(self, s, n=0):
        self.secs = s
        self.nsecs = n


class VoiceMsg(object):
    __slots__ = ['voice_data', 'voice_name', 'default_voice_key']

    def __init__(self):
        self.voice_data = ''
        self.voice_name = ''
        self.default_voice_key = 'sensor_abnormal'


class AlarmMsg(object):
    __slots__ = ['node_name', 'event_level', 'event_time', 'event_data',
                 'event_code', 'event_code_str', 'event_voice', 'uuid',
                 'show_to_user', 'cancel_trigger', 'additional',
                 'applistshow', 'appmescentershow', 'pad_alarm_know',
                 'appToast', 'insert_histort_center', 'pad_alarm_dialog',
                 'pad_alarm_popup', 'param_list']

    def __init__(self, code, data, level, secs, node='/diagnose_node'):
        self.node_name = node
        self.event_level = level
        self.event_time = TimeMsg(secs)
        self.event_data = data
        self.event_code = code
        self.event_code_str = ''
        self.event_voice = VoiceMsg()
        self.uuid = 'x'
        self.show_to_user = False
        self.cancel_trigger = False
        self.additional = ''
        self.applistshow = 1
        self.appmescentershow = 1
        self.pad_alarm_know = 0
        self.appToast = 0
        self.insert_histort_center = 0
        self.pad_alarm_dialog = 0
        self.pad_alarm_popup = 1
        self.param_list = []


class FaultsMsg(object):
    __slots__ = ['alarms', 'alarm_level', 'relocation', 'gohome',
                 'taskpause', 'halt_mode']

    def __init__(self, alarms, level):
        self.alarms = alarms
        self.alarm_level = level
        self.relocation = 0
        self.gohome = 0
        self.taskpause = 0
        self.halt_mode = 0


SUB = {'cb': None, 'topic': None, 'cls': None}
PUBLISHED = {'topics': [('/diagnose_faults', 'v3_msgs/DiagnoseFaults')]}
INIT = {'ok': True}


def make_fake_rospy(init_ok=True):
    m = types.ModuleType('rospy')

    def init_node(name, anonymous=False, disable_signals=False):
        if not init_ok:
            raise RuntimeError('master unreachable')
        return None

    def get_published_topics(namespace='/'):
        return list(PUBLISHED['topics'])

    def Subscriber(topic, cls, cb, **kw):
        SUB['topic'] = topic
        SUB['cls'] = cls
        SUB['cb'] = cb
        return None

    def spin():
        return None

    m.init_node = init_node
    m.get_published_topics = get_published_topics
    m.Subscriber = Subscriber
    m.spin = spin
    return m


def install_msg_module(cls):
    pkg = types.ModuleType('v3_msgs')
    msg = types.ModuleType('v3_msgs.msg')
    setattr(msg, 'DiagnoseFaults', cls)
    pkg.msg = msg
    sys.modules['v3_msgs'] = pkg
    sys.modules['v3_msgs.msg'] = msg


def reset(agent, keep_sub=False):
    with agent.FAULT_LOCK:
        agent.FAULT_STATE.update({'ok': False, 'err': '', 'items': [],
                                  'level': None, 'at': 0, 'type': ''})
    if not keep_sub:
        SUB.update({'cb': None, 'topic': None, 'cls': None})
    PUBLISHED['topics'] = [('/diagnose_faults', 'v3_msgs/DiagnoseFaults')]


def run_thread(agent, timeout=6.0):
    """起订阅线程并等到它真的订阅上（或报错退出）。"""
    t = agent.start_ros()
    end = time.time() + timeout
    while time.time() < end:
        with agent.FAULT_LOCK:
            _e = agent.FAULT_STATE['err']
        # "正在等待话题…"是中间态，不算结束，要一直等到真正的结果
        done = bool(SUB['cb']) or (bool(_e) and not _e.startswith('正在等待'))
        if done:
            break
        time.sleep(0.05)
    time.sleep(0.15)
    return t


# ══════════════════════════════════════════════
sec('1. 真实数据形态（rospy 消息对象）')
agent = load_agent()
sys.modules['rospy'] = make_fake_rospy(True)
install_msg_module(FaultsMsg)
reset(agent)
run_thread(agent)

chk('订阅到了话题', SUB['topic'] == '/diagnose_faults', str(SUB['topic']))
chk('拿到正确的消息类', SUB['cls'] is FaultsMsg)

msg = FaultsMsg([
    AlarmMsg(1589, u'IMU数据获取超时', 3, 1790040465),
    AlarmMsg(261, u'SLAM_A节点心跳异常', 2, 1790047267),
], 3)
SUB['cb'](msg)
snap = agent.fault_snapshot()

chk('标记为已获取', snap['ok'] is True)
chk('无错误', snap['err'] == '', snap['err'])
chk('条数正确', len(snap['items']) == 2, str(len(snap['items'])))
it = snap['items'][0]
chk('严重的排最前', it['code'] == 1589 and it['level'] == 3, str(it))
chk('故障内容正确', it['data'] == u'IMU数据获取超时', repr(it['data']))
chk('节点名正确', it['node'] == '/diagnose_node')
chk('总级别带出来', snap['level'] == 3, str(snap['level']))
chk('时间已格式化', bool(it['tstr']), it['tstr'])
chk('原始秒数保留', it['t'] == 1790040465, str(it['t']))
chk('第二条是低级别', snap['items'][1]['code'] == 261)
chk('消息类型带出来', snap['type'] == 'v3_msgs/DiagnoseFaults', snap['type'])

sec('2. 空数组（alarms 为空）')
reset(agent, keep_sub=True)
SUB['cb'](FaultsMsg([], 0))
snap2 = agent.fault_snapshot()
chk('仍然算已获取', snap2['ok'] is True)
chk('条数为 0', len(snap2['items']) == 0)
chk('总级别为 0', snap2['level'] == 0, str(snap2['level']))

sec('3. 纯 dict 形态（rostopic echo 出来的结构）')
d = {
    'alarms': [
        {'node_name': '/diagnose_node', 'event_level': 3,
         'event_time': {'secs': 1790040465, 'nsecs': 73388005},
         'event_data': u'IMU数据获取超时', 'event_code': 1589},
    ],
    'alarm_level': 3,
}
chk('dict 也能解析', len(agent._items_of(d)) == 1)
chk('dict 内容正确', agent._items_of(d)[0]['data'] == u'IMU数据获取超时')
chk('dict 时间正确', agent._items_of(d)[0]['t'] == 1790040465)

sec('4. 降级路径（都要带 err，不能静默空白）')
reset(agent)
PUBLISHED['topics'] = []
sys.modules['rospy'] = make_fake_rospy(True)
run_thread(agent, timeout=20.0)
snap3 = agent.fault_snapshot()
chk('话题未发布有提示', '未发布' in snap3['err'], snap3['err'])
chk('未发布时不算已获取', snap3['ok'] is False)

reset(agent)
sys.modules['rospy'] = make_fake_rospy(False)
run_thread(agent, timeout=3.0)
snap4 = agent.fault_snapshot()
chk('master 挂了有提示', 'ROS master' in snap4['err'], snap4['err'])

reset(agent)
sys.modules['rospy'] = make_fake_rospy(True)
PUBLISHED['topics'] = [('/diagnose_faults', 'no_such_pkg/Whatever')]
run_thread(agent, timeout=3.0)
snap5 = agent.fault_snapshot()
chk('消息类加载失败有提示', '无法加载消息类型' in snap5['err'], snap5['err'])

reset(agent)
sys.modules['rospy'] = None
del sys.modules['rospy']
sys.modules['v3_msgs'] = None
del sys.modules['v3_msgs']
del sys.modules['v3_msgs.msg']
real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__
run_thread(agent, timeout=3.0)
snap6 = agent.fault_snapshot()
chk('rospy 不可用有提示', 'rospy 不可用' in snap6['err'], snap6['err'])

sec('5. 采集脚本本身不能被 shell 引号搞坏')
src = io.open(AGENT, encoding='utf-8').read()
body = '\n'.join(l for l in src.split('\n')
                 if not l.strip().startswith('#'))
chk('正文不含单引号（会被 shell_quote 破坏）', "'" not in body)
chk('订阅话题名正确', '"/diagnose_faults"' in src)
chk('订阅是独立线程', 'start_ros' in src and 'threading.Thread' in src)
chk('主循环带出故障快照', '"faults": fault_snapshot()' in src)

print('\n' + '=' * 50)
print('通过 %d / 失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
