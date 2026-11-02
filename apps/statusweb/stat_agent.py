#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
stat_agent.py —— 跑在【车上】的状态采集脚本（只读 /proc，不装任何依赖）

AGENT_VER 必读：
    这份脚本的源码由 PC 端通过 SSH 下发到车上执行，车上【不留文件】。
    所以「改了脚本但没生效」的排查永远先问一句：车上跑的是哪一版？
    前端会把 AGENT_VER 与页面版本比对，不一致就提示重启本地服务。
    新增任何字段时都必须同步改 AGENT_VER，否则线上分不清新旧。

输出协议（stdout，一行一个 JSON，每秒一行）：
    {"t":unix秒, "host":主机名, "cpu":整机占用%, "mem_used":MB, "mem_total":MB,
     "load":[1分,5分,15分], "up":运行秒数, "nproc":进程数,
     "top_cpu":[{"pid":..,"name":..,"cpu":..,"rss":MB}, ...]  前 5
     "top_mem":[同上, 按内存排]  前 5
     "faults":{故障话题 /diagnose_faults}
     "hw":{硬件状态 /hardware_state}
     "slam":{SLAM 版本 /slam_a_status}
     "swver":{软件版本 version.yaml}
     "nav":{导航状态 /cmd_vel 的线速度 / 角速度}}

【为什么要自己算 CPU，而不是用 ps 的 %CPU】
    ps 报的 %CPU 是【进程从启动到现在的平均占用】。一个跑了三小时的
    进程，哪怕此刻满载，平均值也会被摊得很低；反过来一个刚启动的
    进程平均值会虚高。看实时状态必须用【两次采样的差值】。

    做法：读 /proc/<pid>/stat 的 utime+stime（累计时钟滴答），
    间隔 1 秒采两次，差值 / 整机总滴答差 * 核数 * 100。
    这与 top 的算法一致，且不需要任何第三方库。

【单引号禁令】
    本脚本会被 shell_quote 用单引号包住，经 paramiko 送到远端 bash。
    一旦正文里出现单引号，shell_quote 会把它转义成嵌套形式，
    经过 paramiko 再到远端 bash，多层引用后极易变形
    （实测出现过 unterminated quoted string）。
    所以全文只用双引号。改这个文件时必须遵守。

【为什么不使用 walrus := / f-string】
    车上的 python 可能是 2.7 或 3.5，语法一新就整个脚本起不来，
    症状是"连上了但没数据"，极难排查。
"""

from __future__ import print_function

import os
import sys
import time
import struct
import json
import threading
import math
import signal
import stat

TAG = "aoting_stat_agent"

# 本进程最长存活时间（秒）。到点自己退出，不等 PC 端来杀。
#
# 为什么必须有这个兜底：过去几版一直在 PC 端想办法（pkill -f → 按 pid
# → 按 ppid → rosnode kill），但只要 PC 端那条 kill 命令没发出去
# （线程卡住 / SSH 已不通 / 用户直接关黑窗口），车端就永远残留。
# 与其把宝押在"清理一定能跑到"，不如让车端自己到点就走 —— 这是唯一
# 不依赖对端行为的保证。
#
# 600 秒 = 10 分钟：默认单次上限 300 秒，留足余量；用户点"延长"时
# PC 端会下发 {"life":1} 把 deadline 往后推。
MAX_LIFE_SEC = 600
LIFE_DEADLINE = [0.0]      # 由 main 设置为 time.time() + MAX_LIFE_SEC

# 脚本版本。必须与 apps/statusweb/app.json 的 ver 保持一致。
# 前端拿它和页面版本比对：不一致说明车上跑的是旧脚本
# （典型原因：8766 的本地服务是旧进程，或用户没重新点连接）。
AGENT_VER = "1.53"


def log(msg):
    try:
        sys.stderr.write("[stat] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def _ncpu():
    try:
        n = os.cpu_count()
        if n:
            return n
    except Exception:
        pass
    try:
        return int(os.sysconf("SC_NPROCESSORS_ONLN"))
    except Exception:
        return 1


def _page():
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except Exception:
        return 4096


NCPU = _ncpu()
PAGE = _page()
SELF = os.getpid()
T0 = time.time()


def total_pair():
    """/proc/stat 第一行：(总滴答, 空闲滴答)"""
    try:
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return sum(v), idle
    except Exception:
        return 0, 0


def meminfo():
    m = {}
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                p = ln.split(":")
                if len(p) >= 2:
                    try:
                        m[p[0]] = int(p[1].split()[0])
                    except Exception:
                        pass
    except Exception:
        pass
    return m


def _argv(pid):
    """/proc/<pid>/cmdline：参数以 NUL 分隔，末尾通常还有一个 NUL。"""
    try:
        with open("/proc/" + str(pid) + "/cmdline", "rb") as f:
            raw = f.read()
    except Exception:
        return []
    out = []
    for p in raw.split(b"\0"):
        p = p.strip()
        if p:
            out.append(p.decode("utf-8", "replace"))
    return out


def _node_from_argv(argv):
    """从命令行猜出「这是哪个 ROS 节点」。

    三个来源，按可靠程度从高到低：
      1) roslaunch / rosrun 一定会追加 __name:=xxx，最准；
      2) python xxx.py 直接跑的，取脚本名（去掉 .py / .sh）；
      3) python -m pkg 这种，取 -m 后面那个包名。
    都取不到就返回空，前端退回显示进程名。
    """
    for a in argv:
        if a.startswith("__name:="):
            v = a[len("__name:="):].strip()
            if v:
                return v
    for i, a in enumerate(argv):
        if a == "-m" and i + 1 < len(argv):
            v = argv[i + 1].strip()
            if v:
                return v
    for a in reversed(argv):
        base = a.rsplit("/", 1)[-1]
        if base.endswith(".py") or base.endswith(".sh"):
            return base[:-3]
    return ""


def _node_of(pid, name):
    """只对 python 进程读 cmdline。

    C++ 节点的进程名本身就是节点名（deep_perception 之类），读它没意义；
    真正看不出身份的就是一堆同名 python，这正是要解决的问题。
    顺带也省掉了每秒几百次无谓的 open()。
    """
    if not name.lower().startswith("python"):
        return ""
    try:
        return _node_from_argv(_argv(pid))
    except Exception:
        return ""


# pid -> (进程名, ROS 节点名)。
# 进程名和命令行一旦启动就不变，没必要每秒重读 —— 车上几百个进程，
# 每次全量 snapshot 都要为 python 进程额外 open 一遍 cmdline，
# 这是采集脚本空载时最大的一笔开销。只在【新 pid】或【进程名变了】
# （exec 改名）时才真正去读。
_PROC_META = {}


def snapshot():
    """返回 {pid: (name, ticks, rss_bytes, node)}"""
    d = {}
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return d
    seen = set()
    for p in pids:
        try:
            with open("/proc/" + p + "/stat") as f:
                s = f.read()
            # 进程名可能含空格和括号，用最后一个右括号定位
            i = s.index("(")
            j = s.rindex(")")
            name = s[i + 1:j]
            rest = s[j + 2:].split()
            if len(rest) < 22:
                continue
            ticks = int(rest[11]) + int(rest[12])
            rss = int(rest[21]) * PAGE
            pid = int(p)
            seen.add(pid)
            meta = _PROC_META.get(pid)
            if meta is None or meta[0] != name:
                # 新进程，或进程被 exec 改名了 —— 重新认一次节点
                node = _node_of(pid, name)
                _PROC_META[pid] = (name, node)
            else:
                node = meta[1]
            d[pid] = (name[:32], ticks, rss, node)
        except Exception:
            continue
    # 清掉已经退出的进程，否则缓存会随车辆运行时间一直涨
    for pid in [k for k in _PROC_META if k not in seen]:
        _PROC_META.pop(pid, None)
    return d


def loadavg():
    try:
        with open("/proc/loadavg") as f:
            return [round(float(x), 2) for x in f.read().split()[:3]]
    except Exception:
        return []


def uptime():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def boot_ts():
    """系统开机时刻（epoch 秒）。

    单看「运行了多久」没有锚点：显示 6 天时，用户无法判断这是
    「这台机器确实 6 天没重启」还是「连到了别的机器」。带上开机
    时刻就能自查 —— 开机日期跟自己开机的时间对不上，基本就是端口
    指到了另一台。
    """
    up = uptime()
    if up <= 0:
        return 0
    return time.time() - up


def hostname():
    try:
        with open("/etc/hostname") as f:
            return f.read().strip()
    except Exception:
        try:
            import socket
            return socket.gethostname()
        except Exception:
            return ""



# ═══════════════ 温度 / 频率 ═══════════════
# 工控机夏天降频、死机前往往先升温。温度是唯一能提前看到的信号。
def cpu_thermal():
    """CPU / 主板温度（摄氏度）。

    读 sysfs 的 thermal_zone，取所有温区里最高的那个 —— 不同机器
    温区数量和含义都不一样（有的是 x86_pkg_temp，有的是 acpitz），
    都读一遍取最大值最稳；温区类型也带出来，方便判断是不是 CPU。
    temp 文件单位是毫摄氏度，要除以 1000。
    """
    vals = []
    base = "/sys/class/thermal"
    try:
        zones = sorted(os.listdir(base))
    except Exception:
        zones = []
    for z in zones:
        if not z.startswith("thermal_zone"):
            continue
        try:
            with open(base + "/" + z + "/temp") as f:
                v = float(f.read().strip()) / 1000.0
        except Exception:
            continue
        try:
            with open(base + "/" + z + "/type") as f:
                tp = f.read().strip()
        except Exception:
            tp = z
        # 有些温区会报 100 度上下的"未校准"值，也有把毫度当度读的；
        # 只挡明显不合理的，其余照实报
        if v > 200 or v < -50:
            continue
        vals.append((v, tp))
    if not vals:
        # 退路：hwmon（部分工控机只在这里暴露温度）
        hw = "/sys/class/hwmon"
        try:
            for h in sorted(os.listdir(hw)):
                for f in sorted(os.listdir(hw + "/" + h)):
                    if not f.endswith("_input") or not f.startswith("temp"):
                        continue
                    try:
                        with open(hw + "/" + h + "/" + f) as fh:
                            vals.append((float(fh.read().strip()) / 1000.0,
                                         h + "/" + f))
                    except Exception:
                        continue
        except Exception:
            pass
    if not vals:
        return {"ok": False, "err": "本机未暴露温度传感器"}
    vals.sort(key=lambda x: -x[0])
    return {"ok": True, "c": round(vals[0][0], 1), "src": vals[0][1],
            "n": len(vals)}


def cpu_freq():
    """CPU 当前频率（MHz），取所有核的平均；顺带读最大频率做参照。

    降频是温度过高 / 电源策略的直接后果 —— 只看温度不看频率，
    就看不出"已经烫到降频了"这件事。
    """
    base = "/sys/devices/system/cpu"
    cur = []
    try:
        for d in sorted(os.listdir(base)):
            if not (d.startswith("cpu") and d[3:].isdigit()):
                continue
            try:
                with open(base + "/" + d + "/cpufreq/scaling_cur_freq") as f:
                    cur.append(float(f.read().strip()) / 1000.0)
            except Exception:
                continue
    except Exception:
        pass
    mx = None
    try:
        with open(base + "/cpu0/cpufreq/cpuinfo_max_freq") as f:
            mx = round(float(f.read().strip()) / 1000.0)
    except Exception:
        pass
    if not cur:
        # 退路：虚拟机里常见，cpufreq 目录不存在但 cpuinfo 有 MHz
        try:
            with open("/proc/cpuinfo") as f:
                for ln in f:
                    if "MHz" in ln and ":" in ln:
                        try:
                            cur.append(float(ln.split(":", 1)[1].strip()))
                        except Exception:
                            pass
        except Exception:
            pass
    if not cur:
        return {"ok": False, "err": "本机未暴露 CPU 频率"}
    return {"ok": True, "mhz": round(sum(cur) / len(cur)),
            "max": mx, "n": len(cur)}


# ═══════════════ 磁盘 ═══════════════
# 录 bag 的机器磁盘满是高频事故，而且一出事就是连锁反应：
# roslaunch 起不来、日志写不进、系统卡死。一条 df 就能防住。
def disk_total(path="/"):
    """整机磁盘容量（MB）。用 os.statvfs，不依赖 df 命令。"""
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
    except Exception:
        return None
    if total <= 0:
        return None
    used = total - free
    return {"mount": path, "total": int(total / MB), "used": int(used / MB),
            "free": int(free / MB), "pct": round(used * 100.0 / total, 1)}


# 目录大小用 du 统计：/usr 这种几万个文件的目录要跑十几秒到一分钟，
# 绝不能放进 1Hz 主循环里（会把整秒的采集卡住）。
# 所以单独起线程算，结果缓存 5 分钟，中途页面显示"统计中"。
DU_DIRS = ("/var", "/usr", "/home/aoting", "/home/aoting/user_config")
DU_LOCK = threading.Lock()
DU_STATE = {"at": 0, "busy": False, "items": [], "err": "", "sec": 0}
DU_TTL = 300.0
DU_TIMEOUT = 120.0
DU_FORCE = [False]


def _du_one(d):
    """统计单个目录（字节）。du 输出形如 "123456\t/var"。"""
    import subprocess
    if not os.path.isdir(d):
        return {"dir": d, "ok": False, "err": "目录不存在", "bytes": 0}
    try:
        p = subprocess.Popen(["du", "-sb", d], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    except Exception as e:
        return {"dir": d, "ok": False, "err": "无法执行 du: " + str(e)[:40],
                "bytes": 0}
    t0 = time.time()
    while p.poll() is None:
        if time.time() - t0 > DU_TIMEOUT:
            try:
                p.kill()
            except Exception:
                pass
            return {"dir": d, "ok": False, "err": "统计超时（目录太大）",
                    "bytes": 0}
        time.sleep(0.2)
    out, errb = p.communicate()
    txt = (out or b"").decode("utf-8", "replace").strip()
    if not txt:
        return {"dir": d, "ok": False,
                "err": (errb or b"").decode("utf-8", "replace").strip()[:60]
                       or "du 无输出（权限不足？）", "bytes": 0}
    try:
        n = int(txt.split()[0])
    except Exception:
        return {"dir": d, "ok": False, "err": "du 输出无法解析", "bytes": 0}
    return {"dir": d, "ok": True, "err": "", "bytes": n}


def du_refresh():
    """逐个统计目录。全程持 busy 标志，页面据此显示"统计中"。"""
    with DU_LOCK:
        if DU_STATE["busy"]:
            return
        DU_STATE["busy"] = True
        DU_STATE["err"] = ""
    t0 = time.time()
    items = [_du_one(d) for d in DU_DIRS]
    with DU_LOCK:
        DU_STATE["items"] = items
        DU_STATE["at"] = time.time()
        DU_STATE["sec"] = round(time.time() - t0, 1)
        DU_STATE["busy"] = False
    log("du done in %.1fs" % (time.time() - t0))


def _du_thread():
    du_refresh()
    while True:
        # 切碎了睡，这样页面点"重新统计"能立刻响应，不用等满 5 分钟
        for _i in range(int(DU_TTL)):
            time.sleep(1.0)
            if DU_FORCE[0]:
                DU_FORCE[0] = False
                break
        du_refresh()


def du_snapshot():
    with DU_LOCK:
        return {"busy": DU_STATE["busy"], "at": DU_STATE["at"],
                "sec": DU_STATE["sec"], "err": DU_STATE["err"],
                "items": list(DU_STATE["items"]), "dirs": list(DU_DIRS)}


# ═══════════════ 故障话题 ═══════════════
# 订阅 /diagnose_faults，把故障码 + 故障内容带出来。
# 为什么单独起线程常驻订阅，而不是主循环里每秒现连一次 ROS：
#   订阅是长连接，建一次就够；每秒建一次会把 master 和车端都拖垮。
# 主循环只负责把"最新的那一份"带走 —— 没收到消息时保持上一次的，
# 这样低频发布的故障也不会在页面上闪没了。
FAULT_TOPIC = "/diagnose_faults"
FAULT_LOCK = threading.Lock()
FAULT_STATE = {"ok": False, "err": "", "items": [], "level": None,
               "at": 0, "type": ""}

# 硬件状态 /hardware_state：电量、电压、急停、清洁模式等。
# 同样是常驻订阅；没收到消息时保持上一次的，不闪没。
HW_TOPIC = "/hardware_state"
HW_LOCK = threading.Lock()
HW_STATE = {"ok": False, "err": "", "fields": {}, "at": 0, "type": ""}

# SLAM 版本 /slam_a_status：只要 sw_version 一个字段，但和硬件状态一样
# 收全量 —— 以后要看 docker_image / sn 不用再改采集端。
SLAM_TOPIC = "/slam_a_status"
SLAM_LOCK = threading.Lock()
SLAM_STATE = {"ok": False, "err": "", "fields": {}, "at": 0, "type": ""}

# 任务状态：/online_task_single 的 task_echo —— 当前在执行的任务。
# 和故障同一套思路：全量字段照收，页面挑它认识的。
TASK_TOPIC = "/online_task_single"
TASK_LOCK = threading.Lock()
TASK_STATE = {"ok": False, "err": "", "echo": {}, "name": "", "status": None,
              "at": 0, "type": ""}

# 任务队列：/online_tasks_deque 的 taskgroup_list —— 按发布顺序竖排。
QUEUE_TOPIC = "/online_tasks_deque"
QUEUE_LOCK = threading.Lock()
QUEUE_STATE = {"ok": False, "err": "", "groups": [], "at": 0, "type": ""}

# 导航状态：/cmd_vel，只取 linear.x（线速度 m/s）和 angular.z（角速度 rad/s）。
#
# 这个话题跟其它几个不一样：【只在有控制指令时才发布】，机器静止时
# 一条都没有。所以不能走 _setup_topic 那套"起脚本时等 10 秒，等不到
# 就判定未发布、之后再也不看" —— 那样脚本启动时机器正好停着，后面
# 真正动起来也永远捕捉不到，页面会一直显示"未发布"。
#
# 这里改成常驻轮询（_cmdvel_watch）：每秒看一次话题在不在，出现就订阅，
# 消失就退订。"当前是不是在发布"用最后一条消息的时间判断 —— 超过
# CMDVEL_STALE 秒没有新消息，就认为现在没有控制指令，但【保留最后一次
# 的值】，这样停下来之后还能知道刚才跑了多快。
CMDVEL_TOPIC = "/cmd_vel"
CMDVEL_LOCK = threading.Lock()
CMDVEL_STATE = {"ok": False, "err": "", "lin_x": None, "ang_z": None,
                "at": 0, "type": "", "live": False}
CMDVEL_STALE = 1.5      # 多久没消息算"停止发布"

# ═══════════════ 实际速度 /odom_origin ═══════════════
# cmd_vel 是【期望】速度（控制指令），odom_origin 是【实际】速度（里程计）。
# 两个必须并排看：车被卡住 / 打滑 / 电机没响应时，cmd_vel 照样发 0.3，
# 任务状态照样"执行中"，只有对比实际速度才能发现"指令发了但车没动"。
# 取 twist.twist.linear.x（线速度）和 twist.twist.angular.z（角速度），
# 与 cmd_vel 同一套取法，不做全量递归（这个话题通常 50Hz 发布）。
ODOM_TOPIC = "/odom_origin"
ODOM_LOCK = threading.Lock()
ODOM_STATE = {"ok": False, "err": "", "lin_x": None, "ang_z": None,
              "at": 0, "type": "", "live": False}
ODOM_STALE = 1.5

# ═══════════════ 定位状态 /ax_slam_state ═══════════════
# 定位飘了 / 丢了 / 重定位失败时，任务状态仍然是"执行中"，
# 但机器会走错路线甚至撞墙 —— 是清洁巡检机器人事故的高频根因，
# 事后复盘往往要翻 bag 才知道，所以必须能实时看到。
# 消息很短（8 个字段），走 _to_dict 全量收，页面挑它认识的几个。
AXSLAM_TOPIC = "/ax_slam_state"
AXSLAM_LOCK = threading.Lock()
AXSLAM_STATE = {"ok": False, "err": "", "fields": {}, "at": 0, "type": ""}

# ═══════════════ TF：把激光点从雷达系搬到地图系 ═══════════════
# 为什么要 TF：/scan 的点在雷达自己的坐标系里（frame_id 常是 laser），
# 而雷达相对车体中心有安装偏移，车体相对地图又有位姿。不做变换直接画，
# 点云就会整体偏一块、方向也不对 —— bag 分析工具里正是用 TF 换算的
# （toMap(frame, stamp)），这里沿用同一套思路。
#
# 顺序上先试 map，再退回 odom。拿不到就退回"相对车体"，由前端用
# /odom_origin 的位姿搬（老行为），并在界面上如实说明用的是哪种。
TF_LISTENER = None
TF_TARGET = ""       # 实际用的是 map 还是 odom，给前端显示

# TF 诊断。位姿对不上已经来回折腾很多轮，根因一直没定位到，
# 就是因为失败信息全被 except 吞掉、只落到 stderr（还不一定被看）。
# 现在每帧都随 JSON 回传，页面上直接能看到到底哪一步失败。
TF_DIAG = {"listener": "", "frames": "", "tried": [],
           "last_ok": 0.0, "last_ok_pair": "", "odom_raw": None,
           "has_tf": False, "has_tf2": False, "node": "",
           "tfmsg": "", "ntf": 0, "nsta": 0, "allframes": "",
           "root": "", "src": ""}


# ═══════════ TF 缓冲：自己订阅 /tf，不依赖 tf / tf2_ros 包 ═══════════
# 诊断反复报 "listener 未建立"，说明车上 import tf 直接失败 ——
# 精简部署里常常只装了 tf2_ros，或者压根没装 python-tf。
# 与其继续猜，不如自己订阅 /tf、/tf_static，在 python 里把变换树
# 拼出来：这条路只依赖 rospy 和标准库，车上一定能跑。
TF_DYN = {}
TF_STA = {}
TF_EDGES = {}
TF_PARENT = {}
TF_BUF_LOCK = threading.Lock()
TF_BUF_DIRTY = [False]
TF_BUF_N = [0, 0]
TF_SUB_ON = [False]


def _q_norm(q):
    n = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
    if n <= 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def _q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return _q_norm((aw * bx + ax * bw + ay * bz - az * by,
                    aw * by - ax * bz + ay * bw + az * bx,
                    aw * bz + ax * by - ay * bx + az * bw,
                    aw * bw - ax * bx - ay * by - az * bz))


def _q_conj(q):
    return (-q[0], -q[1], -q[2], q[3])


def _q_rot(q, v):
    """四元数旋转向量，等价于 R(q) * v。"""
    x, y, z, w = q
    vx, vy, vz = v
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    cx = y * tz - z * ty
    cy = z * tx - x * tz
    cz = x * ty - y * tx
    return (vx + w * tx + cx, vy + w * ty + cy, vz + w * tz + cz)


def _t_compose(A, B):
    """T(A<-C) = T(A<-B) 复合 T(B<-C)：先应用 B 再应用 A。"""
    ta, qa = A
    tb, qb = B
    r = _q_rot(qa, tb)
    return ((ta[0] + r[0], ta[1] + r[1], ta[2] + r[2]), _q_mul(qa, qb))


def _t_inv(A):
    t, q = A
    qi = _q_conj(q)
    r = _q_rot(qi, t)
    return ((-r[0], -r[1], -r[2]), qi)


def _yaw_of(q):
    x, y, z, w = q
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


IDENT = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def _tfbuf_rebuild():
    TF_EDGES.clear()
    TF_PARENT.clear()
    TF_EDGES.update(TF_STA)
    TF_EDGES.update(TF_DYN)     # 动态覆盖静态（map->odom 会一直更新）
    for (p, c) in TF_EDGES:
        if c not in TF_PARENT:
            TF_PARENT[c] = p
    TF_BUF_DIRTY[0] = False


def _tfbuf_path(frame):
    """从 frame 沿 parent 一路向上，返回 (路径, 根名)。"""
    path = []
    cur = frame
    seen = set()
    while cur in TF_PARENT:
        p = TF_PARENT[cur]
        if p == cur or cur in seen or len(path) > 24:
            break
        seen.add(cur)
        path.append((p, cur))
        cur = p
    return path, cur


def _tfbuf_root(frame):
    # 必须先重建：否则拿到的是上一次的树，推断出来的根是旧名字，
    # 于是候选里塞了个不存在的坐标系，最后掉回 odom（差十几米）。
    with TF_BUF_LOCK:
        if TF_BUF_DIRTY[0]:
            _tfbuf_rebuild()
    _, r = _tfbuf_path(frame)
    return r


def _tfbuf_lookup(target, src):
    """纯 python 查 TF 树，返回 ((x, y, yaw), 错误说明)。"""
    with TF_BUF_LOCK:
        if TF_BUF_DIRTY[0]:
            _tfbuf_rebuild()
        if not TF_EDGES:
            return None, "TF 缓冲为空（没收到 /tf）"
        edges = dict(TF_EDGES)
        parent = dict(TF_PARENT)
    if src == target:
        return (0.0, 0.0, 0.0), "src==target"
    p1, r1 = _tfbuf_path(src)
    p2, r2 = _tfbuf_path(target)
    if r1 == r2 and not p1 and not p2:
        return None, "两边都不在 TF 树里"
    if r1 != r2:
        return None, ("不连通：" + str(src) + "(根=" + str(r1) + ") 与 "
                      + str(target) + "(根=" + str(r2) + ")")
    T = IDENT
    for key in p1:
        e = edges.get(key)
        if not e:
            return None, "缺边 " + str(key[0]) + "<-" + str(key[1])
        # 上层的变换要当"后应用"的那一环：compose(上层, 已累积)
        T = _t_compose((e["t"], e["q"]), T)
    U = IDENT
    for key in p2:
        e = edges.get(key)
        if not e:
            return None, "缺边 " + str(key[0]) + "<-" + str(key[1])
        U = _t_compose((e["t"], e["q"]), U)
    R = _t_compose(_t_inv(U), T)
    return (float(R[0][0]), float(R[0][1]), float(_yaw_of(R[1]))), ""


def _tf_msg_cb(msg, is_static):
    try:
        trs = getattr(msg, "transforms", None)
    except Exception:
        return
    if not trs:
        return
    n = 0
    with TF_BUF_LOCK:
        for tr in trs:
            try:
                h = getattr(tr, "header", None)
                if h is None:
                    continue
                p = str(getattr(h, "frame_id", "") or "")
                c = str(getattr(tr, "child_frame_id", "") or "")
                if not p or not c:
                    continue
                tfm = getattr(tr, "transform", None)
                if tfm is None:
                    continue
                tv = getattr(tfm, "translation", None)
                qv = getattr(tfm, "rotation", None)
                if tv is None or qv is None:
                    continue
                rec = {"t": (float(tv.x), float(tv.y), float(tv.z)),
                       "q": _q_norm((float(qv.x), float(qv.y),
                                     float(qv.z), float(qv.w)))}
                if is_static:
                    TF_STA[(p, c)] = rec
                else:
                    TF_DYN[(p, c)] = rec
                n += 1
            except Exception:
                continue
        if n:
            TF_BUF_DIRTY[0] = True
            if is_static:
                TF_BUF_N[1] += n
            else:
                TF_BUF_N[0] += n


def _tfbuf_subscribe(rospy):
    cls = None
    for t in ("tf2_msgs/TFMessage", "tf/tfMessage"):
        try:
            cls = _load_msg_class(t)
        except Exception as e:
            log("tfbuf: load " + str(t) + " failed " + str(e)[:60])
            cls = None
        if cls is not None:
            TF_DIAG["tfmsg"] = t
            break
    if cls is None:
        TF_DIAG["tfmsg"] = "unavailable"
        return False
    ok = False
    for topic, st in (("/tf", False), ("/tf_static", True)):
        try:
            rospy.Subscriber(topic, cls,
                             lambda m, s=st: _tf_msg_cb(m, s),
                             queue_size=100)
            ok = True
        except Exception as e:
            log("tfbuf: sub " + str(topic) + " failed " + str(e)[:60])
    TF_SUB_ON[0] = ok
    return ok


def _tf_init():
    """init_node 之后才能建 listener，否则拿不到数据。"""
    global TF_LISTENER
    try:
        import tf
        TF_LISTENER = tf.TransformListener()
        TF_DIAG["listener"] = "ok"
        TF_DIAG["has_tf"] = True
        log("tf listener ready")
    except Exception as e:
        TF_LISTENER = None
        TF_DIAG["listener"] = "unavailable: %s" % str(e)[:120]
        log("tf listener unavailable: %s" % str(e)[:80])
    # tf2 单独试一次：只装了 tf2 的车上，import tf 会失败，
    # 此时至少要知道是不是这个原因，而不是笼统报"TF 不可用"。
    try:
        import tf2_ros  # noqa
        TF_DIAG["has_tf2"] = True
    except Exception as e:
        TF_DIAG["has_tf2"] = False
        log("tf2_ros unavailable: %s" % str(e)[:80])
    try:
        import rospy
        TF_DIAG["node"] = rospy.get_name() or ""
    except Exception:
        pass
    # 自研缓冲：即便 import tf 失败，这条也能把变换树拼出来。
    # 必须在 init_node 之后订阅。
    try:
        import rospy as _r
        if _tfbuf_subscribe(_r):
            log("tfbuf subscribed")
        else:
            log("tfbuf subscribe failed")
    except Exception as e:
        log("tfbuf init failed: " + str(e)[:80])


def _tf_lookup(target, src):
    """查 target -> src 的变换，返回 (x, y, yaw)，拿不到返回 None。

    必须先 waitForTransform 再 lookup —— 车上 C++ 的 RealtimePose 就是
    这么写的（见 ObtainTransform）。少了这一步，listener 缓存还没填上
    就会直接抛异常，于是一路退回 /odom_origin，而 /odom_origin 是 odom
    系，跟 map 系差出十几米，表现就是"位姿差得离谱"。

    失败原因一律记进 TF_DIAG["tried"]，不再静默吞掉。
    """
    # 位姿线程 20Hz 持续调用，不清会无限增长，直接把车端内存吃满。
    if len(TF_DIAG["tried"]) > 60:
        del TF_DIAG["tried"][:-20]
    if not target or not src:
        return None
    # 1) 自研缓冲优先：只依赖 rospy，import tf 失败的车上照样能跑
    r, err = _tfbuf_lookup(target, src)
    if r is not None:
        TF_DIAG["tried"].append({"pair": "%s<-%s(buf)" % (target, src),
                                 "ok": True, "err": ""})
        TF_DIAG["last_ok"] = time.time()
        TF_DIAG["last_ok_pair"] = "%s<-%s(buf)" % (target, src)
        TF_DIAG["src"] = "tfbuf"
        return r
    if err:
        TF_DIAG["tried"].append({"pair": "%s<-%s(buf)" % (target, src),
                                 "ok": False, "err": err[:120]})
    # 2) 原生 listener 兜底
    if TF_LISTENER is None:
        TF_DIAG["tried"].append({"pair": "%s<-%s" % (target, src), "ok": False,
                                 "err": "listener 未建立"})
        return None
    try:
        import rospy
        TF_LISTENER.waitForTransform(target, src, rospy.Time(0),
                                     rospy.Duration(0.2))
        tr, rot = TF_LISTENER.lookupTransform(target, src, rospy.Time(0))
    except Exception as e:
        TF_DIAG["tried"].append({"pair": "%s<-%s" % (target, src), "ok": False,
                                 "err": str(e)[:120]})
        return None
    try:
        import tf as _tf
        yaw = _tf.transformations.euler_from_quaternion(rot)[2]
        TF_DIAG["tried"].append({"pair": "%s<-%s" % (target, src), "ok": True,
                                 "err": ""})
        TF_DIAG["last_ok"] = time.time()
        TF_DIAG["last_ok_pair"] = "%s<-%s" % (target, src)
        return (float(tr[0]), float(tr[1]), float(yaw))
    except Exception as e:
        TF_DIAG["tried"].append({"pair": "%s<-%s" % (target, src), "ok": False,
                                 "err": "四元数换算失败: %s" % str(e)[:80]})
        return None


def _tf_to_map(src_frame):
    """源坐标系 -> 全局系。返回 ((x,y,yaw), 用的系)。

    不能只硬编码试 "map"/"odom"：车上全局系真叫别的名字（比如 world）
    时就只能退回 odom，而 odom 与 map 差出十几米，表现就是
    "位姿怎么调都不对"。所以第三个候选是 TF 树的根 ——
    根通常就是全局坐标系，叫什么名字都能命中。
    """
    cands = []
    for t in (MAP_FRAME, "map"):
        if t and t != src_frame and t not in cands:
            cands.append(t)
    try:
        root = _tfbuf_root(src_frame)
    except Exception:
        root = ""
    if root and root != src_frame and root not in cands:
        cands.append(root)
    if "odom" != src_frame and "odom" not in cands:
        cands.append("odom")
    for tgt in cands:
        r = _tf_lookup(tgt, src_frame)
        if r is not None:
            return r, tgt
    return None, ""


# ═══════════════ 雷达点云 /scan ═══════════════
SCAN_TOPIC = "/scan"
SCAN_LOCK = threading.Lock()
SCAN_MAX_PTS = 720          # 一帧最多这么多点，多了按 stride 抽稀
SCAN_STALE = 1.5


# ═══════════════ 激光点云：/scan（雷达）与 /depth2laser（相机转激光） ═══════════════
# 两个话题都是 sensor_msgs/LaserScan，结构完全一样，只是来源不同：
#   /scan         雷达本体（前端画橘黄）
#   /depth2laser  深度相机转成的伪激光（前端画绿色）
# 用同一套代码处理，避免两份逻辑各自跑偏。
D2L_TOPIC = "/depth2laser"
LASER_LOCK = threading.Lock()
LASER_STATE = {}


def _laser_new(topic):
    return {"ok": False, "err": "", "pts": [], "n": 0, "at": 0,
            "live": False, "topic": topic, "tf_ok": False, "tf_frame": ""}


for _tp in (SCAN_TOPIC, D2L_TOPIC):
    LASER_STATE[_tp] = _laser_new(_tp)


# ═══════════ 激光点云开关：默认关 ═══════════
# 为什么默认关：/scan 常发 10~50Hz，回调里要逐点做三角函数和 TF 变换，
# 两路都开时实测能吃掉相当一部分单核。而多数时候只是想看 CPU/任务，
# 点云是"要看才开"的东西。默认关掉，采集节点空载时开销降一个台阶。
#
# 双通道（stdin + 标志文件）与深度点云同理：stdin 在部分 SSH 环境下
# 不通，只走它会变成"勾了没反应"，所以标志文件作为第二条路。
LASER_FLAG = {
    SCAN_TOPIC: "/tmp/aoting_scan_on",
    D2L_TOPIC: "/tmp/aoting_d2l_on",
}
LASER_ENABLE = {SCAN_TOPIC: [False], D2L_TOPIC: [False]}
LASER_WANT_LAST = {SCAN_TOPIC: [None], D2L_TOPIC: [None]}


def _laser_flag_on(topic):
    try:
        return os.path.exists(LASER_FLAG[topic])
    except Exception:
        return False


def _laser_want(topic):
    """该不该订阅这路激光：stdin 或 标志文件，取"或"。"""
    v = bool(LASER_ENABLE[topic][0]) or _laser_flag_on(topic)
    if v != LASER_WANT_LAST[topic][0]:
        LASER_WANT_LAST[topic][0] = v
        src = []
        if LASER_ENABLE[topic][0]:
            src.append("stdin")
        if _laser_flag_on(topic):
            src.append("flag")
        log("%s want -> %s via %s" % (topic, v, "+".join(src) or "none"))
    return v


def _laser_cb(topic, msg):
    """LaserScan -> 机器人坐标系下的二维点。

    只算点在 base 系里的坐标，位姿单独带上，由 PC 端搬到地图系 ——
    这样地图换了（换分辨率/原点）时不用重新订阅，前端直接重画。
    """
    # 开关刚关、退订还没生效的那一两帧直接丢掉，别做无用计算
    if not _laser_want(topic):
        return
    try:
        rg = list(getattr(msg, "ranges", []) or [])
    except Exception:
        return
    n = len(rg)
    if not n:
        return
    try:
        amin = float(getattr(msg, "angle_min", 0.0))
        inc = float(getattr(msg, "angle_increment", 0.0))
        rmin = float(getattr(msg, "range_min", 0.0) or 0.0)
        rmax = float(getattr(msg, "range_max", 0.0) or 1e9)
    except Exception:
        return
    if inc == 0.0 and n > 1:
        inc = (float(getattr(msg, "angle_max", 0.0)) - amin) / (n - 1)

    frame = ""
    try:
        h = getattr(msg, "header", None)
        if h is not None:
            frame = str(getattr(h, "frame_id", "") or "")
    except Exception:
        frame = ""
    T, tsrc = _tf_to_map(frame) if frame else (None, "")

    stride = max(1, n // SCAN_MAX_PTS)
    pts = []
    for i in range(0, n, stride):
        try:
            r = float(rg[i])
        except Exception:
            continue
        if r != r or r in (float("inf"), float("-inf")):
            continue                      # inf/nan：激光没打到的点
        if r <= 0.0 or r < rmin or r > rmax:
            continue
        a = amin + i * inc
        bx = r * math.cos(a)
        by = r * math.sin(a)
        if T is not None:
            ca = math.cos(T[2])
            sa = math.sin(T[2])
            # 必须用临时量：直接写 bx = ... 会把下一行的 bx 换成已旋转的值
            wx = T[0] + bx * ca - by * sa
            wy = T[1] + bx * sa + by * ca
            bx, by = wx, wy
        pts.append([round(bx, 3), round(by, 3)])

    with LASER_LOCK:
        st = LASER_STATE[topic]
        st["ok"] = True
        st["err"] = ""
        st["pts"] = pts
        st["n"] = len(pts)
        st["at"] = time.time()
        st["tf_ok"] = T is not None
        st["tf_frame"] = (tsrc + " <- " + frame) if T else frame


def _laser_watch(rospy, topic):
    _poll_subscribe(rospy, topic, LASER_LOCK, LASER_STATE[topic],
                    lambda m: _laser_cb(topic, m),
                    "话题未发布（雷达节点未起？）",
                    want_fn=lambda: _laser_want(topic),
                    off_msg="未开启（勾选「" + topic + "」后才订阅，"
                            "默认关以省车上 CPU）")


def _scan_watch(rospy):
    _laser_watch(rospy, SCAN_TOPIC)


def _d2l_watch(rospy):
    _laser_watch(rospy, D2L_TOPIC)


def _laser_snapshot(topic):
    with LASER_LOCK:
        st = LASER_STATE[topic]
        at = st["at"]
        live = bool(at and (time.time() - at) <= SCAN_STALE)
        st["live"] = live
        with ODOM_LOCK:
            pose = ODOM_STATE.get("pose")
        return {"ok": st["ok"], "err": st["err"],
                "pts": st["pts"] if live else [], "n": st["n"],
                "pose": list(pose) if pose else None,
                "at": at, "live": live, "topic": st["topic"],
                "tf_ok": st["tf_ok"], "tf_frame": st["tf_frame"]}


def scan_snapshot():
    out = _laser_snapshot(SCAN_TOPIC)
    out["robot"] = _robot_pose_map()
    return out


def d2l_snapshot():
    return _laser_snapshot(D2L_TOPIC)


# ═══════════════ 深度相机 3D 点云 ═══════════════
# /camera_01/depth/image_raw (sensor_msgs/Image) 是深度图，
# /camera_01/depth/camera_info (sensor_msgs/CameraInfo) 给内参 K。
# 逐像素反投影就是相机系的三维点：
#     X = (u - cx) * d / fx
#     Y = (v - cy) * d / fy
#     Z = d
# 16UC1 的深度单位是毫米（除 1000 得米），32FC1 直接就是米。
#
# 为什么做成开关、默认关闭：深度图 10Hz 且单帧动辄 1MB 以上，
# 一直订阅会实打实占车上 CPU 和 ROS 带宽。只有用户勾了才订阅。
CAM_IDXS = [1, 2, 3, 4, 5]

# 相机外参：相机光学系 -> base_link 的安装位姿。
# 格式 [x, y, z, roll, pitch, yaw]，xyz 单位米，rpy 单位度。
# 没标定的填 None，点云就留在相机系（X 右 / Y 下 / Z 前）。
#
# 欧拉角约定按 ROS 常见的 fixed-axis RPY：R = Rz(yaw) * Ry(pitch) * Rx(roll)。
# 现场给相机 01 的是 [0.34, 0.03, 0.13, 1.93, -31.96, -2.88]，
# pitch -31.96 度说明相机明显朝下看地面，符合底盘避障相机的装法。
CAM_EXTRINSICS = {
    1: [0.34, 0.03, 0.13, 1.93, -31.96, -2.88],
    2: None,
    3: None,
    4: None,
    5: None,
}

# 外参来源：rosparam / builtin / none。给前端显示，让人知道看到的是
# 车上标定值还是兜底常量 —— 两者混着看会误导排查方向。
CAM_EXT_SRC = {i: "builtin" if CAM_EXTRINSICS.get(i) else "none"
               for i in CAM_IDXS}


def _cam_ext_param(idx):
    """相机 N 的外参在参数服务器上的路径。

    /depth_camera_config/depth_camera_01/install_pose
    02~05 同构，只改编号。
    """
    return "/depth_camera_config/depth_camera_%02d/install_pose" % idx


def _extrinsics_refresh(rospy_mod=None):
    """从 rosparam 读各路相机的外参，读到就覆盖内置常量。

    为什么还要内置常量：参数服务器不可达（ROS master 没起、连错机器）
    时至少还有相机 01 的现场标定值可用，不至于变成"完全没标定"。

    读不到就保留原来的值，不动 —— 一次网络抖动把已读到的标定值清空
    比没读到更糟。
    """
    if rospy_mod is None:
        # main() 里调用时手上没有 rospy 对象（rospy 只在 _ros_thread 里
        # import）。这里自己补一次：先修 python 路径（devel/lib 等），
        # 再 import。失败就保持内置值，绝不能因为读不到外参就让整个
        # 采集进程挂掉。
        try:
            _ensure_ros_path()
            import rospy as rospy_mod
        except Exception:
            return
    for i in CAM_IDXS:
        p = _cam_ext_param(i)
        try:
            v = rospy_mod.get_param(p, None)
        except Exception:
            continue
        if not v:
            continue
        try:
            if len(v) < 6:
                continue
            nums = [float(x) for x in v[:6]]
        except Exception:
            continue
        # 全 0 说明参数存在但没标定，别把它当成"装在原点"
        if not any(abs(x) > 1e-9 for x in nums):
            continue
        CAM_EXTRINSICS[i] = nums
        CAM_EXT_SRC[i] = "rosparam"
    log("extrinsics: " + ", ".join(
        "%d=%s" % (i, CAM_EXT_SRC[i]) for i in CAM_IDXS))

def _rpy_matrix(roll_deg, pitch_deg, yaw_deg):
    """rpy（度）转 3x3 旋转矩阵，R = Rz(yaw) * Ry(pitch) * Rx(roll)。

    返回扁平的 9 元组，行主序。
    """
    r = math.radians(float(roll_deg))
    p = math.radians(float(pitch_deg))
    y = math.radians(float(yaw_deg))
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    # Rx
    rx = (1.0, 0.0, 0.0, 0.0, cr, -sr, 0.0, sr, cr)
    # Ry
    ry = (cp, 0.0, sp, 0.0, 1.0, 0.0, -sp, 0.0, cp)
    # Rz
    rz = (cy, -sy, 0.0, sy, cy, 0.0, 0.0, 0.0, 1.0)
    return _mat3_mul(_mat3_mul(rz, ry), rx)

def _mat3_mul(a, b):
    """两个行主序 3x3 相乘。"""
    o = []
    for i in range(3):
        for j in range(3):
            s = 0.0
            for k in range(3):
                s += a[i * 3 + k] * b[k * 3 + j]
            o.append(s)
    return tuple(o)

def _cam_extrinsic(idx):
    """取相机 N 的外参，返回 (平移3元组, 旋转9元组) 或 (None, None)。"""
    try:
        i = int(idx)
    except Exception:
        return None, None
    e = CAM_EXTRINSICS.get(i)
    if not e or len(e) < 6:
        return None, None
    try:
        t = (float(e[0]), float(e[1]), float(e[2]))
        r = _rpy_matrix(e[3], e[4], e[5])
    except Exception:
        return None, None
    return t, r

def _cam_topics(idx):
    """相机 N 的深度图 / 内参话题。

    命名规则由现场确认：/camera_01/depth/image_raw，
    02 03 04 05 依次类推。越界一律回落 01，绝不拼出不存在的名字。
    """
    try:
        i = int(idx)
    except Exception:
        i = 1
    if i not in CAM_IDXS:
        i = 1
    n = "%02d" % i
    return ("/camera_" + n + "/depth/image_raw",
            "/camera_" + n + "/depth/camera_info")

DEPTH_IMG_TOPIC, DEPTH_INFO_TOPIC = _cam_topics(1)

# 开关标志文件：stdin 之外的第二条通道。
#
# 为什么需要它：开关命令原本只走 stdin。实际车上出现"前端勾了、
# server 也返回 ok，但车端始终显示未开启"——命令根本没到车端
# （SSH 通道不给 stdin / 窗口满 / 读线程没起来都可能）。
# 标志文件不依赖任何通道：server 用独立的 exec 通道 touch / rm，
# 车端每秒看一次文件在不在。两个通道取"或"，任一通就能开。
PC3D_FLAG = "/tmp/aoting_pc3d_on"

PC3D_LOCK = threading.Lock()
PC3D_MAX_PTS = 4000           # 点数再少一半：传 4000 点比 6000 点省 1/3 带宽
PC3D_MAX_RANGE = 8.0          # 米，太远的点没意义且会撑爆画面
PC3D_STALE = 6.0
PC3D_INTERVAL = 2.0           # 秒，最多这么久算一次（算点+传点都费带宽）
# 回调节流：现场实测深度图 10Hz，而上面每 2 秒才用一帧。
# 不节流的话 10Hz × 每帧整份拷贝（640×480×2 ≈ 600KB）纯属白烧 CPU。
PC3D_CB_MIN_GAP = 0.5         # 秒，最快这么久才收一帧进来
PC3D_ENABLE = [False]
PC3D_CAM = [1]
PC3D_CAM_FLAG = "/tmp/aoting_pc3d_cam"
PC3D_CAM_NOW = [1]
PC3D_LAST = [0.0]
PC3D_CB_LAST = [0.0]
PC3D_CNT = {"frames": 0, "calcs": 0, "cmds": 0}
PC3D_STATE = {"ok": False, "err": "", "pts": [], "n": 0, "at": 0,
              "live": False, "enc": "", "w": 0, "h": 0,
              "topic": DEPTH_IMG_TOPIC}
DEPTH_IMG = {"msg": None, "at": 0}
DEPTH_INFO = {"k": None, "at": 0}


def _pc3d_flag_on():
    """标志文件是否存在 —— 第二条开关通道。"""
    try:
        return os.path.exists(PC3D_FLAG)
    except Exception:
        return False


def _pc3d_flag_cam():
    """标志文件里的相机号 —— 与开关同理的第二条通道。"""
    try:
        with open(PC3D_CAM_FLAG) as f:
            v = int((f.read() or "").strip())
        if v in CAM_IDXS:
            return v
    except Exception:
        pass
    return None


def _pc3d_cam():
    """当前看哪路相机：标志文件优先，其次 stdin，都没有就 01。"""
    v = _pc3d_flag_cam()
    if v is None:
        v = PC3D_CAM[0]
    if v not in CAM_IDXS:
        v = 1
    return v


def _pc3d_topics_now():
    img_t, info_t = _cam_topics(_pc3d_cam())
    return img_t, info_t, _pc3d_cam()


PC3D_WANT_LAST = [None]

def _pc3d_want():
    """当前是否该订阅深度图：stdin 命令 或 标志文件，取"或"。

    状态翻转时打一条日志。为什么要记这个：之前排查"勾了没反应"全靠猜，
    因为 PC 端只知道命令发出去了，不知道车端到底认没认。现在车端会自己
    说清楚是【哪条通道】让它变的（stdin / flag / 都没有）。
    """
    v = bool(PC3D_ENABLE[0]) or _pc3d_flag_on()
    if v != PC3D_WANT_LAST[0]:
        PC3D_WANT_LAST[0] = v
        src = []
        if PC3D_ENABLE[0]:
            src.append("stdin")
        if _pc3d_flag_on():
            src.append("flag")
        log("pc3d want -> %s via %s" % (v, "+".join(src) or "none"))
    return v


def _depth_img_cb(msg):
    # 两条通道任一开启就算开。没开就别存，省内存和拷贝开销。
    if not _pc3d_want():
        return
    now = time.time()
    if now - PC3D_CB_LAST[0] < PC3D_CB_MIN_GAP:
        return                        # 10Hz 的帧大部分直接丢掉
    PC3D_CB_LAST[0] = now
    with PC3D_LOCK:
        DEPTH_IMG["msg"] = msg
        DEPTH_IMG["at"] = now
        PC3D_CNT["frames"] += 1
        PC3D_STATE["enc"] = str(getattr(msg, "encoding", "") or "")
        PC3D_STATE["w"] = int(getattr(msg, "width", 0) or 0)
        PC3D_STATE["h"] = int(getattr(msg, "height", 0) or 0)


def _depth_info_cb(msg):
    try:
        k = list(getattr(msg, "K", []) or [])
    except Exception:
        k = []
    with PC3D_LOCK:
        DEPTH_INFO["k"] = [float(x) for x in k] if len(k) >= 9 else None
        DEPTH_INFO["at"] = time.time()


def _pc3d_compute():
    """把最新一帧深度图反投影成三维点（返回扁平数组 [x,y,z,...]）。"""
    with PC3D_LOCK:
        msg = DEPTH_IMG["msg"]
        k = DEPTH_INFO["k"]
    if msg is None:
        return None, "未收到深度图 " + DEPTH_IMG_TOPIC
    if not k:
        return None, "未收到相机内参 " + DEPTH_INFO_TOPIC
    try:
        w = int(getattr(msg, "width", 0))
        h = int(getattr(msg, "height", 0))
        enc = str(getattr(msg, "encoding", "") or "")
        raw = bytes(getattr(msg, "data", b"") or b"")
    except Exception:
        return None, "深度图字段读取失败"
    if w <= 0 or h <= 0:
        return None, "深度图尺寸异常"
    fx, fy, cx, cy = k[0], k[4], k[2], k[5]
    if not fx or not fy:
        return None, "相机内参 K 异常"

    total = w * h
    if enc == "32FC1":
        need, scale = total * 4, 1.0
        fmt = "<" + str(total) + "f"
    else:
        # 16UC1 / mono16：单位是毫米
        need, scale = total * 2, 0.001
        fmt = "<" + str(total) + "H"
    if len(raw) < need:
        return None, "深度数据长度不足（enc=%s）" % enc
    try:
        vals = struct.unpack(fmt, raw[:need])
    except Exception as e:
        return None, "深度解码失败 %r" % (e,)

    # 横竖一起抽稀，点云才不会被抽成几条横线
    step = int(math.sqrt(float(total) / float(PC3D_MAX_PTS)))
    if step < 1:
        step = 1
    lim = PC3D_MAX_PTS * 3
    # 这里【只做反投影】，输出的是相机光学系的点（X 右 / Y 下 / Z 前）。
    #
    # 外参变换放到前端做，原因是轴约定还没定死：现场给的 rpy
    # (1.93, -31.96, -2.88) 按 ROS 标准 Rz*Ry*Rx 算出来，相机正前方
    # 指向 base_link 的 (-0.53, 0, +0.85)，即朝后上方——而任何小于 90
    # 度的单一欧拉角都算不出"朝下"，说明约定至少有一样不是我猜的那样。
    # 与其打包让现场反复试，不如把 6 个数和轴约定都放到界面上可调，
    # 改完立刻重绘，一次就能判定哪种对。
    out = []
    for v in range(0, h, step):
        base = v * w
        for u in range(0, w, step):
            d = vals[base + u]
            if d != d:
                continue
            z = d * scale
            if z <= 0.01 or z > PC3D_MAX_RANGE:
                continue
            out.append(round((u - cx) * z / fx, 3))
            out.append(round((v - cy) * z / fy, 3))
            out.append(round(z, 3))
            if len(out) >= lim:
                break
        if len(out) >= lim:
            break
    return out, ""


def pc3d_snapshot():
    with PC3D_LOCK:
        at = PC3D_STATE["at"]
        live = bool(at and (time.time() - at) <= PC3D_STALE)
        PC3D_STATE["live"] = live
        # 诊断字段：前端靠它们区分「命令没送到车端」和「送到了但没数据」。
        # 以前只有一句"未开启"，用户勾了开关却永远看到这句，排查无从下手。
        diag = {"on": bool(_pc3d_want()),
                "via_stdin": bool(PC3D_ENABLE[0]),
                "via_flag": _pc3d_flag_on(),
                "frames": PC3D_CNT["frames"],
                "calcs": PC3D_CNT["calcs"],
                "age": (round(time.time() - at, 1) if at else None)}
        if not _pc3d_want():
            return {"ok": False, "err": "未开启（勾选后才订阅深度图）",
                    "pts": [], "n": 0, "live": False,
                    "topic": _cam_topics(_pc3d_cam())[0],
                    "enc": "", "w": 0, "h": 0, "diag": diag}
        r = {"ok": PC3D_STATE["ok"], "err": PC3D_STATE["err"],
             "pts": PC3D_STATE["pts"] if live else [],
             "n": PC3D_STATE["n"], "live": live,
             "enc": PC3D_STATE["enc"], "w": PC3D_STATE["w"],
             "h": PC3D_STATE["h"], "topic": _cam_topics(_pc3d_cam())[0]}
        r["diag"] = diag
        r["cam"] = _pc3d_cam()
        # 给前端预填的默认外参（相机 N 的 [x,y,z,roll,pitch,yaw]）。
        # 变换在前端做，这里只负责把标定值送过去。
        _e = CAM_EXTRINSICS.get(_pc3d_cam())
        r["ext"] = list(_e) if _e else None
        r["ext_src"] = CAM_EXT_SRC.get(_pc3d_cam(), "none")
        return r


def _pc3d_tick():
    """按 PC3D_INTERVAL 节流地算一次点云，结果放进 PC3D_STATE。"""
    if not _pc3d_want():
        return
    now = time.time()
    if now - PC3D_LAST[0] < PC3D_INTERVAL:
        return
    PC3D_LAST[0] = now
    pts, err = _pc3d_compute()
    with PC3D_LOCK:
        if pts:
            PC3D_STATE["ok"] = True
            PC3D_STATE["err"] = ""
            PC3D_STATE["pts"] = pts
            PC3D_STATE["n"] = len(pts) // 3
            PC3D_STATE["at"] = now
            PC3D_CNT["calcs"] += 1
        else:
            PC3D_STATE["ok"] = False
            PC3D_STATE["err"] = err or "无有效深度点"


def _pc3d_watch(rospy):
    """深度图订阅：只有用户勾了才订阅，取消后立刻退订。

    不用 _poll_subscribe 是因为它只看"话题在不在发布列表"，
    表达不了"用户现在不想看"。深度图 10Hz 且单帧很大，
    关了就必须真退订，否则会一直白占车上带宽。
    """
    sub = [None, None]
    while True:
        try:
            time.sleep(1.0)
        except Exception:
            return
        want = _pc3d_want()
        cam = _pc3d_cam()
        # 换了相机号就先退订再重订，否则看到的还是上一路相机的点。
        if want and sub[0] is not None and PC3D_CAM_NOW[0] != cam:
            for s in sub:
                try:
                    if s is not None:
                        s.unregister()
                except Exception:
                    pass
            sub[0] = None
            sub[1] = None
            with PC3D_LOCK:
                DEPTH_IMG["msg"] = None
                DEPTH_INFO["k"] = None
                PC3D_STATE["pts"] = []
                PC3D_STATE["n"] = 0
                PC3D_STATE["ok"] = False
            log("pc3d: switch to cam %02d" % cam)
        if want and sub[0] is None:
            img_t, info_t = _cam_topics(cam)
            try:
                from sensor_msgs.msg import Image as _Img
                from sensor_msgs.msg import CameraInfo as _CInfo
                sub[0] = rospy.Subscriber(img_t, _Img,
                                          _depth_img_cb, queue_size=1)
                sub[1] = rospy.Subscriber(info_t, _CInfo,
                                          _depth_info_cb, queue_size=1)
                PC3D_CAM_NOW[0] = cam
                log("pc3d: subscribed cam %02d" % cam)
            except Exception as e:
                log("pc3d: subscribe failed %r" % (e,))
                with PC3D_LOCK:
                    PC3D_STATE["ok"] = False
                    PC3D_STATE["err"] = "订阅失败：%r" % (e,)
                sub[0] = None
                sub[1] = None
        elif (not want) and sub[0] is not None:
            for s in sub:
                try:
                    if s is not None:
                        s.unregister()
                except Exception:
                    pass
            sub[0] = None
            sub[1] = None
            with PC3D_LOCK:
                DEPTH_IMG["msg"] = None
                PC3D_STATE["ok"] = False
                PC3D_STATE["err"] = "未开启"
                PC3D_STATE["pts"] = []
                PC3D_STATE["n"] = 0
            log("pc3d: unsubscribed")


# ═══════════════ 彩色相机（并入 stat_agent） ═══════════════
# 为什么并进 stat_agent，而不是另起一个 cam_agent.py：
#   独立脚本是裸跑的，没有 source 过 ROS，import rospy 直接失败 ——
#   日志里那句 "rostopic：未找到命令" 是同一个原因。而 stat_agent 是
#   在已 source 的 bash 里跑的，ROS 现成可用，订阅也复用同一个 node。
# 帧怎么回传：base64 的 JPEG，作为独立的 {"type":"rgb"} 行打在 stdout。
#   不能发二进制帧：会和每秒一行的 JSON 状态帧抢同一条 stdout，
#   协议会混在一起。单独一行则两种帧互不干扰。
RGB_IDXS = [1, 2, 3, 4, 5]
RGB_FLAG_FILE = "/tmp/aoting_rgb_cams.txt"   # 内容形如 "1,3"
RGB_LOCK = threading.Lock()
RGB_ENABLE = {}      # cam -> [stdin 开关]
RGB_WANT_LAST = {}   # cam -> [上次是否订阅]，只在变化时打日志
RGB_STATE = {}       # cam -> 诊断
RGB_FRAMES = {}      # cam -> [jpeg 字节, 车端时间戳]，只留最新一帧
RGB_LAST = {}        # cam -> [上一帧时刻]，按 fps 节流
RGB_CFG = {"w": [640], "fps": [5.0], "q": [70]}
for _c in RGB_IDXS:
    RGB_ENABLE[_c] = [False]
    RGB_WANT_LAST[_c] = [None]
    RGB_STATE[_c] = {"on": False, "topic": "", "mtype": "", "n": 0,
                     "err": "未开启", "at": 0.0, "w": 0, "h": 0,
                     "enc": "", "lib": ""}
    RGB_LAST[_c] = [0.0]

# 主循环和相机回调会并发 print，两行 JSON 交错成一行的话 PC 端
# json.loads 直接失败、整帧丢掉，所以共用一把锁。
OUT_LOCK = threading.Lock()


def _emit(obj):
    with OUT_LOCK:
        print(json.dumps(obj), flush=True)


def _rgb_flag_cams():
    """标志文件通道：stdin 在部分 SSH 下不通，这是第二条路。"""
    try:
        f = open(RGB_FLAG_FILE, "r")
        try:
            txt = f.read()
        finally:
            f.close()
    except Exception:
        return set()
    out = set()
    for p in txt.replace(";", ",").replace(" ", ",").split(","):
        p = p.strip()
        try:
            v = int(p)
        except Exception:
            continue
        if v in RGB_IDXS:
            out.add(v)
    return out


def _rgb_want(cam):
    fl = cam in _rgb_flag_cams()
    v = bool(RGB_ENABLE[cam][0]) or fl
    if v != RGB_WANT_LAST[cam][0]:
        RGB_WANT_LAST[cam][0] = v
        src = []
        if RGB_ENABLE[cam][0]:
            src.append("stdin")
        if fl:
            src.append("flag")
        log("rgb%02d want -> %s via %s" % (cam, v, "+".join(src) or "none"))
    return v


def _rgb_pick(rospy, cam):
    """从发布列表里挑这路相机的彩色话题，返回 (话题, 类型或错误说明)。

    不硬编码 color/image_raw：各车命名不同（rgb/image_raw、.../compressed
    等），猜错了就是"订阅成功但永远 0 帧"，且现象上看不出原因。
    优先压缩话题：车上已编好 JPEG，直接透传，最省车上 CPU 和带宽。
    """
    pat = "camera_%02d" % cam
    try:
        pub = rospy.get_published_topics()
    except Exception as e:
        return "", "get_published_topics 失败: %r" % (e,)
    cands = []
    for item in pub:
        try:
            name, ty = item[0], item[1]
        except Exception:
            continue
        low = str(name).lower()
        if pat not in low:
            continue
        if "depth" in low:
            continue                       # 深度图不是彩色画面
        if ("color" in low or "rgb" in low
                or low.rstrip("/").endswith("image_raw")):
            if "Image" in str(ty):
                cands.append((name, ty))
    if not cands:
        return "", ("车上没有 %s 的彩色话题" % pat)
    comp = [c for c in cands if "Compressed" in str(c[1])]
    pick = comp[0] if comp else cands[0]
    return pick[0], pick[1]


def _rgb_jpeg(msg, mtype, cam):
    """把一帧编成 JPEG。返回 (bytes, w, h, enc, lib, err)。"""
    want_w = int(RGB_CFG["w"][0])
    q = int(RGB_CFG["q"][0])
    if "Compressed" in str(mtype):
        try:
            data = bytes(msg.data)
        except Exception:
            data = msg.data
        fmt = str(getattr(msg, "format", "") or "").lower()
        head = data[:3] if len(data) >= 3 else b""
        if head == b"\xff\xd8\xff" or "jpeg" in fmt or "jpg" in fmt:
            return data, 0, 0, (fmt or "jpeg"), "passthrough", ""
        return None, 0, 0, fmt, "", ("压缩格式不是 jpeg（%s）" % (fmt or "?"))

    hh = int(getattr(msg, "height", 0) or 0)
    ww = int(getattr(msg, "width", 0) or 0)
    enc = str(getattr(msg, "encoding", "") or "")
    try:
        raw = bytes(msg.data)
    except Exception:
        raw = msg.data
    if not hh or not ww:
        return None, 0, 0, enc, "", "图像尺寸为空"

    cv2 = None
    np = None
    try:
        import numpy as np
        import cv2
    except Exception:
        cv2 = None
    if cv2 is not None:
        try:
            a = np.frombuffer(raw, dtype=np.uint8)
            if enc in ("rgb8", "bgr8"):
                a = a.reshape(hh, ww, 3)
                if enc == "rgb8":
                    a = a[:, :, ::-1]      # cv2 认 BGR
            elif enc in ("mono8", "8UC1"):
                a = a.reshape(hh, ww)
                a = cv2.cvtColor(a, cv2.COLOR_GRAY2BGR)
            elif enc in ("yuv422", "uyvy"):
                a = a.reshape(hh, ww, 2)
                a = cv2.cvtColor(a, cv2.COLOR_YUV2BGR_UYVY)
            else:
                return None, ww, hh, enc, "cv2", ("不支持的编码 %s" % enc)
            if ww > want_w:
                nh = max(1, int(hh * want_w / float(ww)))
                a = cv2.resize(a, (want_w, nh))
                ww, hh = want_w, nh
            ok, buf = cv2.imencode(".jpg", a,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), q])
            if ok:
                return bytes(buf), ww, hh, enc, "cv2", ""
            return None, ww, hh, enc, "cv2", "imencode 返回失败"
        except Exception as e:
            return None, ww, hh, enc, "cv2", ("编码失败 %r" % (e,))

    # 没有 cv2 就退 PIL：只能处理 rgb8 / bgr8 / mono8
    if enc not in ("rgb8", "bgr8", "mono8", "8UC1"):
        return None, ww, hh, enc, "", ("车上没有 cv2，%s 需要它" % enc)
    try:
        from PIL import Image
        import io as _io
        if enc == "rgb8":
            im = Image.frombytes("RGB", (ww, hh), raw)
        elif enc == "bgr8":
            im = Image.frombytes("RGB", (ww, hh), raw)
            r, g, b = im.split()
            im = Image.merge("RGB", (b, g, r))
        else:
            im = Image.frombytes("L", (ww, hh), raw)
        if ww > want_w:
            nh = max(1, int(hh * want_w / float(ww)))
            im = im.resize((want_w, nh))
            ww, hh = want_w, nh
        bio = _io.BytesIO()
        im.save(bio, "JPEG", quality=q)
        return bio.getvalue(), ww, hh, enc, "PIL", ""
    except Exception as e:
        return None, ww, hh, enc, "", ("车上缺 cv2/PIL（%s）" % str(e)[:80])


def _rgb_cb(cam, mtype):
    def _f(msg):
        if not _rgb_want(cam):
            return
        try:
            gap = 1.0 / max(0.2, float(RGB_CFG["fps"][0]))
        except Exception:
            gap = 0.2
        now = time.time()
        if now - RGB_LAST[cam][0] < gap:
            return                      # 按 fps 丢帧，别把带宽打满
        RGB_LAST[cam][0] = now
        data, w, h, enc, lib, err = _rgb_jpeg(msg, mtype, cam)
        with RGB_LOCK:
            st = RGB_STATE[cam]
            if not data:
                st["err"] = err or "编码失败"
                return
            st["n"] = int(st.get("n", 0)) + 1
            st["at"] = now
            st["w"], st["h"] = w, h
            st["enc"], st["lib"] = enc, lib
            st["err"] = ""
        try:
            import base64
            b64 = base64.b64encode(data).decode("ascii")
        except Exception:
            return
        _emit({"type": "rgb", "cam": cam, "jpeg": b64, "ts": now,
               "w": w, "h": h, "n": RGB_STATE[cam]["n"]})
    return _f


def _rgb_watch(rospy):
    """五路彩色相机的订阅管理：勾了才订阅，取消就真退订。"""
    subs = {}
    while True:
        try:
            time.sleep(1.0)
        except Exception:
            return
        for cam in RGB_IDXS:
            want = _rgb_want(cam)
            sub = subs.get(cam)
            if want and sub is None:
                topic, mtype = _rgb_pick(rospy, cam)
                with RGB_LOCK:
                    st = RGB_STATE[cam]
                    st["on"] = True
                    st["topic"] = topic
                    st["mtype"] = mtype
                if not topic:
                    with RGB_LOCK:
                        st["err"] = mtype
                    log("rgb%02d: %s" % (cam, mtype))
                    continue
                try:
                    cls = _load_msg_class(mtype)
                    # buff_size 给大点：默认缓冲放不下一帧 640x480，
                    # 会出现"订阅成功但一直没回调"。
                    subs[cam] = rospy.Subscriber(
                        topic, cls, _rgb_cb(cam, mtype),
                        queue_size=1, buff_size=4194304)
                    log("rgb%02d: subscribed %s [%s]" % (cam, topic, mtype))
                except Exception as e:
                    with RGB_LOCK:
                        st["err"] = "订阅失败 %r" % (e,)
                    log("rgb%02d: subscribe failed %r" % (cam, e))
            elif (not want) and sub is not None:
                try:
                    sub.unregister()
                except Exception:
                    pass
                subs.pop(cam, None)
                with RGB_LOCK:
                    st = RGB_STATE[cam]
                    st["on"] = False
                    st["err"] = "未开启"
                    st["topic"] = ""
                RGB_FRAMES.pop(cam, None)
                log("rgb%02d: unsubscribed" % cam)


def rgb_snapshot():
    with RGB_LOCK:
        out = {}
        for cam in RGB_IDXS:
            st = RGB_STATE[cam]
            out[str(cam)] = {"on": st["on"], "topic": st["topic"],
                             "mtype": st["mtype"], "n": st["n"],
                             "err": st["err"], "at": st["at"],
                             "w": st["w"], "h": st["h"],
                             "enc": st["enc"], "lib": st["lib"]}
        return out


# ═══════════════ 实时位姿（照车上 C++ RealtimePose 的逻辑） ═══════════════
# 车上 module_layer/RealtimePose.h 的做法，这里逐条对齐：
#   1) 坐标系名从 ROS 参数读：/map_frame(默认 map)、/base_frame(默认 base_link)
#      —— 硬编码 base_link 在改过参数的那批车上会查空。
#   2) 独立线程高频循环持续更新缓存（C++ 是 40Hz）。
#      不能"要用时才查一次"：listener 缓存空时必然抛异常，
#      于是退回 /odom_origin（odom 系），跟 map 系差十几米。
#   3) 缓存带时间戳，4 秒内可用（对应 C++ 的 robotPoseActiveTime=4000ms）。
#      偶发一次查不到就用上一次的值，而不是立刻降级。
#   4) 只有缓存真的空了/超时，才退回 /odom_origin 并如实标注来源。
POSE_LOCK = threading.Lock()
POSE_CACHE = {"x": None, "y": None, "yaw": None, "at": 0.0,
              "frame": "", "child": "", "src": "", "err": ""}
POSE_ACTIVE = 4.0          # 秒，位姿缓存有效期
POSE_RATE = 20.0           # Hz，位姿更新频率
MAP_FRAME = "map"
BASE_FRAME = "base_link"


def _read_frames():
    """从 ROS 参数读坐标系名，读不到就用默认值。"""
    global MAP_FRAME, BASE_FRAME
    try:
        import rospy
        MAP_FRAME = rospy.get_param("/map_frame", "map") or "map"
        BASE_FRAME = rospy.get_param("/base_frame", "base_link") or "base_link"
        TF_DIAG["frames"] = "%s / %s" % (MAP_FRAME, BASE_FRAME)
        log("frames: map=%s base=%s" % (MAP_FRAME, BASE_FRAME))
    except Exception:
        pass


def _pose_update_once():
    """查一次 TF，成功就刷新缓存。"""
    r, tgt = _tf_to_map(BASE_FRAME)
    if r is None:
        return False
    with POSE_LOCK:
        POSE_CACHE["x"] = r[0]
        POSE_CACHE["y"] = r[1]
        POSE_CACHE["yaw"] = r[2]
        POSE_CACHE["at"] = time.time()
        POSE_CACHE["frame"] = tgt
        POSE_CACHE["child"] = BASE_FRAME
        POSE_CACHE["src"] = "tf"
        POSE_CACHE["err"] = ""
    return True


def _pose_thread():
    """独立线程持续维护位姿缓存（对应 C++ RealtimePose::Start 的循环）。"""
    import rospy
    rate = rospy.Rate(POSE_RATE)
    while True:
        try:
            _pose_update_once()
        except Exception:
            pass
        try:
            rate.sleep()
        except Exception:
            time.sleep(1.0 / POSE_RATE)


def _robot_pose_map():
    """车体在地图系的位姿，前端拿它把车画到地图上。

    同时它也是 TF 是否可信的探针：如果车标位置和实际明显不符，
    说明 TF 树有问题，点云自然也对不上 —— 比只看点云容易判断。
    """
    # 优先用独立线程维护的缓存（20Hz 更新，4 秒内有效）。
    # 偶发一次查不到不会立刻降级 —— 这一点很重要，否则位姿会在
    # "TF 值"和"odom 值"之间来回跳，两个数差十几米，看着像乱跳。
    # 诊断随每帧回传，页面上直接看到 TF 到底卡在哪一步。
    # tried 只留最近几条：位姿线程 20Hz，不清会无限增长。
    diag = dict(TF_DIAG)
    diag["tried"] = list(TF_DIAG["tried"][-6:])
    diag["age"] = (round(time.time() - TF_DIAG["last_ok"], 1)
                   if TF_DIAG["last_ok"] else None)
    # 把车上所有 TF frame 列出来。坐标系名不对（比如车上叫 slam_map
    # 而不是 map）是位姿对不上的头号原因，光看"查不到"猜不出来。
    # 优先列自研缓冲里的 frame：import tf 失败时它是唯一来源
    try:
        with TF_BUF_LOCK:
            if TF_BUF_DIRTY[0]:
                _tfbuf_rebuild()
            ks = list(TF_EDGES.keys())
            nf = list(TF_BUF_N)
            on = bool(TF_SUB_ON[0])
            frames = set()
            for (p, c) in ks:
                frames.add(p)
                frames.add(c)
        diag["ntf"] = nf[0]
        diag["nsta"] = nf[1]
        diag["subon"] = on
        if frames:
            diag["all"] = " ".join(sorted(frames))[:400]
        try:
            diag["root"] = _tfbuf_root(BASE_FRAME)
        except Exception:
            diag["root"] = ""
    except Exception:
        pass
    try:
        if TF_LISTENER is not None and not diag.get("all"):
            diag["all"] = str(TF_LISTENER.allFramesAsString()).replace("\n", " ")[:400]
    except Exception:
        pass

    with POSE_LOCK:
        if POSE_CACHE["x"] is not None:
            fresh = (time.time() - POSE_CACHE["at"]) <= POSE_ACTIVE
            return {"x": POSE_CACHE["x"], "y": POSE_CACHE["y"],
                    "yaw": POSE_CACHE["yaw"], "frame": POSE_CACHE["frame"],
                    "child": POSE_CACHE["child"],
                    "src": "tf" if fresh else "tf_stale",
                    "age": round(time.time() - POSE_CACHE["at"], 1),
                    "diag": diag}
    # TF 拿不到就退回 /odom_origin。
    # 之前这里只查 TF，TF 不可用时车标整个消失 —— 而点云同一时刻
    # 是靠 /odom_origin 兜底画出来的，于是出现"有点云没车标"的怪象。
    # 车标必须比点云更不容易消失：它是判断 TF 对不对的唯一参照。
    with ODOM_LOCK:
        p = ODOM_STATE.get("pose")
    if p and len(p) >= 3:
        diag["odom_raw"] = [round(p[0], 3), round(p[1], 3)]
        return {"x": p[0], "y": p[1], "yaw": p[2], "frame": "odom",
                "child": "base_link", "src": "odom", "age": None,
                "diag": diag}
    return {"diag": diag}




# ═══════════════ 地图信息 /map_info_v3 ═══════════════
# 只要 base_info.name（如 HGTV）—— PC 端拿它去拼地图目录
#   /home/aoting/user_config/map_database/<name>/map.png
# 把 PNG 拉回来显示，就能"看到机器此刻跑的是哪张图"。
#
# 走轮询订阅而不是一次性等待：地图话题常是 latched（只发一次），
# 采集脚本起得比建图节点早的话，一次性等待会永远等不到。
# 同理中途重新加载地图也能更新，不用重启采集。
MAP_TOPIC = "/map_info_v3"
MAP_REFRESH = [False]
MAP_LOCK = threading.Lock()
# id 与 name 一起给：PC 端拼地图目录时先用 name，找不到再用 id 兜底
# （地图名改过 / 含特殊字符时，目录名往往还是建图时的 id）。
MAP_STATE = {"ok": False, "err": "", "fields": {}, "at": 0, "type": "",
             "name": "", "id": ""}

# 自定义话题：由 PC 端在页面上指定话题名，这里动态订阅并回传内容。
# 用集合装"当前想订阅的"，由 _custom_watch 线程做增量订阅/退订。
CUSTOM_LOCK = threading.Lock()
CUSTOM_WANT = set()          # PC 端要求订阅的话题名
CUSTOM_SUB = {}              # 话题名 -> rospy.Subscriber
CUSTOM_STATE = {}            # 话题名 -> {"ok","err","type","at","fields","note"}

# 单条自定义消息回传上限。点云、图像这类消息全量 dict 化会有几 MB，
# 每秒往 PC 端传会把 SSH 通道和页面都拖死，所以按层深/条数/字节三重截断。
CUSTOM_MAX_DEPTH = 3
CUSTOM_MAX_ITEMS = 25
CUSTOM_MAX_CHARS = 12000

# 任务状态枚举。只做"数字 → 中文"的翻译，不做任何推断 ——
# 状态码的含义由车端定义，这边照抄，猜错了比不显示更糟。
TASK_STATUS = {
    0: ("未知", ""), 1: ("空闲", ""), 2: ("执行中", "ok"),
    11: ("暂停", "warn"), 12: ("阻塞", "bad"), 21: ("正常结束", "ok"),
    23: ("用户取消", ""), 24: ("终止", "warn"), 25: ("补充清扫", "ok"),
    100: ("异常", "bad"),
}


def task_status_text(v):
    """状态码 → (中文, 配色档位)。未定义的状态码原样显示数字。"""
    try:
        n = int(v)
    except Exception:
        return ("-", "")
    if n in TASK_STATUS:
        return TASK_STATUS[n]
    return (str(n), "")


# 软件版本：/home/aoting/<ws>/version.yaml 里的 version 字段。
# 它不是话题，是文件 —— 由本脚本直接读，不经过 rospy，
# 所以即使 ROS 整个不可用，这一项照样能显示出来。
# 工作空间名各车不同（已知 hrg_bee_dev_v3 / aotingos），两个都试。
SWVER_FILE = "version.yaml"
SWVER_WS = ("hrg_bee_dev_v3", "aotingos")
SWVER_HOMES = ("/home/aoting", "/root")
SWVER_LOCK = threading.Lock()
SWVER_STATE = {"ok": False, "err": "", "version": "", "src": "", "at": 0}

# 常见 ROS 的 python 包路径。SSH exec 起来的 shell 通常没有 source
# setup.bash，rospy 会 import 不到 —— 手动补上，失败也只是不显示故障，
# 不影响 CPU / 内存的采集。
ROS_PYPATH = (
    "/opt/ros/noetic/lib/python3/dist-packages",
    "/opt/ros/melodic/lib/python2.7/dist-packages",
    "/opt/ros/kinetic/lib/python2.7/dist-packages",
)


def _ensure_ros_path():
    for p in ROS_PYPATH:
        try:
            if os.path.isdir(p) and p not in sys.path:
                sys.path.append(p)
        except Exception:
            pass


# 车端工作空间名。不同车不一样，两个都试（实地确认过的布局）：
#   源码  /home/aoting/<ws>/src/cln_msgs        ← 只有 .msg 文本，不能 import
#   产物  /home/aoting/<ws>/devel/lib/python3/dist-packages
MSG_WS = ("hrg_bee_dev_v3", "aotingos")
MSG_HOMES = ("/home/aoting", "/root")

# 本次实际追加过的路径。加载失败时回传给页面，便于把真实布局反馈回来。
MSG_TRIED = []


def _msg_site_paths():
    """候选 site 目录，按可能性从高到低。

    踩过的坑：源码目录和产物目录【同名，都叫 cln_msgs】。用 find 按包名
    一把抓会先捞到 src 下的源码目录 —— 那里面只有 .msg 文本和 package.xml，
    压根没有 msg/ 包，加进 sys.path 也 import 不到，还把后面真正能用的
    devel 路径盖住了。所以改成：先按已知工作空间精确拼，再用 find 兜底，
    且兜底只收【带 msg/ 子目录】的（编译产物才有）。
    """
    import glob as _g
    out = []

    def add(p):
        p = (p or "").rstrip("/")
        if p and p not in out and os.path.isdir(p):
            out.append(p)

    # 1) 已知工作空间的 devel / install 空间（python 版本目录通配，
    #    免得写死 python2.7 还是 python3）
    for ws in MSG_WS:
        for home in MSG_HOMES:
            base = home + "/" + ws
            if not os.path.isdir(base):
                continue
            for space in ("devel", "install"):
                for py in sorted(_g.glob(base + "/" + space + "/lib/python*")):
                    add(py + "/dist-packages")
                    add(py + "/site-packages")

    # 2) 任意 home 下的任意工作空间（ws 名又换了的兜底）
    for pat in ("/home/*/*/devel/lib/python*/dist-packages",
                "/home/*/*/install/lib/python*/dist-packages",
                "/opt/ros/*/lib/python*/dist-packages"):
        for p in sorted(_g.glob(pat)):
            add(p)

    # 3) find 兜底：只收 .../dist-packages/<pkg>/msg 这种编译产物
    try:
        import subprocess
        raw = subprocess.check_output(
            ["find", "/home", "/opt", "/root", "-maxdepth", "9",
             "-type", "d", "-name", "msg"],
            stderr=subprocess.DEVNULL, timeout=15).decode("utf-8", "replace")
        for line in raw.splitlines():
            d = line.strip()                       # .../dist-packages/<pkg>/msg
            if not d:
                continue
            pkg_dir = os.path.dirname(d)           # .../dist-packages/<pkg>
            site = os.path.dirname(pkg_dir)        # .../dist-packages
            if os.path.isdir(os.path.join(pkg_dir, "msg")):
                add(site)
    except Exception as e:
        log("fault: find msg dir failed %r" % (e,))
    return out


def _load_msg_class(typ):
    """按 "pkg/Msg" 加载消息类；失败就逐个补路径重试。

    逐个试（而不是把所有候选一次性塞进 sys.path）是为了两件事：
      1. 出错时能说清"试过哪些、在哪一个上成的"，方便反馈真实布局；
      2. 避免 src 源码目录这类无效路径抢占 import 结果。
    """
    import importlib
    mod, _, cn = typ.partition("/")
    if not mod or not cn:
        raise ValueError("消息类型格式不对: %r" % (typ,))

    def _imp():
        return getattr(importlib.import_module(mod + ".msg"), cn)

    try:
        return _imp()
    except Exception as first:
        last = first
        MSG_TRIED[:] = []
        for p in _msg_site_paths():
            if p not in sys.path:
                sys.path.insert(0, p)
            MSG_TRIED.append(p)
            # 清掉 import 缓存，否则 importlib 仍沿用刚才失败的结论
            for k in (mod, mod + ".msg"):
                sys.modules.pop(k, None)
            try:
                cls = _imp()
                log("fault: " + mod + " loaded from " + p)
                return cls
            except Exception as e:
                last = e
        raise last


def _hex_code(code):
    """故障码转十六进制显示：1589 -> 0x0635。

    十进制原样保留在旁边 —— 排查时对着码表大概率还是十进制，
    两种都给，省得心算。负数/非数字返回空串，交给前端原样显示。
    """
    try:
        v = int(code)
    except Exception:
        return ""
    if v < 0:
        return ""
    return "0x%04x" % v


def _ros_time(ev):
    """event_time 可能是 rospy 的 Time 对象，也可能是纯 dict。"""
    secs = None
    if isinstance(ev, dict):
        secs = ev.get("secs")
    else:
        try:
            secs = ev.secs
        except Exception:
            secs = None
    try:
        secs = int(secs or 0)
    except Exception:
        secs = 0
    if not secs:
        return 0, ""
    try:
        return secs, time.strftime("%m-%d %H:%M:%S", time.localtime(secs))
    except Exception:
        return secs, ""


def _to_dict(m, depth=0):
    """把 rospy 消息递归成普通 dict。

    不做任何类型假设：自定义消息的字段名将来可能变，遍历 __slots__
    拿全量字段，页面负责挑它认识的那几个。
    """
    if m is None or depth > 5:
        return None
    if isinstance(m, bool) or isinstance(m, int) or isinstance(m, float):
        return m
    if isinstance(m, dict):
        return dict((k, _to_dict(v, depth + 1)) for k, v in m.items())
    if isinstance(m, (list, tuple)):
        return [_to_dict(x, depth + 1) for x in m[:50]]
    if isinstance(m, bytes):
        return m.decode("utf-8", "replace")
    if isinstance(m, str):
        return m
    slots = getattr(m, "__slots__", None)
    if slots:
        d = {}
        for f in slots:
            try:
                v = getattr(m, f)
            except Exception:
                continue
            d[f] = _to_dict(v, depth + 1)
        return d
    return str(m)


def _items_of(d):
    """从整条消息里挑出要展示的几条：故障码 / 内容 / 级别 / 节点 / 时间。"""
    out = []
    for a in (d.get("alarms") or []):
        if not isinstance(a, dict):
            continue
        t, tstr = _ros_time(a.get("event_time"))
        data = a.get("event_data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        out.append({
            "code": a.get("event_code"),
            "codeHex": _hex_code(a.get("event_code")),
            "data": data,
            "level": a.get("event_level"),
            "node": a.get("node_name"),
            "t": t,
            "tstr": tstr,
        })
    # 严重的排前面，同级按时间新的在前
    out.sort(key=lambda r: (-(r.get("level") or 0), -(r.get("t") or 0)))
    return out


def _fault_cb(msg):
    try:
        d = _to_dict(msg)
        items = _items_of(d)
        with FAULT_LOCK:
            FAULT_STATE["ok"] = True
            FAULT_STATE["err"] = ""
            FAULT_STATE["items"] = items
            FAULT_STATE["level"] = d.get("alarm_level")
            FAULT_STATE["at"] = time.time()
    except Exception as e:
        with FAULT_LOCK:
            FAULT_STATE["err"] = "解析失败: " + str(e)[:80]
        log("fault: parse failed %r" % (e,))


def _wait_topic(rospy, topic, tries=10, gap=1.0):
    """话题可能比采集脚本起得晚（节点还没上来），给一段时间等它出现。"""
    for _i in range(tries):
        try:
            for name, typ in rospy.get_published_topics():
                if name == topic:
                    return typ
        except Exception:
            pass
        time.sleep(gap)
    return ""


def _hw_cb(msg):
    """硬件状态：全量字段照收，页面挑它认识的那几个。

    和故障话题同一套思路 —— 不写死字段名，消息将来加字段不会让它整体失败。
    """
    try:
        d = _to_dict(msg)
        with HW_LOCK:
            HW_STATE["ok"] = True
            HW_STATE["err"] = ""
            HW_STATE["fields"] = d if isinstance(d, dict) else {}
            HW_STATE["at"] = time.time()
    except Exception as e:
        with HW_LOCK:
            HW_STATE["err"] = "解析失败: " + str(e)[:80]
        log("hw: parse failed %r" % (e,))


def _slam_cb(msg):
    """SLAM 版本：全量照收，页面挑 sw_version。"""
    try:
        d = _to_dict(msg)
        with SLAM_LOCK:
            SLAM_STATE["ok"] = True
            SLAM_STATE["err"] = ""
            SLAM_STATE["fields"] = d if isinstance(d, dict) else {}
            SLAM_STATE["at"] = time.time()
    except Exception as e:
        with SLAM_LOCK:
            SLAM_STATE["err"] = "解析失败: " + str(e)[:80]
        log("slam: parse failed %r" % (e,))


def _clean_num(v):
    """过滤未初始化内存读出来的垃圾值，返回 None 表示不该显示。

    车端任务消息里出现过 group_cleaned_progress: 4.57e-41、
    group_status: -1593015442 这类值 —— 那是发布方没给字段赋值，
    float64/int32 直接把随机内存当成了数值。显示出来会被当成
    "真实进度/状态"，比不显示危险得多，所以一律剔除。
    """
    try:
        f = float(v)
    except Exception:
        return None
    if f != f:               # NaN
        return None
    if f != 0 and abs(f) < 1e-30:     # denormal，本质是未初始化内存
        return None
    return f


def _task_cb(msg):
    """当前任务：认 task_echo.task_name 与 task_echo.task_status.status。"""
    try:
        d = _to_dict(msg)
        echo = (d or {}).get("task_echo") or {}
        if not isinstance(echo, dict):
            echo = {}
        st = echo.get("task_status")
        stv = st.get("status") if isinstance(st, dict) else st
        with TASK_LOCK:
            TASK_STATE["ok"] = True
            TASK_STATE["err"] = ""
            TASK_STATE["echo"] = echo
            TASK_STATE["name"] = echo.get("task_name") or ""
            TASK_STATE["status"] = stv
            TASK_STATE["at"] = time.time()
    except Exception as e:
        with TASK_LOCK:
            TASK_STATE["err"] = "解析失败: " + str(e)[:80]
        log("task: parse failed %r" % (e,))


def _queue_cb(msg):
    """任务队列：只取 group_name 与 group_id，脏字段一律不带。

    为什么只取这两个：taskgroup_list 里的 group_status 与
    group_cleaned_* 在这台车上是未初始化的垃圾值（见 _clean_num），
    带出去只会误导。将来车端修好了，再把字段放开即可。
    """
    try:
        d = _to_dict(msg)
        groups = []
        for g in ((d or {}).get("taskgroup_list") or []):
            if not isinstance(g, dict):
                continue
            nm = g.get("group_name")
            if nm is None or nm == "":
                continue
            gid = g.get("group_id")
            # 面积/时间只在数值看着可信时才带；脏值直接留空让页面跳过
            area = _clean_num(g.get("task_area"))
            est = _clean_num(g.get("estimated_time"))
            groups.append({
                "name": str(nm),
                "id": str(gid) if gid is not None else "",
                "area": round(area, 1) if area else None,
                "est": int(est) if est else None,
            })
        with QUEUE_LOCK:
            QUEUE_STATE["ok"] = True
            QUEUE_STATE["err"] = ""
            QUEUE_STATE["groups"] = groups
            QUEUE_STATE["at"] = time.time()
    except Exception as e:
        with QUEUE_LOCK:
            QUEUE_STATE["err"] = "解析失败: " + str(e)[:80]
        log("queue: parse failed %r" % (e,))


def _num(v):
    """消息字段 → 浮点数。取不到、NaN、无穷大都返回 None。

    NaN 要单独挡掉：上游偶发的除零会让它出现，直接显示就是页面上
    一个 "nan m/s"，比不显示更糟（看起来像解析坏了）。
    """
    try:
        f = float(v)
    except Exception:
        return None
    if f != f:
        return None
    if f == float("inf") or f == float("-inf"):
        return None
    return f


def _cmdvel_cb(msg):
    """只取两个值：linear.x 线速度(m/s)、angular.z 角速度(rad/s)。

    用 getattr 逐层取，不走 _to_dict 全量递归：cmd_vel 常见 50Hz 发布，
    每秒几十次地把整条消息递归成 dict 纯属浪费，而且这两个字段的位置
    是 geometry_msgs/Twist 定死的，不会变。
    """
    try:
        lin = getattr(msg, "linear", None)
        ang = getattr(msg, "angular", None)
        lx = _num(getattr(lin, "x", None))
        az = _num(getattr(ang, "z", None))
    except Exception as e:
        with CMDVEL_LOCK:
            CMDVEL_STATE["err"] = "解析失败: " + str(e)[:80]
        log("cmdvel: parse failed %r" % (e,))
        return
    with CMDVEL_LOCK:
        CMDVEL_STATE["ok"] = True
        CMDVEL_STATE["err"] = ""
        CMDVEL_STATE["lin_x"] = lx
        CMDVEL_STATE["ang_z"] = az
        CMDVEL_STATE["at"] = time.time()


def _cmdvel_watch(rospy):
    """轮询 /cmd_vel 是否发布，动态订阅 / 退订。

    和 /odom_origin 共用 _poll_subscribe，只有"没发布时怎么措辞"
    不一样：cmd_vel 静止时不发是【正常状态】，要说"当前无控制指令"
    而不是"话题未发布"，否则会被当成故障。
    """
    _poll_subscribe(rospy, CMDVEL_TOPIC, CMDVEL_LOCK, CMDVEL_STATE,
                    _cmdvel_cb, "当前无控制指令（话题未发布）")


def _odom_pose(msg):
    """/odom_origin 的位姿 (x, y, yaw)。雷达点云要靠它搬到地图系。

    注意：这是 odom 系下的位姿，假定与 map 系重合。SLAM 正常运行时
    两者通常只差一个很小的漂移，用来看点云相对地图的位置够用；真要
    厘米级对齐得走 tf 查 map->base_link。
    """
    try:
        p = getattr(msg, "pose", None)
        p = getattr(p, "pose", p)          # nav_msgs/Odometry 是两层
        pos = getattr(p, "position", None)
        ori = getattr(p, "orientation", None)
        if pos is None or ori is None:
            return None
        px = _num(getattr(pos, "x", None))
        py = _num(getattr(pos, "y", None))
        qx = _num(getattr(ori, "x", None)) or 0.0
        qy = _num(getattr(ori, "y", None)) or 0.0
        qz = _num(getattr(ori, "z", None))
        qw = _num(getattr(ori, "w", None))
        if px is None or py is None or qz is None or qw is None:
            return None
        yaw = math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))
        return [px, py, yaw]
    except Exception:
        return None


def _odom_cb(msg):
    """/odom_origin：取 twist.twist.linear.x 与 twist.twist.angular.z。

    和 cmd_vel 一样不做全量递归 —— 里程计通常 50Hz 发布。
    """
    try:
        tw = getattr(msg, "twist", None)
        tw = getattr(tw, "twist", tw)      # nav_msgs/Odometry 是两层
        lin = getattr(tw, "linear", None)
        ang = getattr(tw, "angular", None)
        lx = _num(getattr(lin, "x", None))
        az = _num(getattr(ang, "z", None))
    except Exception as e:
        with ODOM_LOCK:
            ODOM_STATE["err"] = "解析失败: " + str(e)[:80]
        log("odom: parse failed %r" % (e,))
        return
    with ODOM_LOCK:
        ODOM_STATE["ok"] = True
        ODOM_STATE["err"] = ""
        ODOM_STATE["lin_x"] = lx
        ODOM_STATE["ang_z"] = az
        ODOM_STATE["at"] = time.time()
        ODOM_STATE["pose"] = _odom_pose(msg)


def _axslam_cb(msg):
    """/ax_slam_state：定位状态，全量收（消息只有 8 个字段）。"""
    try:
        d = _to_dict(msg)
        with AXSLAM_LOCK:
            AXSLAM_STATE["ok"] = True
            AXSLAM_STATE["err"] = ""
            AXSLAM_STATE["fields"] = d if isinstance(d, dict) else {}
            AXSLAM_STATE["at"] = time.time()
    except Exception as e:
        with AXSLAM_LOCK:
            AXSLAM_STATE["err"] = "解析失败: " + str(e)[:80]
        log("axslam: parse failed %r" % (e,))


def _map_cb(msg):
    """/map_info_v3：全量收，额外把 base_info.name / base_info.id 提出来。

    两个都是 PC 端拼地图目录的关键：
      /home/aoting/user_config/map_database/<name|id>/map.png
    name 可能改过（或含特殊字符导致目录名不一致），id 是建图时生成的、
    不会变，所以 name 找不到时要用 id 兜底 —— 这也是为什么两个都要存。

    取不到就留空字符串 —— 页面会提示"话题里没有地图名"，
    并退回让用户从地图目录里手动挑一张。
    """
    try:
        d = _to_dict(msg)
        name = ""
        mid = ""
        try:
            bi = (d or {}).get("base_info")
            if isinstance(bi, dict):
                name = str(bi.get("name") or "").strip()
                mid = str(bi.get("id") or "").strip()
        except Exception:
            name = ""
        with MAP_LOCK:
            MAP_STATE["ok"] = True
            MAP_STATE["err"] = ""
            MAP_STATE["fields"] = d if isinstance(d, dict) else {}
            MAP_STATE["name"] = name
            MAP_STATE["id"] = mid
            MAP_STATE["at"] = time.time()
    except Exception as e:
        with MAP_LOCK:
            MAP_STATE["err"] = "解析失败: " + str(e)[:80]
        log("map: parse failed %r" % (e,))


def _poll_subscribe(rospy, topic, lock, state, cb, idle_msg,
                    want_fn=None, off_msg=""):
    """轮询订阅：每秒看一次话题在不在，出现就订阅，消失就退订。

    为什么不用 _setup_topic 那套"起脚本时等 10 秒"：
    话题可能比采集脚本起得晚（节点还没上来），也可能中途停了又起。
    一次性判定失败后就再也不看，会把后者的变化永远漏掉。

    want_fn：可选的开关。返回 False 时【真退订】而不是"不显示" ——
    回调本身才是开销来源（高频 + 逐点三角函数），只是不画出来没用。
    """
    sub = [None]          # 用列表装，闭包里才能改
    while True:
        try:
            time.sleep(1.0)
        except Exception:
            return

        if want_fn is not None and not want_fn():
            if sub[0] is not None:
                try:
                    sub[0].unregister()
                except Exception:
                    pass
                sub[0] = None
                log("%s: unsubscribed (switch off)" % topic)
            with lock:
                state["ok"] = False
                state["err"] = off_msg or ("未开启（勾选后才订阅 " + topic + "）")
                state["pts"] = []
                state["n"] = 0
                state["live"] = False
            continue

        typ = ""
        try:
            for name, ty in rospy.get_published_topics():
                if name == topic:
                    typ = ty
                    break
        except Exception as e:
            log("%s: get_published_topics failed %r" % (topic, e))

        if not typ:
            # 话题不在发布列表里 —— 退订，下次出现时能干净地重新订阅
            if sub[0] is not None:
                try:
                    sub[0].unregister()
                except Exception:
                    pass
                sub[0] = None
            with lock:
                state["ok"] = False
                state["err"] = idle_msg
                if "lin_x" in state:
                    state["lin_x"] = None
                    state["ang_z"] = None
                state["live"] = False
            continue

        if sub[0] is None:
            try:
                cls = _load_msg_class(typ)
                # queue_size=1：只要最新一条。高频话题若排队积压，
                # 处理到的是几秒前的旧值，实时性反而更差。
                sub[0] = rospy.Subscriber(topic, cls, cb, queue_size=1)
            except Exception as e:
                with lock:
                    state["err"] = ("无法加载消息类型 " + str(typ)
                                    + "（" + str(e)[:60] + "）")
                log("%s: msg load failed %r" % (topic, e))
                continue
            with lock:
                state["type"] = typ
                state["ok"] = True
                state["err"] = ""


def _odom_watch(rospy):
    _poll_subscribe(rospy, ODOM_TOPIC, ODOM_LOCK, ODOM_STATE, _odom_cb,
                    "话题未发布（里程计节点未起？）")


def _axslam_watch(rospy):
    _poll_subscribe(rospy, AXSLAM_TOPIC, AXSLAM_LOCK, AXSLAM_STATE,
                    _axslam_cb, "话题未发布（定位节点未起？）")


def _map_watch(rospy):
    """地图只解析一次：拿到第一条就退订，之后不再自动更新。

    为什么改掉原来的每秒轮询：原来每次采样都把话题里的地图名回传，
    前端一比对发现"名字变了"就重新拉图 —— 用户手动选了另一张地图，
    下一帧立刻被话题里的名字覆盖回去，"点了别的图又自己跳回来"就是
    这么来的。地图名在连接后基本不变，没必要一直盯着。

    需要重新解析时（前端点"重新解析话题"），PC 端下发
    {"map_refresh": true}，这里再订阅一次、拿到后继续退订。
    """
    sub = [None]
    got = [False]
    while True:
        try:
            time.sleep(1.0)
        except Exception:
            return

        # 已拿到且没被要求刷新 —— 保持退订，不再解析
        if got[0] and not MAP_REFRESH[0]:
            if sub[0] is not None:
                try:
                    sub[0].unregister()
                except Exception:
                    pass
                sub[0] = None
            continue

        if MAP_REFRESH[0]:
            MAP_REFRESH[0] = False
            got[0] = False
            with MAP_LOCK:
                MAP_STATE["ok"] = False
                MAP_STATE["err"] = "正在重新解析 …"

        typ = ""
        try:
            for nm2, ty2 in rospy.get_published_topics():
                if nm2 == MAP_TOPIC:
                    typ = ty2
                    break
        except Exception as e:
            log("map: get_published_topics failed %r" % (e,))

        if not typ:
            with MAP_LOCK:
                MAP_STATE["ok"] = False
                MAP_STATE["err"] = "话题未发布（建图/定位节点未起？）"
            continue

        if sub[0] is None:
            try:
                sub[0] = rospy.Subscriber(MAP_TOPIC, _load_msg_class(typ),
                                          _map_cb, queue_size=1)
                log("map: subscribed (once)")
            except Exception as e:
                with MAP_LOCK:
                    MAP_STATE["ok"] = False
                    MAP_STATE["err"] = "消息类加载失败: %r" % (e,)
                sub[0] = None

        with MAP_LOCK:
            if MAP_STATE.get("ok") and MAP_STATE.get("name"):
                got[0] = True
                MAP_STATE["done"] = True
                if sub[0] is not None:
                    try:
                        sub[0].unregister()
                    except Exception:
                        pass
                    sub[0] = None
                log("map: got name=%s, unsubscribed" % MAP_STATE.get("name"))


# ═══════════════ 自定义话题 ═══════════════
def _dict_limited(m, depth=0, flag=None):
    """自定义消息 → dict，带层深 / 数组长度截断。

    点云、图像这类消息全量 dict 化能到几 MB，每秒往 PC 端传会把
    SSH 通道和页面一起拖死。这里按层深和数组条数截断，再由调用方
    按字符数兜一刀，并把"已截断"如实告诉用户 —— 悄悄少给内容，
    比明说截断危险得多。
    """
    if flag is None:
        flag = [False]
    if depth > CUSTOM_MAX_DEPTH:
        flag[0] = True
        return "…"
    if isinstance(m, bool) or isinstance(m, int) or isinstance(m, float):
        return m
    if isinstance(m, str):
        return m
    if isinstance(m, bytes):
        return m.decode("utf-8", "replace")
    if isinstance(m, dict):
        return dict((k, _dict_limited(v, depth + 1, flag))
                    for k, v in m.items())
    if isinstance(m, (list, tuple)):
        if len(m) > CUSTOM_MAX_ITEMS:
            flag[0] = True
            return [_dict_limited(x, depth + 1, flag)
                    for x in m[:CUSTOM_MAX_ITEMS]]
        return [_dict_limited(x, depth + 1, flag) for x in m]
    slots = getattr(m, "__slots__", None)
    if slots:
        d = {}
        for f in slots:
            try:
                v = getattr(m, f)
            except Exception:
                continue
            d[f] = _dict_limited(v, depth + 1, flag)
        return d
    return str(m)


def _custom_cb(topic):
    """回调只存原始消息，不在这里转 dict。

    转换放在轮询线程里做（≤1Hz）：自定义话题可能是 50Hz 的大消息，
    每条都递归一遍会把车端 CPU 吃进去 —— 而被测对象正是这台车。
    """
    def f(msg):
        with CUSTOM_LOCK:
            st = CUSTOM_STATE.get(topic)
            if st is None:
                return
            st["raw"] = msg
            st["at"] = time.time()
    return f


def _custom_watch(rospy):
    """按 PC 端的要求增量订阅 / 退订自定义话题。"""
    while True:
        try:
            time.sleep(1.0)
        except Exception:
            return
        with CUSTOM_LOCK:
            want = set(CUSTOM_WANT)

        # 退订必须先做，而且要放在"没有想订阅的就跳过"之前 ——
        # 否则取消最后一个话题时会直接 continue 掉，订阅永远留着，
        # 车端继续往一个没人看的回调里塞消息。
        for t in list(CUSTOM_SUB.keys()):
            if t not in want:
                try:
                    CUSTOM_SUB[t].unregister()
                except Exception:
                    pass
                CUSTOM_SUB.pop(t, None)
                CUSTOM_STATE.pop(t, None)
        if not want:
            continue

        types = {}
        try:
            for name, ty in rospy.get_published_topics():
                types[name] = ty
        except Exception:
            continue

        for t in sorted(want):
            with CUSTOM_LOCK:
                st = CUSTOM_STATE.get(t)
                if st is None:
                    st = {"ok": False, "err": "", "type": "", "at": 0,
                          "fields": None, "note": "", "conv": 0, "raw": None}
                    CUSTOM_STATE[t] = st
            ty = types.get(t)
            if not ty:
                with CUSTOM_LOCK:
                    st["ok"] = False
                    st["err"] = ("话题未发布，或名字不对（注意大小写和开头的 /）"
                                 if not st["type"]
                                 else "话题已停止发布")
                    st["raw"] = None
                continue

            if t in CUSTOM_SUB:
                # 已订阅：有新消息就转换一次
                with CUSTOM_LOCK:
                    raw = st.get("raw")
                    at = st.get("at") or 0
                    conv = st.get("conv") or 0
                if raw is not None and at > conv:
                    flag = [False]
                    try:
                        d = _dict_limited(raw, 0, flag)
                        s = json.dumps(d, ensure_ascii=False)
                    except Exception as e:
                        with CUSTOM_LOCK:
                            st["err"] = "解析失败: " + str(e)[:60]
                        continue
                    note = "已截断（层深 > 3 或数组 > 25 条）" if flag[0] else ""
                    if len(s) > CUSTOM_MAX_CHARS:
                        note = ("内容过大，仅回传前 " + str(CUSTOM_MAX_CHARS)
                                + " 字符")
                    with CUSTOM_LOCK:
                        st["fields"] = d
                        st["conv"] = at
                        st["ok"] = True
                        st["err"] = ""
                        st["note"] = note
                continue

            try:
                cls = _load_msg_class(ty)
            except Exception as e:
                with CUSTOM_LOCK:
                    st["ok"] = False
                    st["err"] = ("无法加载消息类型 " + str(ty)
                                 + "（" + str(e)[:50] + "）")
                log("custom: %s msg load failed %r" % (t, e))
                continue
            try:
                CUSTOM_SUB[t] = rospy.Subscriber(t, cls, _custom_cb(t),
                                                 queue_size=1)
            except Exception as e:
                with CUSTOM_LOCK:
                    st["err"] = "订阅失败: " + str(e)[:50]
                continue
            with CUSTOM_LOCK:
                st["type"] = ty
                st["ok"] = True
                st["err"] = ""


def _unquote(v):
    """去掉成对的首尾引号。

    用 chr(34)/chr(39) 而不是直接写字面量：本文件禁止出现单引号
    （会被 shell_quote 多层转义后变形），双引号同理少写更省心。
    """
    if len(v) > 1 and v[0] == v[-1] and v[0] in (chr(34), chr(39)):
        return v[1:-1].strip()
    return v


def _parse_swver(txt):
    """从 version.yaml 取 version: 的值。

    先试 PyYAML（ROS 机器上一般都有），失败再按行匹配 ——
    这个文件通常只有一行，为它引入依赖不值得，且车上的 python
    未必装得齐。
    """
    try:
        import yaml
        d = yaml.safe_load(txt)
        if isinstance(d, dict):
            v = d.get("version")
            if v is not None and str(v).strip() != "":
                return str(v).strip()
    except Exception:
        pass
    for ln in (txt or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        i = s.find(":")
        if i <= 0:
            continue
        if s[:i].strip().lower() != "version":
            continue
        v = _unquote(s[i + 1:].strip())
        if v:
            return v
    return ""


def _swver_files():
    import glob as _g
    out = []
    for ws in SWVER_WS:
        for home in SWVER_HOMES:
            p = home + "/" + ws + "/" + SWVER_FILE
            if os.path.isfile(p) and p not in out:
                out.append(p)
    for pat in ("/home/*/*/" + SWVER_FILE, "/home/*/" + SWVER_FILE):
        for p in sorted(_g.glob(pat)):
            if os.path.isfile(p) and p not in out:
                out.append(p)
    return out


def _swver_refresh():
    """读一次版本文件。

    不在启动时只读一次：OTA 之后文件会被改写，页面会一直显示旧号。
    60 秒重读一次成本极低（一个小文件），却能跟上变化。
    """
    files = _swver_files()
    if not files:
        with SWVER_LOCK:
            SWVER_STATE.update({
                "ok": False,
                "err": "未找到 " + SWVER_FILE + "（试过 "
                       + " / ".join(SWVER_WS) + "）",
                "version": "", "src": "", "at": time.time()})
        return
    for p in files:
        try:
            with open(p, "r") as f:
                txt = f.read()
        except Exception as e:
            log("swver: read failed %s %r" % (p, e))
            continue
        v = _parse_swver(txt)
        if v:
            with SWVER_LOCK:
                SWVER_STATE.update({"ok": True, "err": "", "version": v,
                                    "src": p, "at": time.time()})
            return
    with SWVER_LOCK:
        SWVER_STATE.update({
            "ok": False,
            "err": files[0] + " 里没有 version 字段",
            "version": "", "src": files[0], "at": time.time()})


def _set_err(lock, state, msg):
    with lock:
        state["err"] = msg


def _setup_topic(rospy, topic, lock, state, cb):
    """等话题 → 加载消息类 → 订阅。返回是否成功。

    任何一步失败都只把原因记进 state，不影响另一个话题 —— 比如
    /hardware_state 没发布，故障那块照样要能看。
    """
    # 先置成"等待中"：否则在话题出现之前，页面看到的是 ok=false 且 err
    # 为空 —— 会被显示成"暂无数据"，比报错更误导。
    with lock:
        state["err"] = "正在等待话题 " + topic + " 发布…"

    t0 = time.time()
    typ = _wait_topic(rospy, topic)
    if not typ:
        with lock:
            # 说实际等了多久，而不是写死 10 —— 等待参数改了文案就会说谎
            state["err"] = ("话题 " + topic + " 未发布（等了 "
                            + str(int(time.time() - t0 + 0.5)) + " 秒仍没有）")
        log("ros: %s not published" % topic)
        return False

    try:
        cls = _load_msg_class(typ)
    except Exception as e:
        # 把试过的路径一并回传。只看到 "No module named" 是没法定位的 ——
        # 真正有用的信息是"它在哪些目录里找过、都没找到"。
        tried = ""
        if MSG_TRIED:
            tried = "；已试 " + str(len(MSG_TRIED)) + " 个路径，最后 3 个: " \
                    + ", ".join(MSG_TRIED[-3:])
        with lock:
            state["err"] = ("无法加载消息类型 " + typ + "（" + str(e)[:60] + "）"
                            + tried)
        log("ros: %s msg load failed %r tried=%s" % (topic, e, MSG_TRIED))
        return False

    with lock:
        state["type"] = typ
        state["ok"] = True       # 已订阅上；没消息就是"暂无数据"
        state["err"] = ""
    try:
        rospy.Subscriber(topic, cls, cb)
        log("ros: subscribed %s (%s)" % (topic, typ))
        return True
    except Exception as e:
        with lock:
            state["err"] = "订阅失败（" + str(e)[:60] + "）"
        log("ros: %s subscribe failed %r" % (topic, e))
        return False


def _ros_thread():
    """一个线程管五个话题。

    rospy.init_node 只能成功调一次，所以不能给每个话题各起一个线程
    各自 init —— 第二个必定失败。这里 init 一次，再把订阅都挂上。
    """
    try:
        _ensure_ros_path()
        import rospy
    except Exception as e:
        msg = "rospy 不可用（" + str(e)[:60] + "）"
        _set_err(FAULT_LOCK, FAULT_STATE, msg)
        _set_err(HW_LOCK, HW_STATE, msg)
        _set_err(SLAM_LOCK, SLAM_STATE, msg)
        _set_err(TASK_LOCK, TASK_STATE, msg)
        _set_err(QUEUE_LOCK, QUEUE_STATE, msg)
        _set_err(CMDVEL_LOCK, CMDVEL_STATE, msg)
        _set_err(ODOM_LOCK, ODOM_STATE, msg)
        _set_err(AXSLAM_LOCK, AXSLAM_STATE, msg)
        _set_err(MAP_LOCK, MAP_STATE, msg)
        log("ros: rospy unavailable %r" % (e,))
        return

    try:
        # anonymous 避免和车上同名节点打架；disable_signals 是因为
        # 这里不是主线程，不能让 rospy 抢注册信号处理器。
        rospy.init_node("aoting_stat_agent", anonymous=True,
                        disable_signals=True)
        _tf_init()
        _read_frames()
        # 位姿走独立线程持续更新（对应 C++ RealtimePose::Start）。
        # 必须在 init_node 之后，且不能阻塞下面的订阅注册。
        try:
            threading.Thread(target=_pose_thread).start()
            log("pose thread started")
        except Exception as e:
            log("pose thread failed: %r" % (e,))
    except Exception as e:
        msg = "ROS master 不可用（" + str(e)[:60] + "）"
        _set_err(FAULT_LOCK, FAULT_STATE, msg)
        _set_err(HW_LOCK, HW_STATE, msg)
        _set_err(SLAM_LOCK, SLAM_STATE, msg)
        _set_err(TASK_LOCK, TASK_STATE, msg)
        _set_err(QUEUE_LOCK, QUEUE_STATE, msg)
        _set_err(CMDVEL_LOCK, CMDVEL_STATE, msg)
        _set_err(ODOM_LOCK, ODOM_STATE, msg)
        _set_err(AXSLAM_LOCK, AXSLAM_STATE, msg)
        _set_err(MAP_LOCK, MAP_STATE, msg)
        log("ros: init_node failed %r" % (e,))
        return

    n = 0
    if _setup_topic(rospy, FAULT_TOPIC, FAULT_LOCK, FAULT_STATE, _fault_cb):
        n += 1
    if _setup_topic(rospy, HW_TOPIC, HW_LOCK, HW_STATE, _hw_cb):
        n += 1
    if _setup_topic(rospy, SLAM_TOPIC, SLAM_LOCK, SLAM_STATE, _slam_cb):
        n += 1
    if _setup_topic(rospy, TASK_TOPIC, TASK_LOCK, TASK_STATE, _task_cb):
        n += 1
    if _setup_topic(rospy, QUEUE_TOPIC, QUEUE_LOCK, QUEUE_STATE, _queue_cb):
        n += 1

    # 这三个走单独的轮询线程，不受上面"一次性等待"的影响：
    # 话题可能起得晚、也可能中途停了又起，一次性判定会漏掉后续变化。
    threading.Thread(target=_cmdvel_watch, args=(rospy,), daemon=True).start()
    threading.Thread(target=_odom_watch, args=(rospy,), daemon=True).start()
    threading.Thread(target=_axslam_watch, args=(rospy,), daemon=True).start()
    threading.Thread(target=_map_watch, args=(rospy,), daemon=True).start()
    threading.Thread(target=_scan_watch, args=(rospy,), daemon=True).start()
    threading.Thread(target=_d2l_watch, args=(rospy,), daemon=True).start()
    threading.Thread(target=_pc3d_watch, args=(rospy,), daemon=True).start()
    threading.Thread(target=_rgb_watch, args=(rospy,), daemon=True).start()
    threading.Thread(target=_custom_watch, args=(rospy,), daemon=True).start()

    if not n:
        log("ros: no topic subscribed (cmd_vel/odom/axslam/map 仍在轮询)")
    try:
        rospy.spin()
    except Exception as e:
        log("ros: spin stopped %r" % (e,))


def start_ros():
    """起 ROS 订阅线程：故障 / 硬件状态 / SLAM / 当前任务 / 任务队列
    都在同一个线程里
    （init_node 只能成功调一次，各起各的第二个必定失败）。

    导航状态 /cmd_vel 不在这里一次性订阅 —— 它静止时不发布，
    由 _cmdvel_watch 单独轮询（见 CMDVEL_TOPIC 处的说明）。"""
    t = threading.Thread(target=_ros_thread)
    t.daemon = True
    t.start()
    return t


def slam_snapshot():
    with SLAM_LOCK:
        return {"ok": SLAM_STATE["ok"], "err": SLAM_STATE["err"],
                "fields": dict(SLAM_STATE["fields"]), "at": SLAM_STATE["at"],
                "type": SLAM_STATE["type"], "topic": SLAM_TOPIC}


def map_snapshot():
    with MAP_LOCK:
        return {"ok": MAP_STATE["ok"], "err": MAP_STATE["err"],
                "fields": dict(MAP_STATE["fields"]),
                "name": MAP_STATE["name"], "id": MAP_STATE["id"],
                "at": MAP_STATE["at"],
                "type": MAP_STATE["type"], "topic": MAP_TOPIC}


def swver_snapshot():
    with SWVER_LOCK:
        return {"ok": SWVER_STATE["ok"], "err": SWVER_STATE["err"],
                "version": SWVER_STATE["version"],
                "src": SWVER_STATE["src"], "at": SWVER_STATE["at"]}


def hw_snapshot():
    with HW_LOCK:
        return {"ok": HW_STATE["ok"], "err": HW_STATE["err"],
                "fields": dict(HW_STATE["fields"]), "at": HW_STATE["at"],
                "type": HW_STATE["type"], "topic": HW_TOPIC}
def fault_snapshot():
    with FAULT_LOCK:
        return {"ok": FAULT_STATE["ok"], "err": FAULT_STATE["err"],
                "items": list(FAULT_STATE["items"]),
                "level": FAULT_STATE["level"], "at": FAULT_STATE["at"],
                "type": FAULT_STATE["type"], "topic": FAULT_TOPIC}


def task_snapshot():
    with TASK_LOCK:
        st = TASK_STATE["status"]
        txt, tone = task_status_text(st)
        return {"ok": TASK_STATE["ok"], "err": TASK_STATE["err"],
                "name": TASK_STATE["name"], "status": st,
                "statusText": txt, "statusTone": tone,
                "echo": dict(TASK_STATE["echo"]), "at": TASK_STATE["at"],
                "type": TASK_STATE["type"], "topic": TASK_TOPIC}


def queue_snapshot():
    with QUEUE_LOCK:
        return {"ok": QUEUE_STATE["ok"], "err": QUEUE_STATE["err"],
                "groups": list(QUEUE_STATE["groups"]),
                "at": QUEUE_STATE["at"], "type": QUEUE_STATE["type"],
                "topic": QUEUE_TOPIC}


def cmdvel_snapshot():
    """导航状态快照。

    live 在这里现算，而不是用轮询线程里那个 1 秒前的结论 ——
    值要显示给用户看，差一秒就可能把"正在动"显示成"已停止"。
    age 给前端用：停下来之后要能说清"这是 N 秒前的值"。
    """
    with CMDVEL_LOCK:
        at = CMDVEL_STATE["at"]
        age = round(time.time() - at, 1) if at else None
        live = bool(at and (time.time() - at) <= CMDVEL_STALE)
        CMDVEL_STATE["live"] = live
        return {"ok": CMDVEL_STATE["ok"], "err": CMDVEL_STATE["err"],
                "lin_x": CMDVEL_STATE["lin_x"], "ang_z": CMDVEL_STATE["ang_z"],
                "at": at, "age": age, "live": live,
                "type": CMDVEL_STATE["type"], "topic": CMDVEL_TOPIC}


def odom_snapshot():
    """/odom_origin 实际速度快照。取法与 cmd_vel 一致。"""
    with ODOM_LOCK:
        at = ODOM_STATE["at"]
        age = round(time.time() - at, 1) if at else None
        live = bool(at and (time.time() - at) <= ODOM_STALE)
        ODOM_STATE["live"] = live
        return {"ok": ODOM_STATE["ok"], "err": ODOM_STATE["err"],
                "lin_x": ODOM_STATE["lin_x"], "ang_z": ODOM_STATE["ang_z"],
                "at": at, "age": age, "live": live,
                "type": ODOM_STATE["type"], "topic": ODOM_TOPIC}


def axslam_snapshot():
    with AXSLAM_LOCK:
        at = AXSLAM_STATE["at"]
        return {"ok": AXSLAM_STATE["ok"], "err": AXSLAM_STATE["err"],
                "fields": dict(AXSLAM_STATE["fields"]), "at": at,
                "age": round(time.time() - at, 1) if at else None,
                "type": AXSLAM_STATE["type"], "topic": AXSLAM_TOPIC}


def custom_snapshot():
    with CUSTOM_LOCK:
        out = {}
        for t in sorted(CUSTOM_STATE.keys()):
            st = CUSTOM_STATE[t]
            out[t] = {"ok": bool(st.get("ok")), "err": st.get("err", ""),
                      "type": st.get("type", ""), "at": st.get("at", 0),
                      "fields": st.get("fields"), "note": st.get("note", ""),
                      "want": t in CUSTOM_WANT}
        return out


def nav_verdict():
    """期望速度(cmd_vel) vs 实际速度(odom) —— 判断"指令发了车没动"。

    为什么要单独算这个：任务状态和 cmd_vel 在车被卡住 / 打滑 / 电机
    没响应时都是"正常"的，只有两边的差值能暴露问题。这类故障在面板
    上最容易被漏掉，所以给它一个独立的判定和持续时间。
    """
    with CMDVEL_LOCK:
        clx = CMDVEL_STATE["lin_x"]
        clive = bool(CMDVEL_STATE["at"]
                     and (time.time() - CMDVEL_STATE["at"]) <= CMDVEL_STALE)
    with ODOM_LOCK:
        olx = ODOM_STATE["lin_x"]
        olive = bool(ODOM_STATE["at"]
                     and (time.time() - ODOM_STATE["at"]) <= ODOM_STALE)
    if not clive or clx is None or olx is None or not olive:
        NAV_STALL[0] = 0.0
        return {"stall": False, "sec": 0.0, "cmd": clx, "act": olx,
                "ready": False}
    # 期望明显在动、实际几乎不动，持续 2 秒以上才算 —— 起步那一瞬
    # 两者的时间差会造成短暂不一致，不设持续时间会误报。
    if abs(clx) >= 0.05 and abs(olx) <= 0.02:
        if NAV_STALL[0] <= 0:
            NAV_STALL[0] = time.time()
        sec = round(time.time() - NAV_STALL[0], 1)
        return {"stall": sec >= 2.0, "sec": sec, "cmd": clx, "act": olx,
                "ready": True}
    NAV_STALL[0] = 0.0
    return {"stall": False, "sec": 0.0, "cmd": clx, "act": olx,
            "ready": True}


NAV_STALL = [0.0]      # 卡住判定的起始时刻，用列表装便于闭包内修改


# ═══════════════ 采集频率 ═══════════════
# 默认 1Hz。PC 端可以随时改（0.1 ~ 10Hz），命令从 stdin 进来 ——
# 走 stdin 而不是重连 SSH，改频率不用打断采集，也不会丢已订阅的话题。
HZ = 1.0
HZ_LOCK = threading.Lock()
HZ_CHOICES = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0)


def set_hz(v):
    """限制在合理区间：太低看不出变化，太高会把车端 CPU 吃进去影响被测对象。"""
    global HZ
    try:
        v = float(v)
    except Exception:
        return False
    if v < 0.05 or v > 20:
        return False
    with HZ_LOCK:
        HZ = v
    log("hz -> %s" % v)
    return True


def cur_hz():
    with HZ_LOCK:
        return HZ


def _graceful_exit(code=0):
    """优雅退出：先让 rospy 注销节点，再退出。

    为什么必须有这一步：进程被直接 kill 时 rosmaster 不知道节点没了，
    `rosnode list` 里会残留 aoting_stat_agent_xxx 好几分钟。用户在车上
    看到"节点还在"，很多时候其实是这个假象；而真残留的进程更麻烦 ——
    每次重连都新建一个节点名，跑久了车上会挂一串。
    """
    try:
        import rospy
        if not rospy.is_shutdown():
            rospy.signal_shutdown("aoting_stat_agent_exit")
            time.sleep(0.5)      # 给 XML-RPC 注销留出时间
    except Exception:
        pass
    os._exit(code)


def _install_signals():
    """TERM/INT/HUP 都走优雅退出，保证 rosmaster 上的节点名被注销。"""
    def h(signum, frame):
        log("signal %d -> graceful exit" % signum)
        _graceful_exit(0)
    for nm in ("SIGTERM", "SIGINT", "SIGHUP"):
        try:
            signal.signal(getattr(signal, nm), h)
        except Exception:
            pass


def _stdin_is_channel():
    """stdin 是不是一条真正的 SSH 通道。

    为什么要判断：只有在管道/套接字上，"读不到数据"才代表 PC 端走了。
    本地直接跑（stdin 是 /dev/null 这种字符设备）时一读就是 EOF，
    若也当成断开，脚本刚启动就会自杀 —— 拿不到任何数据。
    """
    try:
        m = os.fstat(sys.stdin.fileno()).st_mode
    except Exception:
        return False
    return stat.S_ISFIFO(m) or stat.S_ISSOCK(m)


def _stdin_iter():
    return _stdin_lines() if _stdin_is_channel() else sys.stdin


def _stdin_lines():
    """逐行读 stdin；读到 EOF（PC 端断开/关闭）就优雅退出。

    这是最后一道保险：就算 PC 端来不及发 kill 命令（崩溃、强杀、
    SSH 异常断开），采集进程也会自己退出，不会在车上干等着。
    """
    for ln in sys.stdin:
        yield ln
    # 刚启动就读到 EOF，说明本来就没有 SSH 通道（本地直跑 / stdin 是
    # /dev/null 或已关闭的管道），那不是"PC 端走了"，忽略即可 ——
    # 否则脚本一起起来就自杀，一行数据都拿不到。
    if time.time() - T0 < 5.0:
        log("stdin EOF at startup, ignore")
        return
    log("stdin EOF -> exit")
    _graceful_exit(0)


def _life_thread():
    """到点自杀，防止 PC 端清理失败时车端永久残留。

    退出用 os._exit 而不是 sys.exit：这是看门狗线程，sys.exit 只会抛
    异常结束自己这个线程，主进程照跑。

    退出前先关掉 ROS 节点，让 rosnode list 里的名字正常注销 ——
    否则进程没了名字还挂着，用户 rosnode list 一看以为还活着。
    """
    while True:
        time.sleep(1.0)
        try:
            if time.time() < LIFE_DEADLINE[0]:
                continue
        except Exception:
            continue
        log("到达存活上限 %ds，自动退出" % MAX_LIFE_SEC)
        try:
            import shutil as _sh
        except Exception:
            _sh = None
        try:
            if rospy is not None and not rospy.is_shutdown():
                rospy.signal_shutdown("stat_agent life limit")
        except Exception:
            pass
        time.sleep(0.5)
        os._exit(0)


def _stdin_thread():
    """读 PC 端发来的单行 JSON 命令。

    认四种：{"hz":n} 改频率 / {"add_topic":"..."} 订阅自定义话题 /
    {"del_topic":"..."} 取消 / {"du":1} 重新统计目录大小。

    stdin 读不出来就安静退出 —— 有些 SSH 通道不给 stdin，
    那只是"改不了频率、加不了话题"，照常采集即可。

    为什么这里必须有日志：开关只走 stdin 时，一旦这个线程起不来或
    中途死了，表现就是"前端勾了、server 返回 ok、车端永远未开启"，
    而 PC 端完全看不出是通道问题还是车端问题。所以启动、退出原因
    都写进 stderr，PC 端会原样打在黑窗口和页面日志里。
    """
    try:
        log("stdin thread: start (is_channel=%s)" % _stdin_is_channel())
        for ln in _stdin_iter():
            ln = (ln or "").strip()
            if not ln or not ln.startswith("{"):
                continue
            try:
                c = json.loads(ln)
            except Exception:
                continue
            if "hz" in c:
                set_hz(c.get("hz"))
            if c.get("life"):
                # PC 端点"延长"时下发，把自杀 deadline 往后推
                LIFE_DEADLINE[0] = time.time() + MAX_LIFE_SEC
                log("life extended to %ds" % MAX_LIFE_SEC)
            if c.get("map_refresh"):
                MAP_REFRESH[0] = True
            t = c.get("add_topic")
            if t:
                t = str(t).strip()
                with CUSTOM_LOCK:
                    CUSTOM_WANT.add(t)
                log("custom: want %s" % t)
            t = c.get("del_topic")
            if t:
                t = str(t).strip()
                with CUSTOM_LOCK:
                    CUSTOM_WANT.discard(t)
                log("custom: drop %s" % t)
            if "pc3d" in c:
                PC3D_ENABLE[0] = bool(c.get("pc3d"))
                PC3D_CNT["cmds"] += 1
                log("pc3d -> %s (via stdin)" % PC3D_ENABLE[0])
            # 两路激光共用同一套开关语义，与 pc3d 一致：默认关，
            # 开了才订阅。关掉时会真退订，不是"不画"。
            if "scan" in c:
                LASER_ENABLE[SCAN_TOPIC][0] = bool(c.get("scan"))
                log("scan -> %s (via stdin)" % LASER_ENABLE[SCAN_TOPIC][0])
            if "d2l" in c:
                LASER_ENABLE[D2L_TOPIC][0] = bool(c.get("d2l"))
                log("d2l -> %s (via stdin)" % LASER_ENABLE[D2L_TOPIC][0])
            if "rgb" in c:
                want = set()
                try:
                    for v in (c.get("rgb") or []):
                        iv = int(v)
                        if iv in RGB_IDXS:
                            want.add(iv)
                except Exception:
                    pass
                for cam in RGB_IDXS:
                    RGB_ENABLE[cam][0] = cam in want
                log("rgb -> %s (via stdin)"
                    % (",".join(str(x) for x in sorted(want)) or "off"))
            if "rgb_cfg" in c:
                try:
                    g = c.get("rgb_cfg") or {}
                    if g.get("w"):
                        RGB_CFG["w"][0] = int(g.get("w"))
                    if g.get("fps"):
                        RGB_CFG["fps"][0] = float(g.get("fps"))
                    if g.get("q"):
                        RGB_CFG["q"][0] = int(g.get("q"))
                except Exception:
                    pass
            if "cam" in c:
                try:
                    PC3D_CAM[0] = int(c.get("cam"))
                except Exception:
                    pass
                if PC3D_CAM[0] not in CAM_IDXS:
                    PC3D_CAM[0] = 1
                log("cam -> %02d (via stdin)" % PC3D_CAM[0])
            if c.get("du"):
                DU_FORCE[0] = True
                log("du: force refresh")
    except Exception as ex:
        log("stdin thread: EXIT (%s: %s)" % (type(ex).__name__, str(ex)[:120]))


HOST = hostname()
MB = 1048576


# 温度 / 频率这类慢变量的缓存：[上次读取时刻, thermal, freq]。
# 工控机温度几秒才变一次，CPU 频率也只有降频时才动，每秒去读 sysfs
# 是纯浪费 —— 降频到 5 秒一次，读数依然够看。
_SLOW_CACHE = [0.0, {}, {}]
_SLOW_GAP = 5.0


def _slow():
    now = time.time()
    if now - _SLOW_CACHE[0] >= _SLOW_GAP or not _SLOW_CACHE[1]:
        _SLOW_CACHE[0] = now
        try:
            _SLOW_CACHE[1] = cpu_thermal()
        except Exception:
            pass
        try:
            _SLOW_CACHE[2] = cpu_freq()
        except Exception:
            pass
    return _SLOW_CACHE[1], _SLOW_CACHE[2]


def main():
    _install_signals()
    # 自报 PID 和父 PID：
    #   pid  = 本采集进程，PC 端按它精确 kill
    #   ppid = exec_command 起的那个 bash -c。只杀 python 不杀它，
    #          它会一直挂在 SSH session 里 —— 用户 ps 里看到的
    #          "bash -c (... python3 -c 源码 ...)" 就是这个。
    # 为什么不用 pkill -f 匹配命令行：远端 bash 的命令行里含整份源码，
    # 同样带 aoting_stat_agent，字符串匹配分不清该杀谁。
    print(json.dumps({"type": "hello", "pid": SELF,
                      "ppid": os.getppid(), "ver": AGENT_VER}), flush=True)
    LIFE_DEADLINE[0] = time.time() + MAX_LIFE_SEC
    threading.Thread(target=_life_thread, daemon=True).start()
    threading.Thread(target=_stdin_thread, daemon=True).start()
    start_ros()             # 故障/硬件/SLAM/任务/队列：失败只影响那一块
    _swver_refresh()        # 版本文件：和 ROS 无关，单独读
    _extrinsics_refresh()      # 相机外参：从 rosparam 读，读不到用内置值
    # 目录大小统计要跑 du，慢且不能卡主循环，所以单独起线程
    threading.Thread(target=_du_thread, daemon=True).start()
    last_swver = time.time()
    prev = snapshot()
    pt, pi = total_pair()

    while True:
        # 低频时睡得久。CPU 是"两次采样的差值 ÷ 实际间隔"，
        # 所以间隔变长不影响数值正确性，变的只是采样粒度。
        time.sleep(1.0 / cur_hz())
        # 版本文件 60 秒重读一次：OTA 之后不会一直显示旧版本号。
        # 相机外参一起重读：标定可能在采集脚本起来之后才写进参数服务器，
        # 只读一次会永远停在"未标定"。
        if time.time() - last_swver > 60:
            _swver_refresh()
            _extrinsics_refresh()
            last_swver = time.time()
        cur = snapshot()
        t2, i2 = total_pair()
        dt = t2 - pt
        di = i2 - pi

        sys_cpu = 0.0
        if dt > 0:
            sys_cpu = round(100.0 * (dt - di) / dt, 1)

        rows = []
        for pid in cur:
            name, ticks, rss, node = cur[pid]
            if pid == SELF:
                continue      # 不统计采集脚本自己
            p = prev.get(pid)
            if p is None:
                cpu = 0.0     # 新进程，没有上一拍可减
            else:
                dd = ticks - p[1]
                cpu = 0.0 if dt <= 0 else max(0.0, dd * 100.0 * NCPU / dt)
            rows.append({"pid": pid, "name": name, "node": node,
                         "cpu": round(cpu, 1), "rss": int(rss / MB)})

        prev = cur
        pt, pi = t2, i2

        mi = meminfo()
        tot = mi.get("MemTotal", 0)
        av = mi.get("MemAvailable",
                    mi.get("MemFree", 0) + mi.get("Buffers", 0) + mi.get("Cached", 0))
        used = (tot - av) / 1024

        out = {
            "tag": TAG,
            "t": time.time(),
            "host": HOST,
            "cpu": sys_cpu,
            "mem_used": int(used),
            "mem_total": int(tot / 1024),
            "load": loadavg(),
            "up": int(uptime()),
            "boot_ts": int(boot_ts()),
            "nproc": len(rows),
            # 二级键必须是 pid：多数进程 cpu 都是 0.0，只按 -cpu 排的话
            # 并列项的先后顺序完全取决于 /proc 的遍历顺序，每秒都可能变，
            # 表现就是表格里的行上下乱跳。加上 pid 让并列项顺序稳定。
            "top_cpu": sorted(rows, key=lambda r: (-r["cpu"], r["pid"]))[:5],
            "top_mem": sorted(rows, key=lambda r: (-r["rss"], r["pid"]))[:5],
            "faults": fault_snapshot(),
            "hw": hw_snapshot(),
            "slam": slam_snapshot(),
            "swver": swver_snapshot(),
            "task": task_snapshot(),
            "queue": queue_snapshot(),
            "nav": cmdvel_snapshot(),
            "odom": odom_snapshot(),
            "nav_verdict": nav_verdict(),
            "axslam": axslam_snapshot(),
            "map": map_snapshot(),
            "scan": scan_snapshot(),
            "d2l": d2l_snapshot(),
            "pc3d": pc3d_snapshot(),
            "disk": {"total": disk_total("/"), "du": du_snapshot()},
            "thermal": _slow()[0],
            "freq": _slow()[1],
            "rgb": rgb_snapshot(),
            "custom": custom_snapshot(),
            "hz": round(cur_hz(), 2),
            "agent_ver": AGENT_VER,
        }
        _pc3d_tick()
        _emit(out)


if __name__ == "__main__":
    try:
        log("started pid=%d ncpu=%d" % (SELF, NCPU))
        main()
    except Exception as e:
        log("fatal: %r" % e)
        sys.exit(1)
