# -*- coding: utf-8 -*-
"""双激光（/scan、/depth2laser）与深度相机 3D 点云的单元测试。"""
import os, sys, math, struct, types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "apps", "statusweb"))
import stat_agent as A

FAIL = []


def ck(name, cond, extra=""):
    print(("ok  " if cond else "FAIL") + "  " + name + ("  " + extra if extra else ""))
    if not cond:
        FAIL.append(name)


def mk_scan(n=8, r=1.0, frame=""):
    return types.SimpleNamespace(
        ranges=[r] * n, angle_min=0.0,
        angle_increment=2 * math.pi / n, angle_max=2 * math.pi,
        range_min=0.0, range_max=10.0,
        header=types.SimpleNamespace(frame_id=frame))


# ── 两个激光话题独立 ──
# 默认关（车上资源考虑，只有用户勾选才订阅）。测试里显式打开，
# 否则回调会把数据丢掉 —— 这不是 bug，是本次要的行为。
A.LASER_ENABLE[A.SCAN_TOPIC][0] = True
A.LASER_ENABLE[A.D2L_TOPIC][0] = True
A.PC3D_ENABLE[0] = True
A._laser_cb("/scan", mk_scan(8, 1.0))
A._laser_cb(A.D2L_TOPIC, mk_scan(8, 2.0))
s1 = A.LASER_STATE["/scan"]
s2 = A.LASER_STATE[A.D2L_TOPIC]
ck("/scan 存了 8 点", s1["n"] == 8, "n=%s" % s1["n"])
ck("depth2laser 存了 8 点", s2["n"] == 8, "n=%s" % s2["n"])
ck("两者半径不同（互不覆盖）",
   abs(abs(s1["pts"][0][0]) - 1.0) < 1e-6
   and abs(abs(s2["pts"][0][0]) - 2.0) < 1e-6,
   "%s / %s" % (s1["pts"][0], s2["pts"][0]))
ck("topic 字段各自正确",
   A.d2l_snapshot()["topic"] == "/depth2laser"
   and A.scan_snapshot()["topic"] == "/scan")

# inf / nan 必须被丢掉
bad = mk_scan(8, 1.0)
bad.ranges = [1.0, float("inf"), float("nan"), 1.0, 0.0, 1.0, 1.0, 1.0]
A._laser_cb("/scan", bad)
ck("inf/nan/0 被过滤", A.LASER_STATE["/scan"]["n"] == 5,
   "n=%s" % A.LASER_STATE["/scan"]["n"])


# ── 深度图反投影 ──
K = [100.0, 0.0, 1.5, 0.0, 100.0, 1.5, 0.0, 0.0, 1.0]


def mk_depth(w, h, enc, vals):
    if enc == "32FC1":
        raw = struct.pack("<%df" % (w * h), *vals)
    else:
        raw = struct.pack("<%dH" % (w * h), *vals)
    return types.SimpleNamespace(width=w, height=h, encoding=enc,
                                 data=raw, step=w * 2)


def run(w, h, enc, vals):
    A.DEPTH_IMG["msg"] = mk_depth(w, h, enc, vals)
    A.DEPTH_INFO["k"] = K
    return A._pc3d_compute()


# 32FC1：单位是米，z 直接用
pts, err = run(4, 4, "32FC1", [2.0] * 16)
ck("32FC1 反投影成功", bool(pts) and not err, err)
# u=0,v=0 -> x=(0-1.5)*2/100=-0.03  y 同  z=2.0
ck("32FC1 首点坐标", bool(pts) and pts[0] == -0.03 and pts[1] == -0.03
   and pts[2] == 2.0, str(pts[:3] if pts else None))
ck("32FC1 点数 = 16", bool(pts) and len(pts) // 3 == 16,
   str(len(pts) // 3 if pts else 0))

# 16UC1：单位是毫米，要除 1000
pts2, err2 = run(4, 4, "16UC1", [1000] * 16)
ck("16UC1 反投影成功", bool(pts2) and not err2, err2)
ck("16UC1 毫米换算成米", bool(pts2) and pts2[2] == 1.0
   and pts2[0] == -0.015, str(pts2[:3] if pts2 else None))

# 超出量程的点要丢掉
pts3, _ = run(4, 4, "32FC1", [99.0] * 16)
ck("超量程点被丢弃", not pts3)

# 缺内参 / 缺图 要给出明确原因
# 注意：run() 会重新灌内参，这里要直接调，不能再走 run()
A.DEPTH_IMG["msg"] = mk_depth(4, 4, "32FC1", [1.0] * 16)
A.DEPTH_INFO["k"] = None
p4, e4 = A._pc3d_compute()
ck("缺内参有明确报错", (not p4) and ("camera_info" in (e4 or "")), e4 or "")
A.DEPTH_INFO["k"] = K
A.DEPTH_IMG["msg"] = None
p5, e5 = A._pc3d_compute()
ck("缺深度图有明确报错", (not p5) and ("image_raw" in (e5 or "")), e5 or "")

# 未开启时快照必须说清楚，而不是给空数据让人以为坏了
A.PC3D_ENABLE[0] = False
sn = A.pc3d_snapshot()
ck("未开启时快照有说明", (not sn["ok"]) and ("未开启" in (sn["err"] or "")),
   sn["err"])
A.PC3D_ENABLE[0] = True
A.PC3D_STATE["ok"] = True
A.PC3D_STATE["pts"] = [0.0, 0.0, 1.0]
A.PC3D_STATE["n"] = 1
A.PC3D_STATE["at"] = __import__("time").time()
sn2 = A.pc3d_snapshot()
ck("开启后快照带点", sn2["ok"] and sn2["n"] == 1 and len(sn2["pts"]) == 3)

print("")
print("FAIL %d" % len(FAIL))
sys.exit(1 if FAIL else 0)
