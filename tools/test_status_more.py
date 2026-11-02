# -*- coding: utf-8 -*-
"""v1.0.34 采集端测试：磁盘 / 温度频率 / 实际速度 / 定位 / 自定义话题

重点盯这几件事：
  1) 磁盘：整机容量算得对；du 统计不能卡主循环（单独线程 + 缓存）
  2) 实际速度 /odom_origin 与 期望速度 /cmd_vel 是【两个话题】，
     只有对比才能发现"指令发了车没动"
  3) 定位 /ax_slam_state 全量收，不写死字段
  4) 自定义话题：内容必须被截断且【如实说明截断了】，不能悄悄少给
"""
import os, sys, time, threading

sys.path.insert(0, '/data/workspace/aoting_tools/apps/statusweb')

PASS = FAIL = 0
def ck(cond, name, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1; print('ok   ' + name)
    else:
        FAIL += 1; print('FAIL ' + name + ('  | ' + str(extra) if extra else ''))

import stat_agent as A

# ═══════════════ 磁盘 ═══════════════
d = A.disk_total('/')
ck(isinstance(d, dict), 'disk_total 返回 dict', d)
if d:
    ck(d['total'] > 0 and d['free'] >= 0, '总容量为正', d)
    ck(0 <= d['pct'] <= 100, '占用百分比在 0~100', d['pct'])
    ck(abs(d['used'] + d['free'] - d['total']) <= 2,
       '已用 + 可用 ≈ 总量', (d['used'], d['free'], d['total']))
ck(A.disk_total('/no/such/path') is None, '不存在的挂载点返回 None')

r = A._du_one('/no/such/dir')
ck(r['ok'] is False and '目录不存在' in r['err'], '不存在的目录明确报错', r)

# du 用临时目录跑，避免真去统计 /usr（太慢）
import tempfile
tmp = tempfile.mkdtemp()
with open(os.path.join(tmp, 'a.bin'), 'wb') as f:
    f.write(b'0' * 4096)
A.DU_DIRS = (tmp, '/no/such/dir')
A.du_refresh()
snap = A.du_snapshot()
ck(len(snap['items']) == 2, 'du 统计了配置的每个目录', snap['items'])
ck(snap['items'][0]['ok'] and snap['items'][0]['bytes'] >= 4096,
   '存在的目录拿到字节数', snap['items'][0])
ck(snap['items'][1]['ok'] is False, '不存在的目录标记失败', snap['items'][1])
ck(snap['busy'] is False and snap['at'] > 0, '统计完成后 busy 复位')

# ═══════════════ 温度 / 频率 ═══════════════
th = A.cpu_thermal()
ck(isinstance(th, dict) and 'ok' in th, 'cpu_thermal 返回带 ok 的 dict', th)
ck(th['ok'] is False or (isinstance(th.get('c'), float) and 0 <= th['c'] < 200),
   '温度值合理或明确说取不到', th)
fq = A.cpu_freq()
ck(isinstance(fq, dict) and 'ok' in fq, 'cpu_freq 返回带 ok 的 dict', fq)
ck(fq['ok'] is False or fq['mhz'] > 0, '频率为正或明确说取不到', fq)

# ═══════════════ 实际速度 /odom_origin ═══════════════
class Vec(object):
    __slots__ = ('x', 'y', 'z')
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z

class Tw(object):
    __slots__ = ('linear', 'angular')

class Twist(object):
    __slots__ = ('twist',)
    def __init__(self, lx=0.0, az=0.0):
        t = Tw()
        t.linear = Vec(lx, 0.0, 0.0)
        t.angular = Vec(0.0, 0.0, az)
        self.twist = t

class Odom(object):
    __slots__ = ('twist', 'header', 'pose')
    def __init__(self, lx=0.0, az=0.0):
        self.twist = Twist(lx, az)

A._odom_cb(Odom(0.278382201195, 0.0139195507572))
o = A.odom_snapshot()
ck(o['topic'] == '/odom_origin', 'odom 话题名正确', o['topic'])
ck(abs(o['lin_x'] - 0.278382201195) < 1e-9, '取的是 twist.twist.linear.x', o['lin_x'])
ck(abs(o['ang_z'] - 0.0139195507572) < 1e-9, '取的是 twist.twist.angular.z', o['ang_z'])
ck(o['live'] is True, '刚收到消息算正在发布')

# ═══════════════ 卡住判定 ═══════════════
with A.CMDVEL_LOCK:
    A.CMDVEL_STATE.update({'lin_x': 0.5, 'at': time.time(), 'live': True})
with A.ODOM_LOCK:
    A.ODOM_STATE.update({'lin_x': 0.0, 'at': time.time(), 'live': True})
v = A.nav_verdict()
ck(v['ready'] is True, '两边都有数据才算可判定', v)
ck(v['stall'] is False and v['sec'] < 2, '刚出现不算卡住（防起步误报）', v)
# 把起始时刻往前推 3 秒，模拟"已经这样 3 秒了"
A.NAV_STALL[0] = time.time() - 3.0
with A.CMDVEL_LOCK:
    A.CMDVEL_STATE['lin_x'] = 0.5
    A.CMDVEL_STATE['at'] = time.time()
with A.ODOM_LOCK:
    A.ODOM_STATE['lin_x'] = 0.0
    A.ODOM_STATE['at'] = time.time()
v = A.nav_verdict()
ck(v['stall'] is True and v['sec'] >= 2, '持续 2 秒以上判定卡住', v)
ck(v['cmd'] == 0.5 and v['act'] == 0.0, '卡住时把期望/实际都带出来', v)

# 两边都在动 -> 不卡
with A.CMDVEL_LOCK:
    A.CMDVEL_STATE['lin_x'] = 0.5
    A.CMDVEL_STATE['at'] = time.time()
with A.ODOM_LOCK:
    A.ODOM_STATE['lin_x'] = 0.48
    A.ODOM_STATE['at'] = time.time()
v = A.nav_verdict()
ck(v['stall'] is False, '实际跟上期望时不报卡住', v)

# ═══════════════ 定位 /ax_slam_state ═══════════════
class AxSlam(object):
    __slots__ = ('state', 'reliable', 'position_quality', 'relocating',
                 'lidar_matching_score', 'wheel_slipping', 'lidar_reliable',
                 'position_loss_progress')
    def __init__(self):
        self.state = 2
        self.reliable = True
        self.position_quality = 10
        self.relocating = False
        self.lidar_matching_score = 0.666000008583
        self.wheel_slipping = False
        self.lidar_reliable = True
        self.position_loss_progress = 0.0

A._axslam_cb(AxSlam())
a = A.axslam_snapshot()
ck(a['topic'] == '/ax_slam_state', '定位话题名正确', a['topic'])
ck(a['fields'].get('state') == 2, 'state 收到', a['fields'].get('state'))
ck(a['fields'].get('reliable') is True, 'reliable 收到')
ck(a['fields'].get('position_quality') == 10, 'position_quality 收到')
ck(abs(a['fields'].get('lidar_matching_score') - 0.666) < 1e-6,
   'lidar_matching_score 收到')
ck(a['err'] == '', '解析无错误', a['err'])

# ═══════════════ 自定义话题：截断必须如实说明 ═══════════════
class Leaf(object):
    __slots__ = ('v',)
    def __init__(self, v):
        self.v = v

class Big(object):
    __slots__ = ('arr',)
    def __init__(self, n):
        self.arr = [Leaf(i) for i in range(n)]

flag = [False]
d1 = A._dict_limited(Big(5), 0, flag)
ck(len(d1['arr']) == 5 and flag[0] is False, '小数组不截断也不谎报', flag)

flag = [False]
d2 = A._dict_limited(Big(500), 0, flag)
ck(len(d2['arr']) == A.CUSTOM_MAX_ITEMS, '大数组按上限截断', len(d2['arr']))
ck(flag[0] is True, '截断会置标志位（前端据此提示）', flag)

# 层深超限
deep = Leaf(1)
for _i in range(8):
    nxt = Leaf(deep)
    deep = nxt
flag = [False]
d3 = A._dict_limited(deep, 0, flag)
ck(flag[0] is True, '层深超限也置标志位', flag)

# 自定义话题整条链路：假 rospy 起 watch 线程
class FakeSub(object):
    def __init__(self, ros, topic):
        self.ros, self.topic = ros, topic
    def unregister(self):
        self.ros.subs.pop(self.topic, None)

class FakeRos(object):
    def __init__(self, topics):
        self.topics = list(topics)
        self.subs = {}
    def get_published_topics(self):
        return list(self.topics)
    def Subscriber(self, topic, cls, cb, queue_size=None):
        s = FakeSub(self, topic)
        self.subs[topic] = cb
        return s

A._load_msg_class = lambda typ: Leaf      # 车上没有真消息包，直接给假的
ros = FakeRos([('/my_topic', 'fake/Leaf')])
with A.CUSTOM_LOCK:
    A.CUSTOM_WANT.clear()
    A.CUSTOM_STATE.clear()
    A.CUSTOM_WANT.add('/my_topic')
A.CUSTOM_SUB.clear()
t = threading.Thread(target=A._custom_watch, args=(ros,), daemon=True)
t.start()
time.sleep(2.2)
ck('/my_topic' in A.CUSTOM_SUB, '话题发布后自动订阅上', list(A.CUSTOM_SUB))
cb = ros.subs.get('/my_topic')      # CUSTOM_SUB 里存的是 Subscriber 对象
if cb:
    cb(Leaf(42))
    time.sleep(2.2)
    c = A.custom_snapshot().get('/my_topic') or {}
    ck(c.get('ok') is True, '收到消息后标记已订阅', c)
    ck(c.get('fields', {}).get('v') == 42, '内容原样带回来', c.get('fields'))
    ck(c.get('type') == 'fake/Leaf', '消息类型带回来', c.get('type'))
# 取消订阅后要能干净退订
with A.CUSTOM_LOCK:
    A.CUSTOM_WANT.discard('/my_topic')
time.sleep(1.6)
ck('/my_topic' not in A.CUSTOM_SUB, '取消后退订', list(A.CUSTOM_SUB))
ck('/my_topic' not in A.custom_snapshot(), '取消后快照里也移除')

# ═══════════════ 版本号一致性 ═══════════════
import json
app = json.load(open('/data/workspace/aoting_tools/apps/statusweb/app.json',
                     encoding='utf-8'))
ck(A.AGENT_VER == app['ver'],
   'AGENT_VER 与 app.json 的 ver 一致（不一致页面会提示旧版）',
   (A.AGENT_VER, app['ver']))

print('')
print('通过 %d / 失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
