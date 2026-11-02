# -*- coding: utf-8 -*-
"""实时位姿（仿车上 C++ RealtimePose）的单元测试。

重点不是"能不能算"，而是"算错的几种典型方式有没有被堵住"：
  1. 必须先 waitForTransform 再 lookupTransform（否则 listener 缓存空
     时必然抛异常，一路退回 odom 系，跟 map 差十几米）
  2. 坐标系名从 ROS 参数读，不能硬编码
  3. 独立线程维护缓存，偶发一次查不到不降级
  4. 缓存超过 4 秒才算 stale
  5. 缓存真的空了才退回 /odom_origin
"""
import os
import sys
import types
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "apps", "statusweb"))

CALLS = {"wait": 0, "look": 0, "params": {}}
FAIL_ONCE = {"n": 0}


class FakeListener(object):
    def waitForTransform(self, a, b, t, d):
        CALLS["wait"] += 1
        return None

    def lookupTransform(self, a, b, t):
        CALLS["look"] += 1
        if FAIL_ONCE["n"] > 0:
            FAIL_ONCE["n"] -= 1
            raise RuntimeError("tf cache empty")
        # base_link 在 map 下：x=11.264, y=-0.6976, yaw=0.7277
        return (11.2643566132, -0.69761633873, 0.0), (0.0, 0.0, 0.3559, 0.9345)


def _install_fakes():
    rospy = types.ModuleType("rospy")
    rospy.Time = lambda *a, **k: 0
    rospy.Duration = lambda *a, **k: 0
    rospy.get_param = lambda k, d=None: CALLS["params"].get(k, d)
    rospy.init_node = lambda *a, **k: None
    rospy.Rate = lambda hz: types.SimpleNamespace(sleep=lambda: None)
    rospy.logwarn = lambda *a: None
    sys.modules["rospy"] = rospy

    tfmod = types.ModuleType("tf")
    tfmod.TransformListener = lambda *a, **k: FakeListener()
    tr = types.ModuleType("tf.transformations")
    import math
    tr.euler_from_quaternion = lambda q: (
        0.0, 0.0,
        math.atan2(2 * (q[3] * q[2] + q[0] * q[1]),
                   1 - 2 * (q[1] * q[1] + q[2] * q[2])))
    tfmod.transformations = tr
    sys.modules["tf"] = tfmod
    sys.modules["tf.transformations"] = tr


def load_agent():
    _install_fakes()
    import stat_agent
    return stat_agent


def main():
    ag = load_agent()
    ag.TF_LISTENER = FakeListener()
    ok = fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ok  " + name)
        else:
            fail += 1
            print("  FAIL " + name)

    print("[1] _tf_lookup 先 wait 再 lookup")
    CALLS["wait"] = CALLS["look"] = 0
    r = ag._tf_lookup("map", "base_link")
    chk("waitForTransform 被调用", CALLS["wait"] == 1)
    chk("lookupTransform 被调用", CALLS["look"] == 1)
    chk("拿到 x", r is not None and abs(r[0] - 11.264) < 1e-3)
    chk("拿到 y", r is not None and abs(r[1] + 0.6976) < 1e-3)
    chk("yaw 由四元数换算", r is not None and abs(r[2] - 0.7277) < 1e-3)

    print("[2] 坐标系名从 ROS 参数读")
    CALLS["params"] = {"/map_frame": "map2", "/base_frame": "base2"}
    ag._read_frames()
    chk("map_frame 取自参数", ag.MAP_FRAME == "map2")
    chk("base_frame 取自参数", ag.BASE_FRAME == "base2")
    CALLS["params"] = {}
    ag._read_frames()
    chk("无参数回落 map", ag.MAP_FRAME == "map")
    chk("无参数回落 base_link", ag.BASE_FRAME == "base_link")

    print("[3] _pose_update_once 刷新缓存")
    chk("更新成功", ag._pose_update_once() is True)
    ag.POSE_CACHE["frame"] = "map"
    got = ag._robot_pose_map()
    chk("缓存命中 x", got and abs(got["x"] - 11.264) < 1e-3)
    chk("src=tf", got and got["src"] == "tf")

    print("[4] 偶发一次查不到不降级（用上一次的值）")
    FAIL_ONCE["n"] = 1
    ag._pose_update_once()
    got = ag._robot_pose_map()
    chk("仍是 TF 值而非 odom", got and got["src"] == "tf")
    chk("值未被清空", got and abs(got["x"] - 11.264) < 1e-3)

    print("[5] 缓存超 4 秒标 stale")
    ag.POSE_CACHE["at"] = time.time() - 9.0
    got = ag._robot_pose_map()
    chk("src=tf_stale", got and got["src"] == "tf_stale")
    chk("带 age", got and got.get("age", 0) >= 8)

    print("[6] 缓存空才退回 /odom_origin")
    ag.POSE_CACHE["x"] = None
    ag.ODOM_STATE["pose"] = [1.0, 2.0, 0.5]
    got = ag._robot_pose_map()
    chk("src=odom", got and got["src"] == "odom")
    chk("用的是 odom 值", got and abs(got["x"] - 1.0) < 1e-6)

    print("")
    print("通过 %d / 失败 %d" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
