# -*- coding: utf-8 -*-
"""
远程机器状态监控 —— 版本信息测试。

三个来源，两种机制，所以要分开验：
    软件版本  读文件 version.yaml   —— 不依赖 ROS，PyYAML 有没有都得行
    SLAM版本  订阅 /slam_a_status   —— 走 rospy，和故障/硬件共用同一个线程
    （固件版本来自 /hardware_state，已在故障那套测试里覆盖）

重点：
    1) 订阅线程里【三个话题】都要挂上 —— rospy.init_node 只能成功一次，
       加第三个话题时最容易漏的就是"忘了在 _ros_thread 里登记"。
    2) version.yaml 在没有 PyYAML 的车上也要能读出来。
"""
import os
import sys
import types
import time
import shutil
import tempfile

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
    spec = importlib.util.spec_from_file_location('stat_agent_v', AGENT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── 假 rospy ───────────────────────────────────────────
SUBS = []          # [(topic, cls, cb)]
PUBLISHED = {'topics': []}


def make_fake_rospy():
    m = types.ModuleType('rospy')
    m.init_node = lambda name, anonymous=False, disable_signals=False: None
    m.get_published_topics = lambda namespace='/': list(PUBLISHED['topics'])
    m.spin = lambda: None

    def Subscriber(topic, cls, cb, **kw):
        SUBS.append((topic, cls, cb))
        return None
    m.Subscriber = Subscriber
    return m


class SlamMsg(object):
    """按 rostopic echo /slam_a_status 的真实字段造。"""
    __slots__ = ['docker_image', 'docker_tag', 'sw_version', 'sn',
                 'axbot_status']

    def __init__(self, sw='2.7.5-linux64'):
        self.docker_image = 'axbot:1.0.0'
        self.docker_tag = ''
        self.sw_version = sw
        self.sn = 'A010FFFF11968FA4E682808'
        self.axbot_status = None


class FaultsMsg(object):
    __slots__ = ['alarms', 'alarm_level']

    def __init__(self):
        self.alarms = []
        self.alarm_level = 0


class HwMsg(object):
    __slots__ = ['Version', 'bat_state', 'bat_voltage']

    def __init__(self):
        self.Version = '55_V4_03_08_P2.6'
        self.bat_state = 1
        self.bat_voltage = 25.8


def install_msg_module(modname, clsname, cls):
    pkg = types.ModuleType(modname)
    msg = types.ModuleType(modname + '.msg')
    setattr(msg, clsname, cls)
    pkg.msg = msg
    sys.modules[modname] = pkg
    sys.modules[modname + '.msg'] = msg


def install_v3():
    """三个话题里有两个是 v3_msgs，不装假模块它们会走"消息类加载失败"
    分支 —— 那是别处该验的事，这里要验的是"三个都能挂上"。
    """
    pkg = types.ModuleType('v3_msgs')
    msg = types.ModuleType('v3_msgs.msg')
    setattr(msg, 'DiagnoseFaults', FaultsMsg)
    setattr(msg, 'HardwareState', HwMsg)
    pkg.msg = msg
    sys.modules['v3_msgs'] = pkg
    sys.modules['v3_msgs.msg'] = msg


def run_ros_thread(agent, timeout=8.0):
    t = agent.start_ros()
    end = time.time() + timeout
    while time.time() < end:
        if len(SUBS) >= len(PUBLISHED['topics']):
            break
        time.sleep(0.05)
    time.sleep(0.2)
    return t


# ══════════════════════════════════════════════════════
sec('1. version.yaml —— 用户给的真实内容')
agent = load_agent()

tmp = tempfile.mkdtemp()
ws = os.path.join(tmp, 'home', 'aoting', 'hrg_bee_dev_v3')
os.makedirs(ws)
with open(os.path.join(ws, 'version.yaml'), 'w') as f:
    f.write('version: V3.2.11.0_gitdev\n')

# 把临时目录挂到候选路径上（脚本里写死 /home/aoting，测试期改一下常量）
agent.SWVER_HOMES = (os.path.join(tmp, 'home', 'aoting'),)
agent._swver_refresh()
snap = agent.swver_snapshot()
chk('读到软件版本', snap['version'] == 'V3.2.11.0_gitdev', repr(snap['version']))
chk('标记为成功', snap['ok'] is True)
chk('无错误', snap['err'] == '', snap['err'])
chk('来源路径带出来', snap['src'].endswith('hrg_bee_dev_v3/version.yaml'),
    snap['src'])

sec('2. 没有 PyYAML 的车（按行匹配回退）')
real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') \
    else __import__


class NoYaml(object):
    """让 import yaml 失败，验证回退路径真的能走通。"""
    def find_module(self, name, path=None):
        return self if name == 'yaml' else None

    def load_module(self, name):
        raise ImportError('no yaml')


sys.meta_path.insert(0, NoYaml())
sys.modules.pop('yaml', None)
agent._swver_refresh()
snap2 = agent.swver_snapshot()
chk('仍能读出版本', snap2['version'] == 'V3.2.11.0_gitdev', repr(snap2['version']))
chk('仍标记成功', snap2['ok'] is True)
sys.meta_path = [m for m in sys.meta_path if not isinstance(m, NoYaml)]
sys.modules.pop('yaml', None)

sec('3. 各种畸形文件')
agent._parse_swver


class P(object):
    pass


chk('带双引号能去壳',
    agent._parse_swver('version: "V1.2.3"\n') == 'V1.2.3',
    repr(agent._parse_swver('version: "V1.2.3"\n')))
chk('值里含冒号不丢',
    agent._parse_swver('version: V1.0:build7\n') == 'V1.0:build7')
chk('只有注释返回空', agent._parse_swver('# nothing\n') == '')
chk('没有 version 键返回空',
    agent._parse_swver('other: 1\nversion_x: 2\n') == '')
chk('空文件返回空', agent._parse_swver('') == '')

sec('4. 文件不存在 / 没有 version 字段 —— 要说清原因')
agent.SWVER_HOMES = (os.path.join(tmp, 'nope'),)
agent._swver_refresh()
s3 = agent.swver_snapshot()
chk('标记失败', s3['ok'] is False)
chk('错误里点名工作空间', 'hrg_bee_dev_v3' in s3['err'], s3['err'])

ws2 = os.path.join(tmp, 'home2', 'aoting', 'aotingos')
os.makedirs(ws2)
with open(os.path.join(ws2, 'version.yaml'), 'w') as f:
    f.write('build: 123\n')
agent.SWVER_HOMES = (os.path.join(tmp, 'home2', 'aoting'),)
agent._swver_refresh()
s4 = agent.swver_snapshot()
chk('找不到 version 字段时报出来', ('version' in s4['err']) and s4['ok'] is False,
    s4['err'])

shutil.rmtree(tmp, ignore_errors=True)

# ══════════════════════════════════════════════════════
sec('5. SLAM 话题订阅（三话题共用一条 init_node）')
SUBS[:] = []
sys.modules['rospy'] = make_fake_rospy()
install_v3()
install_msg_module('slam_msgs', 'SlamStatus', SlamMsg)
PUBLISHED['topics'] = [
    ('/diagnose_faults', 'v3_msgs/DiagnoseFaults'),
    ('/hardware_state', 'v3_msgs/HardwareState'),
    ('/slam_a_status', 'slam_msgs/SlamStatus'),
]
# 故障/硬件的消息类不必真的能 import —— 只要 SLAM 那一路能通即可。
# 但 _load_msg_class 失败只是那一块报错，不影响别的：正好顺便验这一点。
agent2 = load_agent()
agent2.SLAM_STATE.update({'ok': False, 'err': '', 'fields': {},
                          'at': 0, 'type': ''})
run_ros_thread(agent2)

topics = [t for t, _c, _b in SUBS]
chk('三个话题都订阅上了', len(SUBS) == 3, str(topics))
chk('SLAM 话题在里面', '/slam_a_status' in topics, str(topics))
chk('SLAM 拿到正确的消息类',
    any(t == '/slam_a_status' and c is SlamMsg for t, c, _b in SUBS))

cb = None
for t, _c, b in SUBS:
    if t == '/slam_a_status':
        cb = b
chk('SLAM 回调已注册', cb is not None)

sec('6. SLAM 回调 → sw_version')
cb(SlamMsg('2.7.5-linux64'))
snap3 = agent2.slam_snapshot()
chk('标记为已获取', snap3['ok'] is True)
chk('无错误', snap3['err'] == '', snap3['err'])
chk('sw_version 取到', (snap3['fields'] or {}).get('sw_version') == '2.7.5-linux64',
    repr((snap3['fields'] or {}).get('sw_version')))
chk('消息类型带出来', snap3['type'] == 'slam_msgs/SlamStatus', snap3['type'])
chk('话题名带出来', snap3['topic'] == '/slam_a_status')

sec('7. SLAM 话题没发布 —— 只影响自己那一格')
SUBS[:] = []
PUBLISHED['topics'] = [('/diagnose_faults', 'v3_msgs/DiagnoseFaults')]
agent3 = load_agent()
agent3.SLAM_STATE.update({'ok': False, 'err': '', 'fields': {},
                          'at': 0, 'type': ''})
# 缩短等待：_wait_topic 默认 10 次 * 1 秒，测试里没必要真等。
# _setup_topic 内部按模块全局名调用 _wait_topic，改模块属性即生效。
def fast_wait(rospy, topic, tries=1, gap=0.01):
    for name, typ in rospy.get_published_topics():
        if name == topic:
            return typ
    return ''


agent3._wait_topic = fast_wait
run_ros_thread(agent3)
s5 = agent3.slam_snapshot()
chk('未发布时不谎报有数据', s5['ok'] is False)
chk('错误里点明话题名', '/slam_a_status' in (s5['err'] or ''), s5['err'])

# ══════════════════════════════════════════════════════
print('\n' + '=' * 46)
print('  通过 %d，失败 %d' % (PASS, FAIL))
print('=' * 46)
sys.exit(1 if FAIL else 0)
