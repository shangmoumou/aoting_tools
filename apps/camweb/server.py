#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 远程相机实时预览的【中间层】，跑在你的 Windows 上

   浏览器(只能连本机)  <--MJPEG--  本服务  <--SSH(paramiko)--  车  <--rospy--  相机话题

为什么必须有这一层：浏览器出于安全不能发起 SSH/TCP 裸连，
所以由本机这个小服务去 SSH 上车，把图转成 MJPEG 喂给 <img>。

只监听 127.0.0.1，不对外开端口。

用法：
    python server.py            # 默认 127.0.0.1:8765
    python server.py 9000       # 换端口
"""
from __future__ import print_function

import http.server
import socketserver
import socket
import json
import os
import re
import struct
import sys
import collections
import threading
import threading
import time
import uuid
import hmac
import queue
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────────────────────
# 门禁（账号校验）
#
# 为什么相机服务自己也要校验：
#   门禁最初只做在门户页面上（界面层的锁）。但 apps/camweb/start.bat
#   可以直接双击运行，服务起在 8765 —— 浏览器直接打开 8765 就能连车取图，
#   门户那道锁根本管不着。这是 v1.0.15 修的洞。
#
#   原则：能力在哪一层，锁就得在哪一层。
#
# 免登录的交接（从门户进来不用输第二次）：
#   门户解锁成功后生成一次性令牌，通过环境变量 AOTING_GATE_TK 传给本进程，
#   再用 ?tk= 交给浏览器换取会话 cookie。直接双击 start.bat 的人
#   拿不到这个环境变量，必须登录。
# ─────────────────────────────────────────────────────────────
def _find_root():
    """往上找含 tools/gate.py 的目录（工具箱根目录）。

    找到就用共享实现；找不到说明是被单独拷出来的 camweb ——
    这时门禁不可用，一律拒绝（fail closed），不能放行。
    """
    d = HERE
    for _ in range(5):
        if os.path.isfile(os.path.join(d, 'tools', 'gate.py')):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None

ROOT = _find_root()
GATE = None
if ROOT:
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    try:
        from gate import Gate
        GATE = Gate(ROOT, cache_tag='gate')
    except Exception:
        GATE = None

# 这些接口不对门禁设限：
#   version/ready —— 门户要探测服务是否活着（探测到才能决定是否打开页面），
#                    它们不泄露任何相机数据，也不产生任何动作。
#   gate_state / gate_login —— 登录本身要走的路。
GATE_OPEN = set(['/api/version', '/api/ready',
                 '/api/gate_state', '/api/gate_login'])

BOOT_TK = (os.environ.get('AOTING_GATE_TK') or '').strip()
_SESSIONS = {}          # cookie值 -> {'user':..., 't':时刻}
_SESSION_LOCK = threading.Lock()
COOKIE = 'aoting_cam'


def gate_enabled():
    return GATE is not None and GATE.enabled()


def new_session(user):
    import secrets
    v = secrets.token_urlsafe(24)
    with _SESSION_LOCK:
        _SESSIONS[v] = {'user': user, 't': time.time()}
    return v


def cookie_of(headers):
    """从请求头里取 cookie 值。"""
    try:
        raw = headers.get('Cookie') or ''
    except Exception:
        return ''
    for part in raw.split(';'):
        k, _, v = part.partition('=')
        if k.strip() == COOKIE:
            return v.strip()
    return ''


def authorized(headers):
    """本次请求是否已通过门禁。

    注意是【服务端】判定：不靠页面有没有弹登录框 —— 页面上的锁
    可以绕过（直接开 8765），服务端这道才是真锁。
    """
    if not gate_enabled():
        return True
    ck = cookie_of(headers)
    if not ck:
        return False
    with _SESSION_LOCK:
        return ck in _SESSIONS


def current_user(headers):
    with _SESSION_LOCK:
        r = _SESSIONS.get(cookie_of(headers))
    return (r or {}).get('user', '')
AGENT = os.path.join(HERE, 'cam_agent.py')

# ROS 环境初始化：车上的非交互 shell 默认没有 ROS 变量，必须显式 source
ROS_SRC = (
    'for f in /opt/ros/*/setup.bash; do [ -f "$f" ] && . "$f" && break; done; '
    '. ~/catkin_ws/devel/setup.bash 2>/dev/null; '
    '. ~/.bashrc 2>/dev/null; '
)

# ── 可选依赖：装了就用，没装也能跑（JPEG 编码会用纯 Python 兜底）
try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

try:
    import paramiko
    HAVE_PARAMIKO = True
except Exception:
    HAVE_PARAMIKO = False


# 日志环形缓冲：页面里的"日志"面板靠它显示服务端输出，
# 这样用户不用切到黑窗口也能看到车上发生了什么。
LOG_MAX = 500
LOG_BUF = collections.deque(maxlen=LOG_MAX)
LOG_SEQ = 0              # 累计写入条数，轮询增量用（deque 满了会丢索引）
_LOG_LOCK = threading.Lock()


def log(*a):
    global LOG_SEQ
    line = '[srv] %s' % ' '.join(str(x) for x in a)
    with _LOG_LOCK:
        LOG_BUF.append(line)
        LOG_SEQ += 1
    sys.stderr.write(line + '\n')
    sys.stderr.flush()


# ═══════════════ JPEG 编码 ═══════════════
def to_jpeg(rgb_bytes, w, h, quality=72):
    """RGB 字节 -> JPEG 字节。PIL 优先；没有则用极简 BMP 兜底（浏览器也认）。"""
    if HAVE_PIL:
        import io
        img = Image.frombytes('RGB', (w, h), rgb_bytes)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=quality, optimize=False)
        return buf.getvalue()
    # 兜底：BMP（无压缩，体积大但浏览器原生支持，至少能看到画面）
    header = b'BM'
    row = w * 3
    pad = (4 - row % 4) % 4
    size = 54 + (row + pad) * h
    header += struct.pack('<IHHI', size, 0, 0, 54)
    header += struct.pack('<IiiHHIIiiII', 40, w, -h, 1, 24, 0, 0, 0, 0, 0, 0)
    body = bytearray()
    for y in range(h):
        off = y * row
        line = rgb_bytes[off:off + row]
        # RGB -> BGR
        body += bytes(bytearray(b for p in zip(line[2::3], line[1::3], line[0::3]) for b in p))
        body += b'\x00' * pad
    return header + bytes(body)


def error_jpeg(msg):
    """把错误信息画成一张图推给浏览器，这样页面上能直接看到原因。"""
    w, h = 640, 200
    if HAVE_PIL:
        import io
        img = Image.new('RGB', (w, h), (30, 30, 34))
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        # 中文可能没字体，逐行画，超长截断
        lines = str(msg).split('\n')[:6]
        y = 20
        for ln in lines:
            d.text((20, y), ln[:70], fill=(220, 220, 220))
            y += 24
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=70)
        return buf.getvalue()
    return None


SWEEP_LOCK = threading.Lock()
# (host,port) -> 上次清理时间，避免同一台车被重复清理
SWEEP_AT = {}
# 清理残留的硬超时（秒）：超时就跳过，绝不能挡住连接
SWEEP_DEADLINE = 12.0


# ══════════════════════════════════════════════════════════════
# 车端登录记录
#   每次连上车，往车的 /tmp/aoting_camweb_access.log 追加一行：
#       <车端时间> | pc=<笔记本名> | user=<本机登录用户> | ip=<笔记本IP> | car=<user@host:port>
#   只追加，绝不清理 —— 这台车被谁、什么时候看过，事后可追溯。
#
#   为什么用 >> 而不是覆盖：多人多机共用一台车时，覆盖会只留下最后一个人的
#   记录，前面的人等于没来过。追加才能形成完整的访问史。
#
#   为什么另开一条 SSH 而不是复用会话的：
#   会话的 SSH 正在灌图像数据，新 channel 可能排不上队（踩过，见 close 注释）。
#   这条只在连接瞬间发一个小命令，独立连接更可靠。
# ══════════════════════════════════════════════════════════════
ACCESS_LOG = '/tmp/aoting_camweb_access.log'
# 同一台车短时间内的多路连接只记一次，避免勾 5 路就写 5 行
ACCESS_DEDUPE_SEC = 60
_access_mark = {}


def _local_ips():
    """笔记本的所有局域网 IPv4。

    为什么取全部而不是一个：多网卡机器（有线+无线+虚拟机）上"本机IP"
    本就不唯一，只记一个可能记到 VirtualBox 那个 192.168.56.x 上，
    事后完全对不上人。全列出来才查得到。
    """
    ips = []
    try:
        import socket
        hn = socket.gethostname()
        for info in socket.getaddrinfo(hn, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))       # 不发包，只为看默认路由走哪块网卡
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            ips = ['unknown']
    return ips


def _local_who():
    import getpass
    import socket
    try:
        pc = socket.gethostname()
    except Exception:
        pc = 'unknown'
    try:
        u = getpass.getuser()
    except Exception:
        u = 'unknown'
    return pc, u


def record_access(host, port, user, pwd):
    """往车端访问日志追加一行。失败不影响取图，只记一条日志。"""
    if MOCK:
        log('[登录记录] MOCK 模式，跳过')
        return False
    if not HAVE_PARAMIKO:
        return False
    key = (host, int(port), user)
    now = time.time()
    if now - _access_mark.get(key, 0) < ACCESS_DEDUPE_SEC:
        return False
    _access_mark[key] = now

    pc, who = _local_who()
    ips = '+'.join(_local_ips())

    # 注意：下面刻意用拼接而不是 % 格式化。
    # 车端的 date 格式串里带 %Y %H 这类字符，一旦作为 % 格式串的左值
    # 就会被 Python 当成占位符，直接 ValueError: unsupported format
    # character 'Y' —— 而且这个错发生在连接路径上，表现为"连不上车"，
    # 跟日志功能完全联想不到一起。踩过，别改回 %。
    # gate_user：门户解锁时输入的账号（由 CAMWEB_USER 环境变量传下来）。
    # 之前只记笔记本 IP，事后看不出"是谁连的"——IP 还会变。
    # 没有（比如单独跑 camweb）就记 "-"，保持字段数固定便于脚本解析。
    gate_user = (os.environ.get('CAMWEB_USER') or '').strip() or '-'
    whoami = ('pc=' + pc + ' | user=' + who + ' | account=' + gate_user +
              ' | ip=' + ips +
              ' | car=' + user + '@' + str(host) + ':' + str(port) +
              ' | from=aoting_tools')
    # date 格式串刻意不含空格：含空格就得加引号，而整条命令会被
    # shell_quote 包进单引号，内层再出现引号极易在转义中变形 ——
    # 第一版就栽在这里，车端报 date: extra operand，时间字段全空。
    # car_time 由车端 date 生成：车和笔记本时钟不同源，两边都记，
    # 事后对账才知道该信谁。
    cmd = ('mkdir -p /tmp 2>/dev/null; '
           'printf "%s car_time=%s\\n" "' + whoami + '" '
           '"$(date +%Y-%m-%d_%H:%M:%S_%Z)" >> ' + ACCESS_LOG)
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, port=int(port), username=user, password=pwd,
                    timeout=12, allow_agent=False, look_for_keys=False)
        ssh.exec_command('bash -c ' + shell_quote(cmd), timeout=15)
        time.sleep(0.6)      # 给命令一点执行时间，exec_command 不等待完成
        ssh.close()
        log('[登录记录] 已写入车端 %s  (pc=%s ip=%s)' % (ACCESS_LOG, pc, ips))
        return True
    except Exception as e:
        log('[登录记录] 写入失败（不影响取图）: %s: %s' % (type(e).__name__, e))
        return False


def sweep_host(host, port, user, pwd, tag='cam_agent_'):
    """连上车，把遗留的取图脚本全部杀掉。

    为什么需要它：
        断开时的 pkill 有时会失败（清理线程和读线程抢 transport，
        详见 CamSession.close 的注释）。失败一次就留一个 python 进程在
        车上；反复连接几次，车上就堆了一堆僵尸在跑 rospy 回调，
        CPU 被吃满 —— 之后每次连接都会变慢，直到看起来"卡死在连接中"。

        用户日志里那段一路刷到"已发 1750 帧"就是这些僵尸在跑。

    调用时机：连接某台车的【第一路】之前。此时按定义这台车上没有
    我们自己的活跃会话，杀掉全部残留是安全的。

    返回被清理的进程数；连不上或没有残留则返回 0。
    """
    if MOCK or not HAVE_PARAMIKO:
        return 0
    # 必须在有限时间内返回。
    # 这个函数在 /api/connect 里是【同步】调用的：它卡多久，用户就要
    # 等多久才看到响应。实测过它卡死超过 45s，浏览器先超时放弃，
    # 服务端留下 WinError 10053 —— 用户看到的是"连接失败"，
    # 而真正的卡点（SSH 握手）藏在黑窗口里，完全对不上号。
    #
    # 所以：真活儿丢给后台线程，主线程最多等 deadline 秒。
    # 超时就跳过清理直接连接 —— 清理只是优化（防僵尸吃 CPU），
    # 不该挡着用户出图。
    box = {}
    t = threading.Thread(target=_sweep_worker,
                         args=(host, port, user, pwd, tag, box), daemon=True)
    t.start()
    t.join(SWEEP_DEADLINE)
    if t.is_alive():
        log('清理残留超时（>%ss），跳过清理继续连接' % SWEEP_DEADLINE)
        log('  ↑ 这只是清理步骤慢（SSH 握手），不影响出图；'
            '若反复出现，车端可能有残留进程占 CPU')
        return 0
    return box.get('n', 0)


def _sweep_worker(host, port, user, pwd, tag, box):
    """真正的清理动作，跑在后台线程里，可以被整体放弃。"""
    t0 = time.time()
    def ph(msg):
        log('  [清理] %s (%.1fs)' % (msg, time.time() - t0))
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, port=port, username=user, password=pwd, timeout=10,
                    allow_agent=False, look_for_keys=False)
        ph('SSH 已连接')
        try:
            # 先数一下有多少残留，用来给用户提示
            _, so, _ = ssh.exec_command(
                'pgrep -fc %s 2>/dev/null || echo 0' % shell_quote(tag), timeout=10)
            n = 0
            try:
                n = int(so.read().decode('utf-8', 'replace').strip().split('\n')[0] or 0)
            except Exception:
                n = 0
            ph('已查询残留 (%d 个)' % n)
            if n > 0:
                # 关键护栏：清理线程可能跑得很慢，这期间新的会话已经建起来了。
                # 不加这道检查，pkill 会把刚启动的取图进程一起杀掉 ——
                # 症状就是"连上没几秒就 车上脚本退出"（v1.3 踩过）。
                if host_has_session(host, port):
                    log('  [清理] 已有活跃会话，跳过 pkill 以免误杀')
                    ph('放弃清理')
                    return
                ssh.exec_command('pkill -9 -f %s 2>/dev/null'
                                 % shell_quote(tag), timeout=10)
                log('清理了 %s 上 %d 个残留的取图进程' % (host, n))
            # 顺带清掉 /tmp 下堆积的历史脚本文件。
            # 这里能全量删是因为本函数只在"这台车上没有我们自己的活跃会话"
            # 时调用（见函数头说明），不存在误删正在用的文件。
            _, so2, _ = ssh.exec_command(
                'find /tmp -maxdepth 1 -type f -name "cam_agent_*.py" '
                '-delete 2>/dev/null; echo done', timeout=10)
            try:
                so2.read()
            except Exception:
                pass
            ph('已清理历史脚本')
            box['n'] = n
        finally:
            try:
                ssh.close()
            except Exception:
                pass
    except Exception as e:
        log('清理残留失败（忽略）: %s' % e)
        box['n'] = 0


def nowait_multi(host, port):
    """这台车是否已有会话（含正在建立的）。

    5 路是并发建连的，第一批请求几乎同时到达、都还没进 SESS，
    所以要用一个短时窗的"预约"来去重，避免 5 路各清理一遍。
    """
    if host_has_session(host, port):
        return True
    now = time.time()
    with SWEEP_LOCK:
        last = SWEEP_AT.get((host, int(port)), 0)
        if now - last < 10:      # 10 秒内同车只清理一次
            return True
        SWEEP_AT[(host, int(port))] = now
        return False


def host_has_session(host, port):
    """这台车是否还有活跃会话。用来判断该不该做全局清理。"""
    for s in list(SESS.values()):
        try:
            if s.host == host and int(s.port) == int(port) and s.running:
                return True
        except Exception:
            pass
    return False


# ═══════════════ 车端资源监视（CPU / 内存）═══════════════
#
# 每台车共用一条 SSH 连接做采集，1 秒一次。
#
# 为什么必须是"每台车一条"而不是"每路相机一条"：
#   5 路相机就 5 条 SSH，采集本身还要跑脚本，等于给车额外加负载 ——
#   而我们要观测的恰恰就是车的负载，观测行为把观测对象搞偏了。
#   而且 5 路相机看到的 CPU 是同一台车的同一个数字，重复采毫无意义。
#   所以按 (host,port,user) 做 key 全局共享，引用计数归零才断开。
#
# 为什么单独开连接、不复用取图那条：
#   取图通道被二进制帧灌满，在它上面再开 channel 去 exec 命令会排队，
#   轻则数据延迟，重则命令发不出去（v1.5 的 pkill 就是这么丢的）。

SYSMON_PY = (
    "import time\n"
    "def s():\n"
    "    v=list(map(int,open(\"/proc/stat\").readline().split()[1:]))\n"
    "    idle=v[3]+(v[4] if len(v)>4 else 0)\n"
    "    return sum(v),idle\n"
    "t0,i0=s()\n"
    "while True:\n"
    "    time.sleep(1)\n"
    "    t1,i1=s()\n"
    "    d=t1-t0; di=i1-i0\n"
    "    cpu=0 if d<=0 else int(100*(d-di)/d)\n"
    "    m={}\n"
    "    for ln in open(\"/proc/meminfo\"):\n"
    "        m[ln.split(\":\")[0]]=int(ln.split()[1])\n"
    "    tot=m.get(\"MemTotal\",0)\n"
    "    av=m.get(\"MemAvailable\","
    " m.get(\"MemFree\",0)+m.get(\"Buffers\",0)+m.get(\"Cached\",0))\n"
    "    print(\"cpu=%d mem=%d/%d\"%(cpu,(tot-av)//1024,tot//1024),flush=True)\n"
    "    t0,i0=t1,i1\n"
)

# 为什么整段用双引号、由 shell_quote 用单引号包住：
#   如果里面也写单引号，shell_quote 会转义成 '"'"' 那种嵌套形式，
#   经过 paramiko exec_command 再到远端 bash，多一层引用就容易变形
#   （实测出现过 "unterminated quoted string"）。里面只用双引号，
#   外层单引号包裹，任何环节都不会再转义一次。


class SysMon(object):
    """一台车一条采集连接，多路相机共享结果。"""

    REG = {}          # key -> SysMon
    LOCK = threading.Lock()

    def __init__(self, key, host, port, user, pwd):
        self.key = key
        self.host, self.port, self.user, self.pwd = host, port, user, pwd
        self.refs = 0
        self.cpu = None
        self.mem_used = None
        self.mem_total = None
        self.at = 0            # 最近一次拿到数据的时间
        self.err = None
        self.ssh = None
        self.stop = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    @classmethod
    def acquire(cls, key, host, port, user, pwd):
        with cls.LOCK:
            m = cls.REG.get(key)
            if m is None:
                m = cls.REG[key] = SysMon(key, host, port, user, pwd)
            m.refs += 1
            return m

    @classmethod
    def release(cls, key):
        with cls.LOCK:
            m = cls.REG.get(key)
            if not m:
                return
            m.refs -= 1
            if m.refs <= 0:
                m.stop = True
                cls.REG.pop(key, None)

    def alive(self):
        """数据是否还算新鲜（超过 5 秒没更新就不显示了，免得显示假数据）"""
        return self.at and (time.time() - self.at) < 5.0

    def _loop(self):
        try:
            import paramiko
        except Exception as e:
            self.err = 'paramiko 未安装'
            return
        while not self.stop:
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(self.host, port=self.port, username=self.user,
                            password=self.pwd, timeout=15,
                            allow_agent=False, look_for_keys=False)
                self.ssh = ssh
                # 采集脚本本身尽量轻：不编解码、不读大文件，1 秒一行文本
                _i, out, err = ssh.exec_command('python3 -c %s' % shell_quote(SYSMON_PY),
                                                timeout=None)
                buf = b''
                while not self.stop:
                    import select
                    r, _, _ = select.select([out.channel], [], [], 0.5)
                    if r:
                        ch = out.channel.recv(4096)
                        if not ch:
                            break
                        buf += ch
                        while b'\n' in buf:
                            ln, buf = buf.split(b'\n', 1)
                            self._parse(ln.decode('utf-8', 'replace').strip())
                    if out.channel.exit_status_ready():
                        break
            except Exception as e:
                self.err = str(e)[:80]
                time.sleep(3)      # 采不到就退避重试，不影响取图
            finally:
                try:
                    if self.ssh:
                        self.ssh.close()
                except Exception:
                    pass
                self.ssh = None

    def _parse(self, ln):
        # 形如: cpu=23 mem=1821/7822
        try:
            if 'cpu=' not in ln:
                return
            a, b = ln.split(' mem=')
            cpu = int(a.split('cpu=')[1])
            used, tot = (int(x) for x in b.split('/'))
            self.cpu, self.mem_used, self.mem_total = cpu, used, tot
            self.at = time.time()
        except Exception:
            pass


# ═══════════════ 会话：一条 SSH 连一路相机 ═══════════════
class CamSession(object):
    def __init__(self, host, port, user, pwd, topic, mtype, width, fps):
        self.host, self.port, self.user = host, int(port), user
        self.pwd = pwd
        self.topic, self.mtype = topic, mtype
        self.width, self.fps = int(width), float(fps)
        self.q = queue.Queue(maxsize=2)      # 只留最新 2 帧，宁可丢帧不要延迟
        self.err = None
        self.running = True
        self.nframes = 0
        self.started = time.time()
        self.quality = int(os.environ.get('CAM_QUALITY', 55))
        self.enc = None            # 车端编码方式：cv2 / PIL / raw / passthrough
        self.clock_offset = 0.0    # 车钟 - 本地钟，用来算真实延迟
        self.clock_unknown = False # 偏移没测出来时不显示延迟，免得误导
        # 每路相机一个唯一 id，车端脚本文件名和 pkill 都带上它。
        # 不能只按 "cam_agent_" 前缀 pkill：断开上一台车的清理动作若执行
        # 晚了，会把刚启动的下一路一起杀掉（v1.3 实测会这样，症状是
        # 连上没几秒就"车上脚本退出"）。
        self.agent_id = uuid.uuid4().hex[:8]
        self.agent_tag = 'cam_agent_' + self.agent_id
        # 帧/心跳到达时间：用来区分"话题没数据"和"脚本挂了"
        self.last_frame_at = None
        self.last_hb_at = None
        self.no_msg_hint = None   # 车端上报的"话题多久没消息了"
        self.lag = 0.0             # 最近一帧的延迟（秒）
        self.bytes = 0             # 累计收字节，用来算码率
        self.max_lag = 1.0         # 超过这个年龄的帧直接丢，不慢慢回放积压
        # 取图时限：到点自动断开。start() 时刻为准，不是首帧时刻 ——
        # 否则一路没数据的相机会永远挂着（first frame 永远不来）。
        self.deadline = None       # 连接成功时设定
        self.expired = False       # 是否因超时被断开
        self._last_rate = (time.time(), 0)
        self.ssh = None
        self.chan = None
        self.proc = None                     # mock 模式用
        self.sysmon = None                  # 车端资源监视（同一台车多路共享）
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self, max_sec=None):
        self.deadline = time.time() + (MAX_SESSION_SEC if max_sec is None else float(max_sec))
        if not MOCK:
            # 车端资源监视。失败无所谓 —— 只是没有 CPU/内存数字，不影响取图。
            try:
                key = '%s@%s:%s' % (self.user, self.host, self.port)
                self.sysmon = SysMon.acquire(key, self.host, self.port,
                                             self.user, self.pwd)
            except Exception as e:
                log('资源监视启动失败（不影响取图）:', e)
        self.thread.start()

    def remain(self):
        """剩余秒数。无时限时返回 None。"""
        if not self.deadline:
            return None
        return max(0.0, self.deadline - time.time())

    # ── 帧解析 ──
    def _feed(self, buf):
        """从字节流里切出完整帧。返回 (剩余字节, 帧数)"""
        n = 0
        while True:
            if len(buf) < 4:
                return buf, n
            (plen,) = struct.unpack('>I', buf[:4])
            if plen <= 0 or plen > 64 * 1024 * 1024:
                raise ValueError('帧长度异常: %d' % plen)
            if len(buf) < 4 + plen:
                return buf, n
            payload = buf[4:4 + plen]
            buf = buf[4 + plen:]
            if len(payload) < 16:
                continue
            w, h = struct.unpack('>II', payload[:8])
            (ts,) = struct.unpack('>d', payload[8:16])
            data = payload[16:]
            self.bytes += 4 + plen
            if w == 0:
                # 已编码的 jpeg/png：直接透传给浏览器
                self._put(('rawimg', 'JPGD' if h == 0x4A504744 else 'PNGD', data, ts))
            else:
                self._put(('rgb', (w, h), data, ts))
            n += 1

    def _put(self, frame):
        if self.q.full():
            try:
                self.q.get_nowait()
            except Exception:
                pass
        try:
            self.q.put_nowait(frame)
        except Exception:
            pass
        self.nframes += 1
        now = time.time()
        self.last_frame_at = now
        # 收到帧本身就说明脚本活着；心跳只是在"没帧"时也能证明它还活着
        self.last_hb_at = now
        # 车钟可能和本地差很多（没开 NTP），用握手时测出的偏移校正
        self.lag = max(0.0, time.time() - (frame[3] - self.clock_offset))

    def _sync_clock(self, ssh):
        """测车钟偏移。

        两个要点：
        1) 用 (t0+t1)/2 作本地参考时刻，抵消 RTT 的单向偏差。
           直接拿 t1（读到结果后）会系统性偏大半个 RTT。
        2) 必须周期性重测。车钟和本机钟是两套晶振，总有频率差；
           只在握手时测一次的话，误差会随时间线性累积 ——
           表现出来的症状就是"延迟不断增大"（实测可达数秒）。
        """
        t0 = time.time()
        _, so, _se = ssh.exec_command('date +%s.%N', timeout=10)
        txt = so.read().decode('utf-8', 'replace').strip().split('\n')[0]
        t1 = time.time()
        car_t = float(txt)
        off = car_t - (t0 + t1) / 2.0
        first = self.clock_offset is None
        self.clock_offset = off
        self.clock_unknown = False
        # 只在首次或变化明显时打日志，免得刷屏
        if first or abs(off) > 1.0:
            log('车钟偏移: %+.3f s%s' % (off, '（已周期重测）' if not first else ''))

    def _clock_loop(self, ssh):
        """每 20 秒重测一次车钟偏移，抵消晶振频差累积的误差。"""
        while self.running:
            for _ in range(20):
                if not self.running:
                    return
                time.sleep(1)
            try:
                self._sync_clock(ssh)
            except Exception:
                pass

    def _run_ssh(self):
        import paramiko
        # 分阶段打点：连不上车时，用户只能看到"超时"，
        # 却不知道卡在握手、传脚本还是订阅。打点后一眼就能定位。
        self._t0 = time.time()
        def ph(msg):
            log('  [%s] %s (%.1fs)' % (self.topic.split('/')[-2] if '/' in self.topic
                                       else self.topic, msg,
                                       time.time() - self._t0))
        self._ph = ph
        ph('开始 SSH 握手')
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh = ssh
        ssh.connect(self.host, port=self.port, username=self.user,
                    password=self.pwd, timeout=15,
                    allow_agent=False, look_for_keys=False)

        # 0) 测车上时钟与本地的偏移。
        #    延迟 = 本地现在 - 帧时间戳，但两个时钟不同源的话这个减法没意义。
        #    开 NTP 的车偏移接近 0；没开的话可能是几秒甚至几天，不校正就没法看。
        # 建连后第一次 exec 偶发失败（channel 还没稳），重试两次再放弃。
        self.clock_offset = None
        for _try in range(3):
            try:
                self._sync_clock(ssh)
                break
            except Exception as e:
                if _try == 2:
                    log('测车钟失败（延迟显示将不可靠）:', e)
                else:
                    time.sleep(0.5)
        if self.clock_offset is None:
            self.clock_offset = 0.0
            self.clock_unknown = True
            # 偏移未知时，延迟数字没有参考意义，索性不显示，避免误导
            log('警告: 车钟偏差未知，延迟值不可信（页面会隐藏）')

        # 1) 找可用的 python（车上有可能只有 python2）
        threading.Thread(target=self._clock_loop, args=(ssh,), daemon=True).start()

        py = 'python'
        try:
            _, so, se = ssh.exec_command(
                ROS_SRC + 'for p in python3 python python2; do command -v $p >/dev/null 2>&1 '
                '&& $p -c "import rospy" 2>/dev/null && echo "PY:$p" && break; done', timeout=30)
            out = so.read().decode('utf-8', 'replace')
            m = re.search(r'PY:(\S+)', out)
            if m:
                py = m.group(1)
        except Exception as e:
            log('探测 python 失败（用默认）:', e)

        # 2) 传脚本：用 heredoc 直接写，不走 SFTP
        #    有些车禁了 sftp 子系统，而 exec 一定可用；少一个失败点。
        remote = '/tmp/%s.py' % self.agent_tag
        script = open(AGENT, 'rb').read().decode('utf-8')
        if 'AGENTEOF' in script:
            raise RuntimeError('脚本内容里出现了 AGENTEOF，换个标记')

        # 写新脚本前，先把历史的 cam_agent_*.py 删掉。
        #
        # 每次连接都往 /tmp 写一个带随机后缀的新文件，反复连接会在车上
        # 堆上百个 .py（用户 ls /tmp 看到满屏 cam_agent_*.py 就是这个）。
        #
        # 关键细节：必须【排除本次要写的这个文件】。
        #   5 路是并发建连的，A 路清理时 B 路可能刚 cat 完还没启动 python；
        #   不加排除就会把 B 的文件删掉，B 启动时报 "can't open file"。
        ph('SSH 已连接，准备上车脚本')
        # 另外 Linux 下删除正在运行的脚本是安全的（python 早已读入内存，
        # unlink 只减引用计数），所以这里不用判断有没有进程在用。
        # 注意排除项必须是【完整文件名】 cam_agent_<tag>.py，不能只写 <tag>.py：
        #   find 的 -name 匹配的是 basename，写 "! -name aaaa1111.py" 时
        #   实际文件 cam_agent_aaaa1111.py 并不等于它，结果连自己一起删掉
        #   —— 自己刚 cat 完就被删，python 启动报 can't open file。
        #   这个坑是在本机 /tmp 上真跑 find 验证时抓到的。
        cleanup = ('find /tmp -maxdepth 1 -type f -name "cam_agent_*.py" '
                   '! -name "cam_agent_%s.py" -delete 2>/dev/null; '
                   % self.agent_tag)

        # 注意：8 个参数要对应 8 个 %s（cat 的 remote + script + py + remote
        # + mtype + topic + width + fps）。写错会抛 TypeError，且症状表现为
        # "一直连不上"，很难定位。
        cmd = cleanup + ('cat > %s <<"AGENTEOF"\n%s\nAGENTEOF\n'
                         '%s %s %s %s %s %s %d') % (
            remote, script, py, remote, shell_quote(self.mtype),
            shell_quote(self.topic), self.width, self.fps, self.quality)
        full = 'bash -c %s' % shell_quote(ROS_SRC + cmd)
        log('执行:', full)
        chan = ssh.get_transport().open_session()
        chan.settimeout(3)
        chan.exec_command(full)
        ph('车端脚本已启动，等待出图')
        self.chan = chan

        buf = b''
        while self.running:
            if chan.recv_ready():
                buf += chan.recv(65536)
                buf, _n = self._feed(buf)
            if chan.recv_stderr_ready():
                err = chan.recv_stderr(4096).decode('utf-8', 'replace')
                for ln in err.strip().split('\n'):
                    if not ln.strip():
                        continue
                    log('[车]', ln.strip())
                    # 心跳：即使话题没数据，也能证明脚本还活着
                    if 'HB n=' in ln:
                        self.last_hb_at = time.time()
                        if 'no_msg=' in ln:
                            self.no_msg_hint = ln.split('no_msg=')[1].split()[0]
                        else:
                            self.no_msg_hint = None
                    if 'MODE enc=' in ln:
                        self.enc = ln.split('MODE enc=')[1].split()[0]
                    if '不支持的 encoding' in ln:
                        self.err = ln.strip()
            if chan.exit_status_ready():
                code = chan.recv_exit_status()
                self.err = '车上脚本退出，code=%s' % code
                log(self.err)
                break
            time.sleep(0.005)

    def _run_mock(self):
        """本地 mock：跑一个假 agent 进程，用来在没有车的时候验证整条链路"""
        script = os.path.join(HERE, 'test', 'mock_agent.py')
        env = dict(os.environ)
        env['AGENT_ARGS'] = '%s %s %d %s' % (self.mtype, self.topic, self.width, self.fps)
        self.proc = subprocess.Popen([sys.executable, script, self.topic, str(self.width)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        buf = b''
        while self.running:
            import select
            r, _, _ = select.select([self.proc.stdout], [], [], 0.2)
            if r:
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                buf, _n = self._feed(buf)

    def _run(self):
        try:
            if MOCK:
                self._run_mock()
            else:
                self._run_ssh()
        except Exception as e:
            self.err = '%s: %s' % (type(e).__name__, e)
            log('会话异常:', self.err)
        finally:
            self.running = False

    def get_frame(self, timeout=3.0):
        """取一帧。会丢掉年龄超过 max_lag 的帧 —— 否则积压会一帧帧慢慢回放，
        用户看到的就是"画面停在 N 秒前"。宁可跳帧也要贴近实时。"""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            try:
                fr = self.q.get(timeout=min(left, 0.5))
            except Exception:
                continue
            age = time.time() - (fr[3] - self.clock_offset)
            if age <= self.max_lag:
                self.lag = max(0.0, age)
                return fr
            # 太旧，继续取下一帧；取不到就返回这帧（有画面总比黑屏好）
            if self.q.empty() or time.time() >= deadline:
                self.lag = max(0.0, age)
                return fr

    def stall(self):
        """距上一帧过了多少秒。None = 一帧都还没收到。"""
        if self.last_frame_at is None:
            return None
        return time.time() - self.last_frame_at

    def hb_age(self):
        """距上次心跳过了多少秒。None = 从没收到过心跳。"""
        if self.last_hb_at is None:
            return None
        return time.time() - self.last_hb_at

    def rate(self):
        """最近码率（字节/秒）"""
        now = time.time()
        t0, b0 = self._last_rate
        dt = now - t0
        if dt < 0.5:
            return None
        r = (self.bytes - b0) / dt
        self._last_rate = (now, self.bytes)
        return r

    def close(self):
        """断开。

        关键：清理动作一律丢到后台线程做，本函数立刻返回。

        为什么必须这样：会话超时时 SSH 通道处于半死状态，paramiko 的
        chan.close()/ssh.close() 会阻塞在 socket 上（可能几十秒）。
        原来这里同步 close，HTTP 的 /api/disconnect 就一直不返回，
        前端 await 永远等下去 —— 表现就是"点了断开没反应"。
        """
        self.running = False

        # 先唤醒可能正卡在读的 worker 线程。
        # 关闭 channel 会让阻塞中的 recv 立刻抛错返回，这是最快的一刀。
        try:
            if self.chan:
                self.chan.close()
        except Exception:
            pass

        # 关键顺序：必须先等读线程真正停下来，再发 pkill。
        #
        # 两者共用同一个 SSH transport：读线程在 while 里疯狂 recv，
        # _cleanup 线程的 exec_command 要开新 channel。有数据的相机在持续
        # 灌数据时，transport 窗口被占满，新 channel 的 open 请求排不上
        # 队 —— pkill 命令压根没送到车上。这正是用户看到的现象：
        # 点了断开，画面关了，但日志里车端还在"已发 1750 帧"。
        try:
            if self.thread is not threading.current_thread():
                self.thread.join(timeout=2.0)
        except Exception:
            pass

        def _cleanup():
            # kill 车端进程：父 ssh session 没了，agent 有时不会自己退出。
            #
            # 关键：另开一条【独立的 SSH 连接】来发 pkill，绝不能复用
            # 正在灌数据这条。
            #
            #   读线程在 while 里疯狂 recv，把 transport 的窗口占满；
            #   同一 transport 上开新 channel 发 pkill，open 请求排不上
            #   队 —— 命令压根到不了车上。这就是"点了断开，画面关了，
            #   日志里车端却还在刷「已发 N 帧」"的根因。
            #   新开一条连接走自己的 transport，不受影响。
            self._pkill_via_new_conn()
            try:
                if self.proc:
                    self.proc.kill()
            except Exception:
                pass
            try:
                if self.ssh:
                    self.ssh.close()
            except Exception:
                pass
            log('会话已清理: %s (%s)' % (self.topic, self.agent_tag))

        # 释放车端资源监视的引用。归零时采集连接才真正断开 ——
        # 5 路相机共享同一条，前 4 路断开时采集必须继续。
        if self.sysmon:
            try:
                SysMon.release(self.sysmon.key)
            except Exception:
                pass
            self.sysmon = None

        t = threading.Thread(target=_cleanup, daemon=True)
        t.start()
        # 读线程已经退出，transport 空闲，pkill 通常几百毫秒就能完成。
        # 这里给 3 秒；超了也让 HTTP 先返回（前端不等这个请求），
        # 剩下的由 _cleanup 线程做完，加上车端 ppid 兜底，不会漏。
        t.join(timeout=3.0)


    def _pkill_via_new_conn(self):
        """另开一条 SSH 连接去 kill 车端脚本。

        见 close() 里的说明：复用正在传数据的 transport 发 pkill
        会排不上队，命令根本到不了车上。
        """
        if MOCK or not HAVE_PARAMIKO or not self.host:
            return 0
        try:
            import paramiko
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(self.host, port=self.port, username=self.user,
                        password=self.pwd, timeout=8,
                        allow_agent=False, look_for_keys=False)
            try:
                # 先数一下有几个，给日志一个明确的交代
                _, so, _ = ssh.exec_command(
                    'pgrep -fc %s 2>/dev/null || echo 0'
                    % shell_quote(self.agent_tag), timeout=8)
                n = 0
                try:
                    n = int(so.read().decode('utf-8', 'replace')
                            .strip().split('\n')[0] or 0)
                except Exception:
                    n = 0
                if n > 0:
                    ssh.exec_command('pkill -9 -f %s 2>/dev/null'
                                     % shell_quote(self.agent_tag), timeout=8)
                    log('  已 kill 车端 %d 个进程 (%s)' % (n, self.agent_tag))
                else:
                    log('  车上已无该脚本进程 (%s)' % self.agent_tag)
                return n
            finally:
                try:
                    ssh.close()
                except Exception:
                    pass
        except Exception as e:
            log('  独立连接清理失败（车端有 ppid 兜底）: %s' % e)
            return 0


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


# ═══════════════ 断开冷却 ═══════════════
# 断开是个异步过程：kill 车端进程、关 SSH 都可能需要几秒。
# 这段时间内立刻再点连接，新脚本会和还没死透的旧进程打架，
# 症状就是"连接中"卡住。所以记下断开时刻，前端据此冷却。
# 单次取图最长时长。到点自动断开，需要再看就再点一次连接。
# 为什么要限制：忘关的流会一直占着 SSH 连接和车上 CPU，
# 一台车被反复挂着几个无人看的流，后面的人就再也连不上了。
MAX_SESSION_SEC = float(os.environ.get('CAM_MAX_SESSION_SEC', 600))
# 剩余多少秒时开始提醒，让用户知道快到点了
SESSION_WARN_SEC = 60.0

LAST_STOP_AT = 0.0
CLEANUP_GRACE = 5.0        # 断开后建议等待的秒数


def session_watchdog():
    """到点自动断开取图会话。

    为什么要单独一个线程而不是塞进 _run：_run 阻塞在 recv 上，
    无数据的相机永远醒不过来，挂在里面的检查永远不会执行。
    独立线程按 deadline 判断，跟有没有帧完全无关。
    """
    while True:
        time.sleep(2)
        try:
            now = time.time()
            for sid in list(SESS.keys()):
                s = SESS.get(sid)
                if not s or not s.running or s.deadline is None:
                    continue
                rem = s.deadline - now
                if rem <= 0:
                    log('[时限] %s 单次取图已达上限(%.0f 分钟)，自动断开'
                        % (s.topic, MAX_SESSION_SEC / 60.0))
                    s.expired = True
                    try:
                        s.close()
                    except Exception:
                        pass
                    SESS.pop(sid, None)
        except Exception:
            pass


def cleanup_remaining():
    """距上次断开还需等待多少秒。0 表示可以立即连。"""
    if LAST_STOP_AT <= 0:
        return 0.0
    left = CLEANUP_GRACE - (time.time() - LAST_STOP_AT)
    return left if left > 0 else 0.0


# ═══════════════ HTTP ═══════════════
MOCK = os.environ.get('CAM_MOCK') == '1'
SESS = {}
VERSION = '2.10'
SERVER_STARTED = time.strftime('%Y-%m-%d %H:%M:%S')
BOUNDARY = 'frameboundary'


_UNSET = object()   # 区分"没缓存过"和"缓存了 None/{}"

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    # 每个请求一个 Handler 实例，body 缓存在实例上
    _body_cache = _UNSET

    def log_message(self, fmt, *a):
        pass   # 太吵，关掉

    def _cors(self):
        # 门户（8899）要跨域探测本服务（8765）。
        # 少了这几个头，浏览器会把响应挡掉 —— 门户永远显示
        # "本地服务未启动"，而服务端其实一切正常。
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _set_cookie(self, v):
        self.send_header('Set-Cookie', '%s=%s; Path=/; SameSite=Lax'
                         % (COOKIE, v))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def gate_login(self, body):
        """POST /api/gate_login  {"user":"zhangsan","pwd":"..."}

        注意 _body() 返回的已经是【解析好的 dict】，不是字符串。
        再 json.loads 一次必然报错（而且报的是 bad json，
        跟真实原因八竿子打不着）。
        """
        j = body if isinstance(body, dict) else {}
        if GATE is None:
            # 单独拷出来的 camweb 没有 tools/gate.py —— 一律拒绝，
            # 不能"没门禁就放行"，那等于把锁拆了。
            return self._json(
                {'ok': False,
                 'err': '找不到账号校验模块（tools/gate.py）。'
                        '请使用完整工具箱，不要单独运行 camweb。'}, 403)
        r = GATE.check(j.get('user') or '', j.get('pwd') or '')
        if not r.get('ok'):
            return self._json(r, 200)
        v = new_session(r.get('user') or '')
        b = json.dumps({'ok': True, 'user': r.get('user'),
                        'src': r.get('src')}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self._set_cookie(v)
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def _html(self, path):
        b = open(path, 'rb').read()
        # 把已登录账号直接烧进页面：水印第一帧就能显示账号，
        # 不用等 checkGate 的异步返回。
        #
        # 为什么必须服务端注入：水印的用户名原先只由前端异步去取，
        # 而 WM_USER 从来没被赋过值 —— 结果所有人水印都显示"未登录"，
        # 水印等于白做。这里在 HTML 里直接给出，是唯一的可靠来源。
        u = current_user(self.headers)
        if u:
            inj = (b'<script>window.__WM_USER__=' +
                   json.dumps(u).encode('utf-8') + b';</script>')
            b = b.replace(b'</head>', inj + b'\n</head>', 1)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        # 必须禁缓存：否则浏览器拿旧 index.html，改了代码看不到效果，
        # 且页面显示的版本号也是旧的，极难判断到底跑的是哪一份。
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split('?')[0]
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        # 版本接口。用途：页面拿它跟自己内嵌的版本号比对。
        #
        # 为什么要这个：用户很可能同时开着好几个黑窗口（旧服务没关）。
        # Windows 下 SO_REUSEADDR 允许重复绑定 8765，新进程"启动成功"
        # 不报错，但浏览器连上的可能仍是旧进程 —— 于是代码明明改了，
        # 表现却完全没变。这是最难自查的一类问题，必须让它自己暴露出来。
        if p == '/api/version':
            # 依赖状态一并报出去：门户靠 GET 探测，只拿到版本号的话，
            # "paramiko 没装"这种致命问题会被当成"服务正常"。
            return self._json({'ok': True, 'version': VERSION,
                               'started': SERVER_STARTED, 'pid': os.getpid(),
                               'paramiko': HAVE_PARAMIKO, 'pil': HAVE_PIL,
                               'python': '%d.%d.%d' % sys.version_info[:3],
                               'exe': sys.executable})
        if p in ('/', '/index.html'):
            # 门户交接：带 ?tk= 且与此进程启动时拿到的令牌一致，
            # 就直接发会话 cookie 并跳回干净地址（令牌不留在地址栏）。
            tk = (qs.get('tk') or [''])[0]
            if tk and BOOT_TK and hmac.compare_digest(str(tk), BOOT_TK):
                v = new_session(os.environ.get('CAMWEB_USER') or 'portal')
                self.send_response(302)
                self.send_header('Location', '/')
                self._set_cookie(v)
                self.send_header('Content-Length', '0')
                self._cors()
                self.end_headers()
                return
            return self._html(os.path.join(HERE, 'index.html'))
        if p == '/api/gate_state':
            return self._json({
                'ok': True,
                'need': bool(gate_enabled()) and not authorized(self.headers),
                'user': current_user(self.headers),
            })
        # 门禁：所有 /api/* 与视频流都要先登录。
        # 这是服务端判定 —— 页面上的登录框只是提示，绕过它直接开
        # 8765 的人在这里被拦住。
        if gate_enabled() and (p.startswith('/api/') or p == '/stream'):
            if p not in GATE_OPEN and not authorized(self.headers):
                return self._json({'ok': False, 'needLogin': True,
                                   'err': '请先登录（账号密码）'}, 401)
        if p == '/api/caps':
            return self._json({
                'pil': HAVE_PIL, 'paramiko': HAVE_PARAMIKO, 'mock': MOCK,
            })
        if p == '/stream':
            return self.stream()
        if p == '/api/logs':
            return self.log_poll(qs)
        self.send_error(404)

    def log_poll(self, qs):
        """日志增量轮询（替代原来的 SSE 长连接）。

        为什么要换掉 SSE：
            浏览器对同一个 host 的并发连接数有上限（HTTP/1.1 通常是 6）。
            5 路相机 = 5 个 MJPEG 长连接，再加 1 个 SSE 日志长连接，
            正好把 6 个名额占满 —— 于是第 6 路流、以及 /api/status 轮询
            全都排不上队，表现就是"日志里明明有数据，画面却只出来一路"。

            改成短连接轮询后，日志请求用完即释放，长连接名额全部留给
            真正需要持续传输的视频流。
        """
        try:
            since = int((qs.get('since') or ['0'])[0])
        except Exception:
            since = 0
        with _LOG_LOCK:
            cur = list(LOG_BUF)
            seq = LOG_SEQ
        if since <= 0:
            lines = cur[-80:]
        else:
            # 客户端已看过的条数 = 上次 seq 到现在增长的条数
            skip = max(0, len(cur) - (seq - since))
            lines = cur[skip:]
            if len(lines) > 100:
                lines = lines[-100:]
        return self._json({'ok': True, 'lines': lines, 'seq': seq})

    def _body(self):
        # Python 的 http.server 不解析 chunked 请求体。浏览器（fetch）永远带
        # Content-Length，所以正常用不到；但一些 HTTP 客户端库在不显式设置
        # Content-Length 时会退化成 chunked，此时这里读到的是 "长度\r\n{json}\r\n0\r\n"
        # 这种畸形数据 —— 表现为空 body，进而报出"主机和话题不能为空"这种
        # 完全跑偏的错误，极难排查。干脆在这里就明确拒绝。
        te = (self.headers.get('Transfer-Encoding') or '').lower()
        if 'chunked' in te:
            raise ValueError('不支持 chunked 请求体：请让客户端发送 Content-Length')
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        # 缓存结果：body 是流，读一次就没了。
        # 之前因为重复读导致请求挂死 45s（见 do_POST 里的注释），
        # 这里加缓存，即使将来有人二次调用也不会再踩。
        if self._body_cache is not _UNSET:
            return self._body_cache
        try:
            v = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            v = {}
        self._body_cache = v
        return v

    def _quit_soon(self, delay=1.0):
        """延迟一点再退出，先让当前这个 HTTP 响应返回给浏览器。

        直接 os._exit 会把响应截断，页面上的"已退出"提示就收不到了。
        """
        time.sleep(delay)
        # 先把车上的取图脚本都收掉，避免留下孤儿进程
        try:
            for sid, s in list(SESS.items()):
                try:
                    s.close()
                except Exception:
                    pass
        except Exception:
            pass
        log('服务退出')
        try:
            self.server.shutdown()
        except Exception:
            pass
        os._exit(0)

    def do_POST(self):
        p = self.path.split('?')[0]
        try:
            b = self._body()
        except ValueError as e:
            return self._json({'ok': False, 'err': str(e)}, 400)

        # 门户也可能用 POST 探测（有些环境的 GET 会被代理改写），
        # 这里一并支持，避免探测失败被误判成"服务未启动"。
        #
        # 【严重回归教训】这段曾经写在 _body() 之前，用
        #   self.rfile.read(ln) 自己读了一遍 body，
        # 结果后面的 _body() 是【第二次】读 —— 流已经被消耗，
        # HTTP/1.1 keep-alive 下 read() 会一直阻塞等读满，
        # 请求挂死 45s，浏览器超时放弃，服务端只留下
        #   WinError 10053（用户看到的就是"连接失败"）。
        # 症状（连不上）和原因（重复读 body）完全对不上，极难排查。
        #
        # 铁律：body 只能读一次。要复用就用 _body() 的缓存结果。
        if p == '/api/gate_login':
            return self.gate_login(b)
        if gate_enabled() and p not in GATE_OPEN \
                and not authorized(self.headers):
            return self._json({'ok': False, 'needLogin': True,
                               'err': '请先登录（账号密码）'}, 401)

        if p == '/api/version':
            # 把依赖状态一并报出去。
            # 为什么：从门户启动时，黑窗口的输出曾被引到 DEVNULL，
            # 依赖没装也看不出来，只表现为"连不上车"。
            # 让门户能主动问一句，比让用户去猜强。
            return self._json({'ok': True, 'version': VERSION,
                               'pid': os.getpid(),
                               'paramiko': HAVE_PARAMIKO,
                               'pil': HAVE_PIL,
                               'python': '%d.%d.%d' % sys.version_info[:3],
                               'exe': sys.executable})

        if p == '/api/quit':
            # 浏览器页面关闭时（beforeunload + sendBeacon）通知本服务退出。
            # 这样用户关掉网页，黑窗口也跟着结束，不用手动去关。
            # 反过来做不到：关黑窗口没法主动关浏览器标签（浏览器不允许），
            # 只能做到"关窗口时提示"，见 start.bat。
            log('收到页面关闭通知，即将退出')
            threading.Thread(target=self._quit_soon, daemon=True).start()
            return self._json({'ok': True})

        if p == '/api/ready':
            left = cleanup_remaining()
            return self._json({'ok': True, 'wait': round(left, 2),
                               'active': len(SESS)})

        if p == '/api/connect':
            # 清理没做完就点连接，直接拒绝并说明还要等几秒。
            # 比让它连上去然后卡在"连接中"要友好得多。
            left = cleanup_remaining()
            if left > 0:
                return self._json(
                    {'ok': False,
                     'err': '正在清理上一台车的进程，还需 %.1f 秒' % left,
                     'wait': round(left, 2)}, 400)
            return self.api_connect(b)
        if p == '/api/probe':
            return self.api_probe(b)
        if p == '/api/disconnect':
            global LAST_STOP_AT
            LAST_STOP_AT = time.time()
            sid = b.get('sid')
            s = SESS.pop(sid, None)
            if s:
                log('──── 断开请求 ────')
                log('  话题 : %s' % s.topic)
                log('  目标 : %s@%s:%s' % (s.user, s.host, s.port))
                log('  sid  : %s' % sid)
                s.close()
                log('  已断开，剩余活跃会话 = %d' % len(SESS))
            else:
                log('断开请求: sid %s 不存在（可能已断开）' % (sid or '?'))
            return self._json({'ok': True})
        if p == '/api/status':
            return self.api_status(b)
        self.send_error(404)

    def api_status(self, b):
        """单路状态。含车端 CPU/内存（1 秒采样，同车共享）。"""
        sid = b.get('sid')
        s = SESS.get(sid)
        if not s:
            return self._json({'ok': False, 'err': '会话不存在'})

        # 车端资源。采不到就给 None，前端不显示 —— 不能编造数字。
        sysinfo = None
        sm = getattr(s, 'sysmon', None)
        if sm and sm.alive():
            sysinfo = {'cpu': sm.cpu, 'mem': sm.mem_used,
                       'memtot': sm.mem_total}

        return self._json({'ok': True, 'frames': s.nframes, 'err': s.err,
                           'running': s.running, 'lag': round(s.lag, 3),
                           'topic': s.topic,
                           'enc': s.enc, 'rate': s.rate(),
                           'offset': round(s.clock_offset, 3),
                           'offset_unknown': s.clock_unknown,
                           'max_lag': s.max_lag,
                           'stall': (round(s.stall(), 1)
                                     if s.stall() is not None else None),
                           'hb': (round(s.hb_age(), 1)
                                  if s.hb_age() is not None else None),
                           # 取图时限：前端据此倒计时并在到点时提示重连
                           'remain': (round(s.remain(), 1)
                                      if s.remain() is not None else None),
                           'expired': s.expired,
                           'limit': MAX_SESSION_SEC,
                           'sys': sysinfo})

    # ── 建连 ──
    def api_connect(self, b):
        host = (b.get('host') or '').strip()
        port = int(b.get('port') or 22)
        user = (b.get('user') or '').strip()
        pwd = b.get('pwd') or ''
        topic = (b.get('topic') or '').strip()
        mtype = (b.get('mtype') or 'sensor_msgs/Image').strip()
        width = int(b.get('width') or 640)
        fps = float(b.get('fps') or 10)
        quality = int(b.get('quality') or 55)
        max_lag = float(b.get('max_lag') or 1.0)

        if not host or not topic:
            return self._json({'ok': False, 'err': '主机和话题不能为空'}, 400)
        if not MOCK and not HAVE_PARAMIKO:
            return self._json({'ok': False, 'err': '缺少 paramiko，请先 pip install paramiko'}, 500)
        if not MOCK and not user:
            return self._json({'ok': False, 'err': '用户名不能为空'}, 400)

        # nowait：不等首帧，立刻返回 sid。
        # 多路同开时必须用它 —— 否则 5 路串行各等 25 秒，最坏要两分钟。
        # 是否出图交给前端轮询 /api/status 判断。
        nowait = bool(b.get('nowait'))

        # 这台车的第一路之前，先把上次的残留进程清干净。
        # 不清的话僵尸会堆积（每次断开都可能留一个），CPU 被吃满后
        # 连接越来越慢，最终表现为"一直卡在连接中"。
        # 只有确认这台车上没有活跃会话时才清，避免误杀正在看的流。
        if not MOCK and not nowait_multi(host, port):
            sweep_host(host, port, user, pwd)

        # 记一条登录记录到车上（后台跑，不挡着出图）
        if not MOCK:
            threading.Thread(target=record_access,
                             args=(host, port, user, pwd), daemon=True).start()

        sid = uuid.uuid4().hex
        log('──── 连接请求 ────')
        log('  目标 : %s@%s:%s' % (user, host, port))
        log('  话题 : %s (%s)' % (topic, mtype))
        log('  参数 : 宽<=%s 帧率=%s 质量=%s 追帧=%ss' % (width, fps, quality, max_lag))
        log('  sid  : %s' % sid)
        s = CamSession(host, port, user, pwd, topic, mtype, width, fps)
        s.quality = quality
        s.max_lag = max_lag
        SESS[sid] = s
        s.start()
        log('  本次取图时限 : %s（到点自动断开，需再看请再次连接）'
            % ('无限制' if s.remain() is None else '%.0f 分钟' % (s.remain()/60.0)))
        log('  会话已建立，活跃会话数 = %d' % len(SESS))
        if nowait:
            log('  等待出图: %s' % topic)
            return self._json({'ok': True, 'sid': sid, 'frames': 0,
                               'pending': True, 'topic': topic})
        # 等首帧，最多 25 秒（SSH 握手 + 传脚本 + 订阅）
        t0 = time.time()
        while time.time() - t0 < 25:
            if s.err:
                break
            if s.nframes > 0:
                break
            time.sleep(0.2)
        if s.err:
            log('  失败: %s -> %s' % (topic, s.err))
            return self._json({'ok': False, 'err': s.err}, 500)
        if s.nframes == 0:
            log('  超时: %s 25 秒内无帧' % topic)
            return self._json({
                'ok': False,
                'err': '25 秒内没收到图像。检查：话题名是否正确 / 车上 roscore 是否在运行 / '
                       'rostopic hz %s 是否有数据 / 消息类型是否选对。\n'
                       '若画面能出但延迟很大，多半是带宽不够：降低「最大宽度」和「帧率」'
                       '（原始 RGB 在 640 宽 @10fps 就要 73Mbps）' % topic}, 500)
        log('  出图: %s (首帧 %ss, 编码=%s)' % (topic, round(s.lag, 3), s.enc))
        return self._json({'ok': True, 'sid': sid, 'frames': s.nframes,
                           'lag': round(s.lag, 3), 'enc': s.enc,
                           'offset': round(s.clock_offset, 3),
                           'offset_unknown': s.clock_unknown})

    # ── 探测话题 ──
    def api_probe(self, b):
        host = (b.get('host') or '').strip()
        port = int(b.get('port') or 22)
        user = (b.get('user') or '').strip()
        pwd = b.get('pwd') or ''
        if MOCK:
            return self._json({'ok': True, 'topics': [
                ['/camera_01/color/image_raw', 'sensor_msgs/Image'],
                ['/camera_02/color/image_raw', 'sensor_msgs/Image'],
                ['/camera_01/color/image_raw/compressed', 'sensor_msgs/CompressedImage'],
            ]})
        if not HAVE_PARAMIKO:
            return self._json({'ok': False, 'err': '缺少 paramiko'}, 500)
        try:
            import paramiko
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, port=port, username=user, password=pwd, timeout=15,
                        allow_agent=False, look_for_keys=False)
            cmd = (ROS_SRC +
                   'rostopic list 2>/dev/null | while read t; do '
                   'ty=$(rostopic type "$t" 2>/dev/null); '
                   'case "$ty" in *Image*) echo "$t|$ty";; esac; done')
            _, so, se = ssh.exec_command('bash -c ' + shell_quote(cmd), timeout=40)
            out = so.read().decode('utf-8', 'replace')
            err = se.read().decode('utf-8', 'replace')
            ssh.close()
            topics = []
            for ln in out.strip().split('\n'):
                if '|' in ln:
                    t, ty = ln.split('|', 1)
                    topics.append([t.strip(), ty.strip()])
            if not topics:
                return self._json({'ok': False,
                                   'err': '车上没找到图像话题。rostopic 是否可用？\n' + err[-500:]})
            return self._json({'ok': True, 'topics': topics})
        except Exception as e:
            return self._json({'ok': False, 'err': '%s: %s' % (type(e).__name__, e)})

    # ── MJPEG ──
    def stream(self):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        sid = (qs.get('sid') or [''])[0]
        s = SESS.get(sid)
        if not s:
            self.send_error(404, 'session not found')
            return
        self.send_response(200)
        self.send_header('Age', '0')
        self.send_header('Cache-Control', 'no-store, no-cache, private')
        self.send_header('Expires', '0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Content-Type',
                         'multipart/x-mixed-replace; boundary=%s' % BOUNDARY)
        self.end_headers()
        try:
            idle = 0
            while True:
                fr = s.get_frame(timeout=2.0)
                if fr is None:
                    idle += 1
                    # 故意不结束流：MJPEG 一断，浏览器的 img 就变空白，
                    # 反而分不清"卡住了"还是"断了"。保持连接让画面停在
                    # 最后一帧，真正的状态由 stall / hb 在状态栏说清楚。
                    if idle == 15:
                        log('已 30 秒无帧（画面停在最后一帧，状态栏会标中断）')
                    continue
                idle = 0
                # 协议第 4 项是拍摄时间戳（车钟）。这里不用它做年龄判断 —— 
                # get_frame 已经丢过旧帧了，这里只管画。
                kind, meta, data = fr[0], fr[1], fr[2]
                if kind == 'rgb':
                    w, h = meta
                    body = to_jpeg(data, w, h)
                    ctype = 'image/jpeg'
                else:
                    # 车上给的就是 jpeg/png 原始字节，直接透传
                    body = data
                    ctype = 'image/jpeg' if meta == b'JPGD' else 'image/png'
                self.wfile.write(b'--' + BOUNDARY.encode() + b'\r\n')
                self.wfile.write(('Content-Type: %s\r\n' % ctype).encode())
                self.wfile.write(('Content-Length: %d\r\n\r\n' % len(body)).encode())
                self.wfile.write(body)
                self.wfile.write(b'\r\n')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log('流异常:', e)


class ThreadedHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _hide_console():
    """Windows 下把控制台窗口最小化。

    用户不想看到黑窗口 —— 日志已经在页面的"日志"面板里显示了。
    只在 Windows 且确实有控制台时执行，其它平台直接跳过。
    """
    if os.name != 'nt':
        return
    try:
        import ctypes
        whnd = ctypes.windll.kernel32.GetConsoleWindow()
        if whnd:
            ctypes.windll.user32.ShowWindow(whnd, 6)   # SW_MINIMIZE
    except Exception:
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    _hide_console()
    srv = ThreadedHTTP(('127.0.0.1', port), Handler)
    log('服务已启动: http://127.0.0.1:%d' % port)
    # 单次取图时限守护（默认 10 分钟自动断开）
    threading.Thread(target=session_watchdog, daemon=True).start()
    if MAX_SESSION_SEC and MAX_SESSION_SEC > 0:
        log('单次取图时限 : %.0f 分钟（到点自动断开，需再看请再次连接）'
            % (MAX_SESSION_SEC / 60.0))
    else:
        log('单次取图时限 : 无限制')
    if MOCK:
        log('*** MOCK 模式：不会真的连 SSH，用假图像验证链路 ***')
    if not HAVE_PIL:
        log('警告: 没有 PIL，JPEG 编码会退化成 BMP（能用但流量大）。pip install pillow')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
