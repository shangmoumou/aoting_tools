#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""雷达点云的 TF 换算（/scan -> 地图系）。

为什么单独测这个：点云画在地图上位置不对时，现场很难判断是
"坐标算错"还是"地图原点不对"。这里把换算本身钉死，
真机上再出问题就能直接排除这一环。

沙箱里没有 ROS，所以 rospy / tf 都是假的 —— 但被验的是
stat_agent 里真实的 _scan_cb 与 _tf_to_map 代码路径。
"""
from __future__ import print_function
import os
import sys
import math
import types

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), 'apps', 'statusweb')
sys.path.insert(0, APP)

OK = FAIL = 0


def ok(name, cond, extra=''):
    global OK, FAIL
    if cond:
        OK += 1
        print('  ok   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s %s' % (name, extra))


# ── 假 rospy / tf（必须在 import stat_agent 之前注入）──
rospy = types.ModuleType('rospy')


class _Time(object):
    def __init__(self, v=0):
        self.v = v


rospy.Time = _Time
sys.modules['rospy'] = rospy


class FakeListener(object):
    """map -> laser 固定为 平移(5,7) + 绕 z 旋转 90°。"""

    def lookupTransform(self, tgt, src, t):
        self.last = (tgt, src)
        qz = math.sin(math.pi / 4)
        qw = math.cos(math.pi / 4)
        return ((5.0, 7.0, 0.0), (0.0, 0.0, qz, qw))


class FakeTF(object):
    TransformListener = FakeListener

    class transformations(object):
        @staticmethod
        def euler_from_quaternion(q):
            return (0.0, 0.0, math.pi / 2)


sys.modules['tf'] = FakeTF

import stat_agent as A      # noqa: E402


class P(object):
    def __init__(s, x, y, z=0.0):
        s.x, s.y, s.z = x, y, z


class Q(object):
    def __init__(s):
        s.x = s.y = s.z = 0.0
        s.w = 1.0


class Hdr(object):
    def __init__(s, f):
        s.frame_id = f


class ScanMsg(object):
    """angle_min=-90°，每 45° 一个点。ranges 里故意混了脏值。"""

    def __init__(s, frame='laser'):
        s.header = Hdr(frame)
        s.angle_min = -math.pi / 2
        s.angle_increment = math.pi / 4
        s.angle_max = math.pi / 2
        s.range_min = 0.05
        s.range_max = 50.0
        s.ranges = [1.0, float('inf'), 2.0, float('nan'), 0.0, 100.0, 3.0]


print('[1] 没有 TF 时退回"相对车体"，前端用 /odom_origin 搬')
A.TF_LISTENER = None
A.ODOM_STATE['pose'] = (10.0, 20.0, 0.0)
A.LASER_ENABLE[A.SCAN_TOPIC][0] = True
A._laser_cb(A.SCAN_TOPIC, ScanMsg())
sn = A.scan_snapshot()
ok('ok=True', sn['ok'] is True)
ok('tf_ok=False（没有 TF）', sn['tf_ok'] is False, str(sn['tf_ok']))
ok('pose 原样带出', sn['pose'] == [10.0, 20.0, 0.0], str(sn['pose']))
ok('脏值被过滤（inf/nan/0/超量程）', sn['n'] == 3, 'n=%s' % sn['n'])
# 首个点角度 = angle_min = -90°，距离 1m -> (0, -1)
ok('点未被旋转', abs(sn['pts'][0][0]) < 1e-6 and abs(sn['pts'][0][1] + 1.0) < 1e-6,
   str(sn['pts'][0]))

print('[2] 有 TF 时点被搬到地图系')
# TF 不再依赖 tf 包：v1.0.46 起改成自己订阅 /tf 拼树（车上 import tf 失败过）。
# 所以这里要往自研缓冲里塞边，塞 TF_LISTENER 已经没用了。
_yaw = math.pi / 2
A.TF_STA[('map', 'laser')] = {
    't': (5.0, 7.0, 0.0),
    'q': (0.0, 0.0, math.sin(_yaw / 2), math.cos(_yaw / 2)),
}
A.TF_BUF_DIRTY[0] = True
A.TF_LISTENER = None
A._laser_cb(A.SCAN_TOPIC, ScanMsg('laser'))
sn = A.scan_snapshot()
ok('tf_ok=True', sn['tf_ok'] is True)
ok('tf_frame 标注来源', sn['tf_frame'] == 'map <- laser', str(sn['tf_frame']))
# (0,-1) 旋转 90° -> (1, 0)；平移 (5,7) -> (6, 7)
ok('旋转+平移正确', abs(sn['pts'][0][0] - 6.0) < 1e-6
   and abs(sn['pts'][0][1] - 7.0) < 1e-6, str(sn['pts'][0]))

print('[3] 旋转不得污染下一个分量（bx 覆盖 bug 的回归）')
# 若写成 bx = T[0]+bx*ca-by*sa 再算 by，by 就会用到已旋转的 bx。
# 这里用一个非对称点把它钉死：只有先存临时量才可能对。
T = (1.0, 2.0, math.pi / 2)
bx, by = 2.0, 3.0
ca, sa = math.cos(T[2]), math.sin(T[2])
wx = T[0] + bx * ca - by * sa
wy = T[1] + bx * sa + by * ca
buggy = T[1] + wx * sa + by * ca          # 若 bx 被覆盖会得到的错值
ok('源码用临时量（与错误写法不同）', abs(wy - buggy) > 1e-6,
   '正确=%s 错误=%s' % (round(wy, 3), round(buggy, 3)))
ok('手算值一致', abs(wx + 2.0) < 1e-6 and abs(wy - 4.0) < 1e-6,
   '(%s, %s)' % (round(wx, 3), round(wy, 3)))

print('[4] frame_id 为空时不强求 TF')
A.TF_LISTENER = FakeListener()
A._laser_cb(A.SCAN_TOPIC, ScanMsg(''))
sn = A.scan_snapshot()
ok('空 frame 不崩且 tf_ok=False', sn['ok'] and sn['tf_ok'] is False,
   str(sn['tf_ok']))

print('[5] 采集脚本仍遵守单引号禁令（shell 多层引用会变形）')
src = open(os.path.join(APP, 'stat_agent.py'), encoding='utf-8').read()
ok('全文无单引号', src.count("'") == 0, '出现 %d 次' % src.count("'"))

print('[6] 前端按 tf_ok 决定要不要自己旋转')
html = open(os.path.join(APP, 'index.html'), encoding='utf-8').read()
ok('前端读 tf_ok', '.tf_ok' in html)
ok('TF 模式下不套 pose 旋转', 'inMap' in html)
ok('界面标注坐标来源', '坐标来自 TF' in html)

print('=' * 50)
print(' 通过 %d  失败 %d' % (OK, FAIL))
print('=' * 50)
sys.exit(1 if FAIL else 0)
