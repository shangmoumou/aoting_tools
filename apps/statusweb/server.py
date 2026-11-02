#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
远程机器状态监控 —— 本地服务

为什么要有一个本地服务：
    浏览器不能自己发起 SSH。所以和相机预览一样，由这个 Python 进程
    负责连车、跑采集脚本、把结果以 HTTP 交给页面。
    服务只监听 127.0.0.1，不外开端口；SSH 密码只在内存里，不落盘。

数据流：
    浏览器 --HTTP--> 本服务 --SSH--> 车上 stat_agent.py --stdout--> JSON 行

当前只做 CPU / 内存占用 TOP5。后续要加别的话题或指标，
改 stat_agent.py 多输出几个字段、页面多画几块即可，链路不用动。
"""

import os
import sys
import sys
import re
import json
import io
import time
import threading
import collections
import database_tools
from http.server import BaseHTTPRequestHandler

try:
    import socketserver
except Exception:
    socketserver = None

import http.server

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 内置 SSH 凭据（可选）──────────────────────────────────
# 管理员在 tools/status_pwd.txt 里预置机器人 SSH 密码后，
# 使用者从头到尾不需要输入任何密码：密码框会直接隐藏。
# 没有这个文件就退回"密码可留空"（走密钥 / agent 免密）。
def _qval(q, key):
    """从 query string 里取一个参数值（已 url 解码）。

    GET 兜底路由要用。单独抽出来是因为之前直接在 do_GET 分支里引用
    未定义的名字，handler 抛 NameError 后连接被掐断，前端只看到
    "Failed to fetch"，根本看不出是服务端崩了。
    """
    try:
        import urllib.parse as _up
    except Exception:
        return ''
    for part in (q or '').split('&'):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        if k.strip() == key:
            try:
                return _up.unquote_plus(v)
            except Exception:
                return v
    return ''


def _first_value(txt):
    """取第一个非空、非 # 注释的值行。

    为什么不复用 gate.Gate 那套：它是按「访问令牌」的模样写的，
    要求长度 >= 8 且纯 ASCII。SSH 密码比这自由得多 —— 一个 6 位的
    aoting 会被静默判为无效，表现出来就是"明明填了还是要我输密码"。
    """
    for ln in (txt or '').splitlines():
        s = ln.strip()
        if not s or s.startswith('#'):
            continue
        if '=' in s:
            s = s.split('=', 1)[1].strip()
        s = s.strip('"').strip("'")
        # 乱码检测：多编码试读时，非 ASCII 多半是解码错了而非真密码
        if s and all(32 <= ord(c) < 127 for c in s):
            return s
    return ''


def _default_pwd():
    root = os.path.dirname(os.path.dirname(HERE))      # 工具箱根目录
    for rel in ('tools/status_pwd.txt', 'status_pwd.txt'):
        fp = os.path.join(root, rel)
        if not os.path.exists(fp):
            continue
        for enc in ('utf-8-sig', 'utf-16', 'utf-8', 'gbk', 'latin-1'):
            try:
                with open(fp, 'r', encoding=enc) as f:
                    v = _first_value(f.read())
            except Exception:
                continue
            if v:
                return v
    return ''

# ── 已保存机器（连接记录）────────────────────────────────────────
# 存到用户主目录 ~/.aoting_tools/conn_profiles.json，而不是工具包目录：
# 每次下载新版都是解压到一个新目录（aoting_tools_v1.0.xx），
# 存在包里的话一升级就全没了 —— 保存功能等于白做。
def _prof_path():
    env = os.environ.get('AOTING_CONN_PROFILES')
    if env:
        return env
    try:
        base = os.path.expanduser('~')
    except Exception:
        base = ''
    if base:
        try:
            d = os.path.join(base, '.aoting_tools')
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, 'conn_profiles.json')
        except Exception:
            pass
    root = os.path.dirname(os.path.dirname(HERE))      # 工具箱根目录
    return os.path.join(root, 'tools', 'conn_profiles.json')


def _prof_enc(s):
    """密码做一层 base64。

    必须说清楚：这是【轻度混淆，不是加密】。目的是文件被人随手打开时
    不会一眼看到明文密码，挡不住真想读的人。本机单机工具，
    真要保证安全得用系统密钥环，那会引入额外依赖，这里不做。
    """
    import base64
    try:
        return base64.b64encode((s or '').encode('utf-8')).decode('ascii')
    except Exception:
        return ''


def _prof_dec(s):
    import base64
    try:
        return base64.b64decode((s or '').encode('ascii')).decode('utf-8')
    except Exception:
        return ''


def _profiles_load():
    fp = _prof_path()
    if not os.path.exists(fp):
        return [], fp
    j = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk', 'latin-1'):
        try:
            with io.open(fp, 'r', encoding=enc) as f:
                j = json.load(f)
            break
        except Exception:
            j = None
    if not isinstance(j, dict):
        return [], fp
    out = []
    for it in (j.get('items') or []):
        if not isinstance(it, dict):
            continue
        name = (it.get('name') or '').strip()
        host = (it.get('host') or '').strip()
        if not name or not host:
            continue
        try:
            port = int(it.get('port') or 0)
        except Exception:
            port = 0
        out.append({
            'name': name,
            'host': host,
            'port': port,
            'user': (it.get('user') or 'aoting').strip(),
            'pwd': _prof_dec(it.get('pwd64') or ''),
        })
    return out, fp


def _profiles_write(items):
    fp = _prof_path()
    blob = {
        '_comment': '远程机器状态监控 —— 已保存机器。改完不用重启服务（每次读盘）。'
                    '密码是 base64 轻度混淆，不是加密，别把这个文件发给别人。',
        'items': [{
            'name': x.get('name', ''),
            'host': x.get('host', ''),
            'port': int(x.get('port') or 0),
            'user': x.get('user') or 'aoting',
            'pwd64': _prof_enc(x.get('pwd') or ''),
        } for x in items],
    }
    d = os.path.dirname(fp)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    with io.open(fp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(blob, ensure_ascii=False, indent=1))
    return fp


def _app_ver():
    """版本号从 app.json 读，不在代码里写死。

    写死过一次就会再写死第二次：页面已经 v1.7 而服务还报 1.6，
    门户拿到的就是旧版本号。这里统一以 app.json 为唯一来源。
    """
    try:
        with io.open(os.path.join(HERE, 'app.json'), encoding='utf-8') as f:
            return (json.load(f).get('ver') or '1.0').strip()
    except Exception:
        return '1.0'


VER = _app_ver()

# 单次监控最长时长。到点自动断开，想继续就再点一次连接。
# 为什么要限制：一个没人看的采集循环会一直占着 SSH 连接和车上 CPU，
# 几台车各挂几个，后面的人就再也连不上了（相机那边踩过同样的坑）。
MAX_SESSION_SEC = float(os.environ.get('STATUS_MAX_SEC', 300))
# 剩余多少秒开始把倒计时标红提醒
WARN_SEC = 60.0
EXTEND_SEC = float(os.environ.get('STATUS_EXTEND_SEC', 300))


# ═══════════════ 日志 ═══════════════
LOG_MAX = 500
LOG_BUF = collections.deque(maxlen=LOG_MAX)
LOG_SEQ = 0
_LOG_LOCK = threading.Lock()


def log(*a):
    global LOG_SEQ
    line = '[srv] %s' % ' '.join(str(x) for x in a)
    with _LOG_LOCK:
        LOG_BUF.append(line)
        LOG_SEQ += 1
    try:
        sys.stderr.write(line + '\n')
        sys.stderr.flush()
    except Exception:
        pass


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def load_agent():
    """读车端采集脚本。

    为什么不内联进本文件：那份脚本要能单独看懂、单独测；
    内联的话改一次就要同步两处，迟早会不一致。
    代价是它必须随包分发（app.json 的 service.files 已列）。
    """
    p = os.path.join(HERE, 'stat_agent.py')
    if not os.path.isfile(p):
        raise RuntimeError('缺少 stat_agent.py（应在 %s）' % p)
    with open(p, encoding='utf-8') as f:
        return f.read()


# SSH exec 起的是非交互 shell，不会读 .bashrc —— 车上的 ROS 环境没加载，
# rospy 和自定义消息包（cln_msgs 之类）就都 import 不到。
# 逐个试常见的 setup.bash，把 ROS_PACKAGE_PATH / PYTHONPATH 补上。
ROS_SETUP_CANDIDATES = (
    # 先 ROS 本体，再工作空间 —— 工作空间的 setup.bash 是"叠加"语义，
    # 顺序反了会被后面的覆盖掉。
    "/opt/ros/noetic/setup.bash",
    "/opt/ros/melodic/setup.bash",
    "/opt/ros/kinetic/setup.bash",
    "/opt/ros/*/setup.bash",
    # 自定义消息包（cln_msgs 等）只在工作空间的 devel 空间里。
    # 只 source /opt/ros 的话 rospy 能 import，cln_msgs 就是 import 不到 ——
    # 症状是"能连上 ROS，但报 No module named 'cln_msgs'"。
    # 工作空间名各车不同，已知的写死，再用通配兜底。
    "/home/aoting/hrg_bee_dev_v3/devel/setup.bash",
    "/home/aoting/hrg_bee_dev_v3/install/setup.bash",
    "/home/aoting/aotingos/devel/setup.bash",
    "/home/aoting/aotingos/install/setup.bash",
    "/home/aoting/*/devel/setup.bash",
    "/home/*/*/devel/setup.bash",
    "/home/*/*/install/setup.bash",
    "~/catkin_ws/devel/setup.bash",
    "~/catkin_ws/install/setup.bash",
    "~/ws/devel/setup.bash",
)


def _ros_env_prefix():
    parts = []
    for f in ROS_SETUP_CANDIDATES:
        parts.append('[ -f %s ] && . %s' % (f, f))
    # 2>/dev/null 是必须的：不存在的候选会往 stderr 刷一堆"No such file"，
    # 混进采集脚本的输出里会把 JSON 流污染成不可解析。
    return '(' + '; '.join(parts) + ') 2>/dev/null; '


ROS_ENV_PREFIX = _ros_env_prefix()

# Linux 单个命令行参数的硬上限：MAX_ARG_STRLEN = 32 * PAGE_SIZE。
# 采集脚本整份源码经 python3 -c '<源码>' 下发时就是这个参数的全部内容，
# 一旦超过，execve 直接 E2BIG —— 现象是"退出码 1、零输出、收不到 hello 帧"，
# 极易被误判成 ROS 环境或账号问题。实测 131071 能过，131072 起失败。
MAX_ARG_STRLEN = 32 * 4096


# ═══════════════ 会话 ═══════════════
class StatSession(object):
    """一台机器一条 SSH 连接，采集脚本在上面跑，每秒吐一行 JSON。"""

    def __init__(self, host, port, user, pwd):
        self.host = host
        self.port = int(port)
        self.user = user
        # 密码可为空：有些机器走密钥/免密登录。
        # 留空时 password=None，并放开 agent 与本地密钥，
        # 否则 paramiko 会拿着空字符串去试密码认证，必失败。
        self.pwd = (pwd or '').strip()
        self.running = True
        self.err = None
        self.agent_log = []       # 车端 stderr，页面"车端日志"用
        self.latest = None        # 最近一次完整快照
        self.at = 0               # 拿到快照的时刻
        self.t0 = time.time()
        self.ssh = None
        self.stdin = None
        self.out = None
        self.expired = False
        self.need_pwd = False      # 免密失败后置 True，前端才弹密码框
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ── 对外 ──
    def age(self):
        return time.time() - self.at if self.at else None

    def left(self):
        return MAX_SESSION_SEC - (time.time() - self.t0)

    def extend(self, sec):
        """把本次会话的上限往后推 sec 秒（页面上的「延长」按钮）。"""
        try:
            sec = float(sec)
        except Exception:
            return False
        if sec <= 0 or sec > 86400:
            return False
        self.t0 += sec
        self.expired = False
        log('单次上限延长 %.0f 秒' % sec)
        return True

    def stop(self):
        """主动停止采集并打断阻塞，让清理真正跑起来。

        为什么不能只置 running=False（上一版的 bug）：
        _loop 结束时会调 out.channel.recv_exit_status()，而远端采集脚本
        是无限循环 —— 不给它 EOF 它永远不退出，这个调用就会一直阻塞。
        于是 finally 里的 _cleanup() 永远执行不到，车端进程没人杀。

        实测表现就是：点了断开，日志里只打印"已断开"，
        既没有"[清理] 已关闭"也没有"可能仍有残留" ——
        说明清理压根没被调用过。

        所以要主动打断：
          1) 关 stdin：车端脚本读到 EOF 会自己退出（它有 stdin 看门狗）
          2) 关 out channel：让阻塞中的 select / recv_exit_status 立刻返回
        """
        self.running = False
        self._hard_break()

    def _hard_break(self):
        """打断采集线程的阻塞读。"""
        try:
            if self.stdin is not None and self.stdin.channel:
                self.stdin.channel.close()
        except Exception:
            pass
        try:
            out = getattr(self, 'out', None)
            if out is not None and out.channel:
                out.channel.close()
        except Exception:
            pass

    def set_hz(self, v):
        """改采集频率：往车端 stdin 写一行 JSON。

        为什么走 stdin 而不是重连：重连会丢掉已订阅的话题（订阅是常驻的），
        而且 SSH 握手要一两秒，改个频率不该付出这种代价。
        """
        try:
            v = float(v)
        except Exception:
            return False, '频率不是数字'
        if v < 0.05 or v > 20:
            return False, '频率超出范围'
        ch = getattr(self, 'stdin', None)
        if ch is None:
            return False, '采集通道未就绪'
        try:
            ch.channel.send(json.dumps({'hz': v}) + '\n')
            log('采集频率改为 %s Hz' % v)
            return True, ''
        except Exception as e:
            return False, '下发失败: %s' % (str(e)[:80])

    def send_cmd(self, obj):
        """往车端 stdin 下发一行 JSON 命令（自定义话题 / 重算目录大小等）。

        和改频率同一条通道：重连 SSH 会丢掉已订阅的话题，代价太大。
        """
        ch = getattr(self, 'stdin', None)
        if ch is None:
            return False, '采集通道未就绪'
        try:
            ch.channel.send(json.dumps(obj) + '\n')
            return True, ''
        except Exception as e:
            log('stdin 下发异常 %s: %s' % (type(e).__name__, str(e)[:200]))
            return False, '下发失败: %s: %s' % (type(e).__name__, str(e)[:120])

    def send_flag_val(self, name, val):
        """把相机号这类数值写进标志文件（stdin 之外的备份通道）。"""
        cli = getattr(self, 'ssh', None)
        if cli is None:
            return False, 'SSH 未连接'
        cmd = 'echo %s > %s' % (int(val), name)
        try:
            _, o, e = cli.exec_command(cmd, timeout=10)
            code = o.channel.recv_exit_status()
            if code != 0:
                try:
                    eo = e.read().decode('utf-8', 'replace').strip()[:200]
                except Exception:
                    eo = ''
                return False, '退出码 %s %s' % (code, eo or '')
            return True, ''
        except Exception as ex:
            return False, '%s' % (type(ex).__name__)

    def send_flag(self, name, on):
        """第二条命令通道：在车上 touch / rm 一个标志文件。

        为什么还要这条路：stdin 在部分 SSH 链路上不通（通道不给 stdin、
        窗口满、车端读线程没起来都可能），表现出来就是"前端勾了开关、
        server 也返回 ok，但车端永远显示未开启"。

        标志文件不依赖采集通道：走一条独立的 exec 通道，成不成都在日志
        里写清楚。车端每秒看一次文件在不在，与 stdin 取"或"。
        """
        cli = getattr(self, 'ssh', None)
        if cli is None:
            return False, 'SSH 未连接'
        cmd = ('touch %s' % name) if on else ('rm -f %s' % name)
        log('标志文件: 下发 %s' % cmd)
        try:
            _, o, e = cli.exec_command(cmd, timeout=10)
            code = o.channel.recv_exit_status()
            try:
                eo = e.read().decode('utf-8', 'replace').strip()[:200]
            except Exception:
                eo = ''
            log('标志文件: 退出码=%s stderr=%s' % (code, eo or '(空)'))
            if code != 0:
                return False, '退出码 %s %s' % (code, eo or '')
            # 别信"命令发出去了"，回读一次确认文件真的在。
            # 这样"标志文件=通"五个字才是有依据的，不是自我安慰。
            try:
                _, o2, _ = cli.exec_command(
                    'test -f %s && echo YES || echo NO' % name, timeout=10)
                o2.channel.recv_exit_status()
                rb = (o2.read().decode('utf-8', 'replace') or '').strip()
            except Exception:
                rb = 'ERR'
            log('标志文件: 回读=%s' % (rb or '(空)'))
            if 'YES' not in rb:
                return False, '文件写入后回读不到(%s)' % (rb or '空')
            return True, ''
        except Exception as ex:
            log('标志文件: 异常 %s: %s' % (type(ex).__name__, str(ex)[:200]))
            return False, '下发失败: %s: %s' % (type(ex).__name__, str(ex)[:120])

    # ── 内部 ──
    def _loop(self):
        try:
            import paramiko
        except Exception:
            self.err = 'paramiko 未安装'
            log('paramiko 未安装')
            return
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            log('连接 %s@%s:%d ...' % (self.user, self.host, self.port))
            # 免密优先：没给密码就先走 agent + 本地密钥。
            # 只有这条路走不通才让页面弹出密码框 —— 使用者不该
            # 为了"可能不需要"的东西先填一遍。
            try:
                ssh.connect(self.host, port=self.port, username=self.user,
                            password=(self.pwd or None), timeout=15,
                            allow_agent=not self.pwd,
                            look_for_keys=not self.pwd)
            except Exception as e:
                auth_cls = getattr(paramiko, 'AuthenticationException', None)
                is_auth = auth_cls is not None and isinstance(e, auth_cls)
                if is_auth and not self.pwd:
                    self.need_pwd = True
                    self.err = '需要密码认证'
                    log('免密登录失败，需要密码')
                elif is_auth:
                    self.err = '认证失败（账号或密码不正确）'
                    log('认证失败')
                else:
                    self.err = 'SSH 连接失败: %s' % (str(e)[:120])
                    log('连接失败: %s' % self.err)
                self.running = False
                return
            self.ssh = ssh
            self._preclean(ssh)
            log('SSH 已连接，启动车端采集脚本')
            stdin, out, err = self._push_and_start(ssh, AGENT_SRC)
            if out is None:
                self.err = '车端采集脚本启动失败（见上方日志）'
                log('启动失败：源码既落不了盘，也超命令行上限')
                self.running = False
                return
            self.stdin = stdin      # 调频命令走这条通道，不用重连 SSH
            self.out = out          # stop() 时靠它打断阻塞读
            threading.Thread(target=self._pump_err, args=(err,), daemon=True).start()

            import select
            buf = b''
            graceful = False        # True = 远端自己结束的，才值得等退出码
            while self.running:
                if self.left() <= 0:
                    self.expired = True
                    log('已达单次上限 %.0f 秒，自动断开' % MAX_SESSION_SEC)
                    break
                try:
                    r, _, _ = select.select([out.channel], [], [], 0.5)
                except Exception:
                    break               # 通道被 stop() 关掉了
                if r:
                    ch = out.channel.recv(65536)
                    if not ch:
                        graceful = True
                        break
                    buf += ch
                    while b'\n' in buf:
                        ln, buf = buf.split(b'\n', 1)
                        if not self._hello(ln):
                            self._parse(ln)
                elif out.channel.exit_status_ready():
                    graceful = True
                    break
            # 只有远端自己结束才等退出码。主动断开 / 超时断开时远端还在跑，
            # 等下去就是永久阻塞 —— 这正是上一版车端进程杀不掉的根因。
            if graceful:
                try:
                    code = out.channel.recv_exit_status() if out.channel else None
                    if code not in (0, None):
                        log('采集脚本退出码 %s' % code)
                        # 退出码不等于 0 时，一定要把车端 stderr 的最后几行打出来。
                        # 只有退出码没有原因的话，"1" 这一个数字能指向的故障
                        # （ROS 环境、账号、话题、语法、长度超限……）太多了，
                        # 只能靠猜，一轮轮试。
                        tail = list(getattr(self, 'agent_log', []))[-8:]
                        if tail:
                            log('车端最后几行输出：')
                            for s in tail:
                                log('  ' + s)
                        else:
                            log('车端没有任何输出 —— 脚本没被真正执行'
                                '（源码长度超限 / ROS 环境缺失 / 落盘失败）')
                except Exception:
                    pass
        except Exception as e:
            self.err = str(e)[:200]
            log('采集失败: %s' % self.err)
        finally:
            self.running = False
            self._cleanup()

    def _write_remote(self, ssh, path, src):
        """把源码经 SSH 通道的 stdin 流写到车上指定路径。

        数据流没有 argv 的长度上限，所以这是送大文件的正路。
        写完回读字节数比对 —— 只信"命令发出去了"是不行的，
        /tmp 满了、只读挂载这类情况 cat 会静默写成 0 字节。
        """
        try:
            data = src.encode('utf-8')
            stdin, out, _e = ssh.exec_command('cat > %s' % path, timeout=20)
            stdin.write(data)
            stdin.flush()
            stdin.channel.shutdown_write()
            code = out.channel.recv_exit_status()
            if code != 0:
                log('落盘 %s 退出码 %s' % (path, code))
                return False
            _, o2, _ = ssh.exec_command('wc -c < %s' % path, timeout=10)
            got = int((o2.read().strip() or b'0'))
            if got < len(data):
                log('落盘 %s 不完整: %d/%d 字节' % (path, got, len(data)))
                return False
            return True
        except Exception as ex:
            log('落盘 %s 失败: %r' % (path, ex))
            return False

    def _push_and_start(self, ssh, src):
        """把采集脚本送上車并执行，返回 (stdin, out, err)。

        为什么不再用 python3 -c '<源码>'：
          源码会以【单个命令行参数】交给 execve，而 Linux 的
          MAX_ARG_STRLEN = 32*PAGE_SIZE = 131072 字节。超了直接 E2BIG，
          表现是「退出码 1、零输出、收不到 hello 帧、拿不到 pid」——
          看着像 ROS 环境 / 账号 / 网络故障，排查方向被彻底带偏。
          实测 131071 字节能过，131072 起 Argument list too long。

        主路改成落盘执行：源码走 stdin 流（无此上限），再用短命令跑。
        落盘和执行分两个通道，stdin 留给开关命令用，不互相占用。
        """
        n = len(src.encode('utf-8'))
        log('采集脚本 %d 字节（argv 上限 %d）' % (n, MAX_ARG_STRLEN))
        for d in ('/tmp', '/dev/shm', '$HOME'):
            p = '%s/aoting_stat_agent_%d.py' % (d, os.getpid())
            if self._write_remote(ssh, p, src):
                log('采集脚本已落盘 %s' % p)
                return ssh.exec_command(
                    ROS_ENV_PREFIX + 'python3 -u %s; rm -f %s' % (p, p),
                    timeout=None)
        if n < MAX_ARG_STRLEN - 8192:
            log('落盘不可用，回退 python3 -c（余量 %d 字节）'
                % (MAX_ARG_STRLEN - n))
            return ssh.exec_command(
                ROS_ENV_PREFIX + 'python3 -c %s' % shell_quote(src),
                timeout=None)
        log('启动失败：%d 字节超 argv 上限，且 /tmp、/dev/shm、$HOME 都不可写' % n)
        return None, None, None

    def _pump_err(self, err):
        """车端 stderr —— 这是排查"开关没生效"最直接的材料。

        车端脚本的每一条 log() 都走这里。以前只打到黑窗口，用户要么
        没看、要么黑窗口刷太快被冲掉。现在同时累积进 self.agent_log，
        页面上有专门的"车端日志"区域可以翻。
        """
        try:
            while True:
                ln = err.readline()
                if not ln:
                    break
                s = ln.decode('utf-8', 'replace').rstrip()
                if s:
                    log('[车] ' + s)
                    try:
                        self.agent_log.append(s)
                        if len(self.agent_log) > 300:
                            del self.agent_log[:100]
                    except Exception:
                        pass
        except Exception:
            pass

    def _hello(self, raw):
        """认采集进程的第一行自报帧，记住它的 PID。

        返回 True 表示这行已处理，调用方别再交给 _parse（它不是数据帧）。
        """
        try:
            d = json.loads(raw.decode('utf-8', 'replace'))
        except Exception:
            return False
        if not isinstance(d, dict) or d.get('type') != 'hello':
            return False
        self.agent_pid = int(d.get('pid') or 0) or 0
        self.agent_ppid = int(d.get('ppid') or 0) or 0
        log('车端采集进程 pid=%s ppid=%s'
            % (self.agent_pid or '?', self.agent_ppid or '?'))
        return True

    def _parse(self, raw):
        try:
            s = raw.decode('utf-8', 'replace').strip()
            if not s or not s.startswith('{'):
                return
            d = json.loads(s)
            # 彩色相机帧是独立的一种行：{"type":"rgb","cam":1,"jpeg":b64}
            # 它和每秒一行的状态帧共用 stdout，必须在这里分流，
            # 否则会被当成状态帧存进 self.latest，把整屏数据顶掉。
            if d.get('type') == 'rgb':
                self._on_rgb(d)
                return
            if d.get('rgb'):
                global RGB_DIAG
                RGB_DIAG = d.get('rgb') or {}
            self.latest = d
            self.at = time.time()
        except Exception as e:
            # 只报一次，否则每秒刷一行会把真正的错误埋掉
            if not getattr(self, '_parse_err', False):
                self._parse_err = True
                log('解析失败: %r / 原文: %s' % (e, raw[:120]))

    def _on_rgb(self, d):
        """收一帧彩色画面：base64 解回 JPEG，只留最新一帧。

        只留最新一帧是因为前端按固定间隔来取，堆积旧帧没意义，
        反而会让画面越播越滞后。
        """
        try:
            cam = int(d.get('cam') or 0)
        except Exception:
            return
        if not cam:
            return
        try:
            import base64
            data = base64.b64decode(d.get('jpeg') or '')
        except Exception:
            return
        if not data:
            return
        with RGB_LOCK:
            RGB_FRAMES[cam] = (data, float(d.get('ts') or time.time()))
            st = RGB_DIAG.get(str(cam))
            if isinstance(st, dict):
                st['n'] = int(d.get('n') or st.get('n') or 0)
                st['w'] = int(d.get('w') or st.get('w') or 0)
                st['h'] = int(d.get('h') or st.get('h') or 0)
                st['at'] = float(d.get('ts') or time.time())

    def _new_ssh(self):
        try:
            import paramiko
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(self.host, port=self.port, username=self.user,
                        password=(self.pwd or None), timeout=8,
                        allow_agent=not self.pwd,
                        look_for_keys=not self.pwd)
            return ssh
        except Exception:
            return None

    def _preclean(self, ssh):
        """连接后、启动采集前，先清掉上一轮遗留的节点。

        为什么需要：断开时如果清理没成功（网络闪断、PC 端被强杀），
        车上会留着上一轮的采集进程。再点连接就又起一个 —— rosnode
        list 里会看到两个 /aoting_stat_agent_<pid>_<时间戳>。
        不清的话，旧进程还在读 /proc、占 CPU，且新数据可能被旧进程
        抢着发，表现为"连上了但数据不对"。

        按 ROS 节点名前缀清理（名字里嵌着 pid），比按命令行匹配精确：
        远端 bash 的命令行含整份源码，同样带 aoting_stat_agent。
        """
        # 顺序非常重要：先按【进程】杀，再注销【节点名】。
        #
        # 上一版是反的：先 rosnode kill（只注销名字），再按名字 grep 复查
        # —— 名字没了就报"无遗留"，可进程还活得好好的。用户 ps 看到
        # bash -c 还在、rosnode list 里名字也还在，就是这个假象。
        #
        # 进程才是本体，必须按进程判定和复查。
        try:
            pat = '"[a]oting_stat_agent"'
            pids = self._run(ssh, 'pgrep -f %s || true' % pat,
                             timeout=10).split()
            pids = [p for p in pids if p.strip().isdigit()]
            if pids:
                log('[预清理] 发现 %d 个旧采集进程 %s，先 TERM 后 KILL'
                    % (len(pids), pids[:6]))
                self._run(ssh, 'kill -TERM %s 2>/dev/null' % ' '.join(pids),
                          timeout=10)
                time.sleep(1.2)
                left = [p for p in self._run(
                    ssh, 'pgrep -f %s || true' % pat,
                    timeout=10).split() if p.strip().isdigit()]
                if left:
                    self._run(ssh, 'kill -KILL %s 2>/dev/null'
                              % ' '.join(left), timeout=10)
                    time.sleep(0.3)
            # 进程清完再注销残留的节点名（进程被硬杀时 rosmaster 不会
            # 察觉，名字会挂好几分钟，rosnode list 看着像还活着）
            self._run(ssh, ROS_ENV_PREFIX +
                      'rosnode list 2>/dev/null '
                      '| grep -E "^/aoting_stat_agent_" '
                      '| xargs -r rosnode kill 2>/dev/null', timeout=20)
            left = [p for p in self._run(ssh, 'pgrep -f %s || true' % pat,
                                         timeout=10).split()
                    if p.strip().isdigit()]
            if left:
                log('[预清理] 仍有残留进程 %s' % left[:6])
            else:
                log('[预清理] 无遗留采集进程')
        except Exception as e:
            log('[预清理] 跳过: %s' % str(e)[:80])

    def _run(self, ssh, cmd, timeout=10):
        """在 ssh 上跑一条命令，等它真正结束，返回 stdout 文本。

        exec_command 是异步的：不等 recv_exit_status 就 close()，
        命令多半还没发出去就被掐断 —— 上一版"断开后车端进程还在"
        就是这么来的。timeout 参数对 exec_command 并不阻塞等待。
        """
        try:
            _, o, _ = ssh.exec_command(cmd, timeout=timeout)
            o.channel.recv_exit_status()
            try:
                return o.read().decode('utf-8', 'replace')
            except Exception:
                return ''
        except Exception:
            return ''

    def _kill_remote(self):
        """杀车端采集进程 + 父 shell + 注销 ROS 节点名。

        返回 (ok, 残留 pid 列表)。

        为什么不能只靠 pkill -f 匹配命令行：exec_command 把整份源码当
        命令行传，远端 bash -c 的命令行里同样含 aoting_stat_agent，
        字符串既会命中它、也会命中正在执行 pkill 的那个 shell ——
        靠命令行分辨谁该杀谁，本来就不牢靠。

        改成三层，缺一不可：
          1) 按 pid 杀采集进程（hello 帧自报）
          2) 按 ppid 杀 exec_command 起的父 bash —— 只杀 python 不杀它，
             它会永远挂在 SSH session 里。用户 ps 里看到的
             "bash -c (... python3 -c 源码 ...)" 就是它。
          3) rosnode kill 掉 /aoting_stat_agent_* —— 进程被硬杀后
             rosmaster 不会察觉，节点名会残留好几分钟；只看
             rosnode list 会误以为"进程还在"。
        """
        pids = [p for p in (getattr(self, 'agent_pid', 0) or 0,
                            getattr(self, 'agent_ppid', 0) or 0) if p]
        if not pids:
            log('[清理] 拿不到 pid（hello 帧没收到？），跳过按 pid 杀')
            return False, []

        CHK = 'kill -0 %d 2>/dev/null && echo ALIVE || echo GONE'

        for ssh, mine in ((self.ssh, True), (self._new_ssh(), False)):
            if not ssh:
                log('[清理] 连接不可用，换下一条')
                continue
            log('[清理] 用%s连接杀 pid=%s'
                % ('主' if mine else '新建', pids))
            try:
                # 先 TERM：让采集进程跑完 rospy.signal_shutdown()，
                # 节点名才会正常注销，而不是等 rosmaster 超时才发现。
                for p in pids:
                    self._run(ssh, 'kill -TERM %d 2>/dev/null' % p)
                time.sleep(1.2)
                for p in pids:
                    if 'ALIVE' in self._run(ssh, CHK % p):
                        log('[清理] pid=%d TERM 后仍存活，升级 KILL' % p)
                        self._run(ssh, 'kill -KILL %d 2>/dev/null' % p)
                time.sleep(0.3)

                # 注销 rosmaster 里残留的节点名（含被硬杀遗留的）。
                # 节点名形如 /aoting_stat_agent_<pid>_<时间戳>，
                # 所以按这个前缀清理，比按命令行匹配精确得多。
                self._run(ssh, ROS_ENV_PREFIX +
                          'rosnode list 2>/dev/null '
                          '| grep -E "^/aoting_stat_agent_" '
                          '| xargs -r rosnode kill 2>/dev/null', timeout=20)

                log('[清理] rosnode 注销 /aoting_stat_agent_*')
                # 复查按精确 pid，不用 pgrep 数字符串
                left = [p for p in pids if 'ALIVE' in self._run(ssh, CHK % p)]
                log('[清理] 复查剩余 pid=%s' % (left or '无'))

                if not mine:
                    try:
                        ssh.close()
                    except Exception:
                        pass
                return True, left
            except Exception as e:
                log('[清理] 异常: %s' % str(e)[:120])
                if not mine:
                    try:
                        ssh.close()
                    except Exception:
                        pass
                continue
        log('[清理] 所有连接都不可用，未能清理')
        return False, list(pids)

    def _cleanup(self):
        """关 SSH + 杀车端残留进程。

        为什么要显式 pkill：PC 端一断开，远端 bash 死了但 python 可能
        被 init 收养继续跑。相机那边就出现过"一路刷已发 N 帧"把 CPU
        吃满、后面再也连不上的情况。

        顺序很重要：必须趁主连接还活着先杀，杀完再 close。
        上一版是先 close 再另开连接去杀，成功率低得多。
        """
        ok, left = self._kill_remote()
        self.cleanup_ok = bool(ok and not left)
        self.cleanup_left = left
        if self.cleanup_ok:
            log('[清理] 车端采集进程已关闭')
        else:
            log('[清理] 车端进程可能仍有残留 (ok=%s, 剩余pid=%s)' % (ok, left))
        try:
            if self.ssh:
                self.ssh.close()
        except Exception:
            pass
        self.ssh = None

SESS = None
SESS_LOCK = threading.Lock()
AGENT_SRC = None
# 最近一次连接用的凭据。glog 提取这类「一次性作业」不依赖采集进程，
# 采集会话到点断开后照样要能用 —— 否则每次提取都要先重连一次机器。
LAST_CONN = None


# ═══════════════ 终端（交互式 shell）═══════════════
# 为什么不能用 exec_command 顶替：exec_command 每条命令开一个新 shell，
# `cd /home` 的效果到下一句就没了，那不是终端，是"逐条执行"。
# 这里用 invoke_shell 起一个真交互式 shell 并一直养着，
# cd / export / alias 这些状态会保留，用法和 xshell 一致。
# ── 纯文本输出清洗（保留兼容） ──
# invoke_shell 必须带 pty（不带就不是交互式 shell，bash 不读 .bashrc、
# 不出提示符），有 pty 就一定会带颜色/光标/标题这类转义序列。
# 旧版纯文本页面需要剥掉这些序列；当前交互终端由 xterm 解析原始输出，
# 不再调用这里的清洗函数，否则会丢失补全后的光标和编辑状态。
_ANSI_OSC = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?')   # 设标题
_ANSI_CSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')               # 颜色/光标
_ANSI_ESC = re.compile(r'\x1b[@-Z\\-_]')                            # 单字符序列
_ANSI_CHR = re.compile(r'\x1b[()][0-9A-Za-z]')                      # 字符集切换


def strip_ansi(txt):
    """剥掉终端控制序列，只留可见文本。

    裸 \r 统一换成 \n：pty 下进度条用 \r 回行首覆盖，前端 <pre> 不会
    处理覆盖语义，换成换行至少不丢内容（代价是进度条会占几行）。
    """
    if not txt:
        return txt
    txt = _ANSI_OSC.sub('', txt)
    txt = _ANSI_CSI.sub('', txt)
    txt = _ANSI_ESC.sub('', txt)
    txt = _ANSI_CHR.sub('', txt)
    txt = txt.replace('\x00', '')
    txt = txt.replace('\r\n', '\n').replace('\r', '\n')
    return txt


TERM_MAX_CHARS = 200000      # 输出缓冲上限，防止跑个 yes 把内存吃光


class TermSession(object):
    """一条交互式 shell。输出持续累积，页面按 offset 拉增量。"""

    def __init__(self, host, port, user, pwd):
        self.host = host
        self.port = int(port)
        self.user = user
        self.pwd = (pwd or '').strip()
        self.running = True
        self.err = None
        self.ssh = None
        self.chan = None
        self.buf = ''          # 最近 TERM_MAX_CHARS 个字符
        self.total = 0         # 累计收到的字符数（裁剪也不减）
        self.lock = threading.Lock()
        self.dec = None
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ── 对外 ──
    def send(self, cmd):
        ch = self.chan
        if ch is None:
            return False, '终端未就绪'
        try:
            # sendall 保证补全按键、粘贴文本等数据完整到达同一交互式 shell。
            ch.sendall(cmd)
            return True, ''
        except Exception as e:
            return False, '发送失败: %s' % (str(e)[:80])

    def fetch(self, since):
        with self.lock:
            if since < self.total - len(self.buf):
                # 客户端落后太多，那段已经被裁掉了：整段重发，
                # 不然会接不上，页面显示一堆断片。
                return {'txt': self.buf, 'len': self.total, 'reset': True,
                        'ready': self.chan is not None,
                        'running': self.running, 'err': self.err}
            start = since - (self.total - len(self.buf))
            return {'txt': self.buf[start:], 'len': self.total,
                    'ready': self.chan is not None,
                    'reset': False, 'running': self.running, 'err': self.err}

    def stop(self):
        self.running = False
        try:
            if self.chan:
                self.chan.close()
        except Exception:
            pass
        try:
            if self.ssh:
                self.ssh.close()
        except Exception:
            pass

    # ── 内部 ──
    def _push(self, txt):
        with self.lock:
            self.buf += txt
            self.total += len(txt)
            over = len(self.buf) - TERM_MAX_CHARS
            if over > 0:
                self.buf = self.buf[over:]

    def _loop(self):
        try:
            import paramiko
            import select
            import codecs
        except Exception as e:
            self.err = '缺少依赖: %s' % str(e)[:60]
            log('终端启动失败: %s' % self.err)
            self.running = False
            return
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(self.host, port=self.port, username=self.user,
                        password=(self.pwd or None), timeout=15,
                        allow_agent=not self.pwd,
                        look_for_keys=not self.pwd)
        except Exception as e:
            self.err = 'SSH 连接失败: %s' % (str(e)[:120])
            log('终端连接失败: %s' % self.err)
            self.running = False
            return
        self.ssh = ssh
        # get_pty 是要的：没有 pty 就不是交互式 shell，bash 不读 .bashrc、
        # 不给提示符、ls 不带颜色，跟真终端手感差很远。
        # 宽度给大一些，免得长行被 shell 自己折断了。
        chan = ssh.invoke_shell(term='xterm-256color', width=220, height=60)
        chan.settimeout(0.1)
        self.chan = chan
        log('终端已连接')
        dec = codecs.getincrementaldecoder('utf-8')('replace')
        while self.running:
            try:
                r, _, _ = select.select([chan], [], [], 0.3)
                if not r:
                    if chan.exit_status_ready():
                        break
                    continue
                data = chan.recv(65536)
                if not data:
                    break
                # 保留光标、颜色和擦除序列，由页面终端解析，补全后才能继续编辑。
                self._push(dec.decode(data))
            except Exception:
                if not self.running:
                    break
                time.sleep(0.2)
        self.running = False
        log('终端已断开')
        try:
            ssh.close()
        except Exception:
            pass


TERM = None
TERM_LOCK = threading.Lock()


def term_stop():
    global TERM
    with TERM_LOCK:
        t = TERM
        TERM = None
    if t:
        t.stop()


# ═══════════════ 地图 ═══════════════
# 地图名来自 /map_info_v3 的 base_info.name（如 HGTV），
# 用它拼出车上的 PNG 路径再拉回来显示。
MAP_DIR = "/home/aoting/user_config/map_database"
MAP_FILE = "map.png"
MAX_MAP_BYTES = 25 * 1024 * 1024

# 地图名会拼进远端路径，必须严格过滤：
# 不拦的话 ?name=../../../../etc/shadow 能读到车上任意文件。
# 只放行"看起来像地图名"的字符，不含 / 和 ..。
MAP_NAME_OK = re.compile(r'^[A-Za-z0-9._\-\u4e00-\u9fff]{1,64}$')


def _map_name_ok(name):
    """地图名是否可安全拼进远端路径。

    '..' 必须单独挡：正则里 '.' 是放行字符，'..' 会匹配通过，
    拼进路径就跳出 map_database 目录了（测试抓出来的）。
    判断只放这一个函数里 —— 正则匹配和调用处各判一套的话，
    迟早会出现"正则说可以、实际被拦"这种对不上的情况。
    """
    if not name:
        return False
    if '..' in name:
        return False
    # 点开头也不要：地图名是目录名，正常不会这样，放行它等于
    # 允许读隐藏目录（. 本身也在这条里被挡掉）。
    if name[0] == '.':
        return False
    return bool(MAP_NAME_OK.match(name))


def _sftp_read(cands):
    """读车上的 map.png。

    cands：候选目录名，按顺序试（先 name 后 id）。
    为什么要两级：地图名改过、或名字里含特殊字符时，磁盘上的目录名
    往往还是建图时生成的 id，只按 name 找就会报"车上没有这个文件"。
    id 是建图时定下来的、不会随改名变，正好兜底。

    优先复用已建立的采集连接（省掉 1~2 秒握手）—— paramiko 的
    transport 支持多 channel，开 sftp 不影响采集那条命令。

    返回 (data, err, used)：used 是实际命中的那个目录名，
    页面用来标明"按名称命中"还是"按 ID 命中"。
    """
    if isinstance(cands, str):
        cands = [cands]
    cands = [c for c in (cands or []) if c]
    if not cands:
        return None, '没有给出地图名', ''
    bad = [c for c in cands if not _map_name_ok(c)]
    if len(bad) == len(cands):
        return None, '地图名不合法（只允许中英文 / 数字 / . _ -）', ''
    return _sftp_read_any([c for c in cands if _map_name_ok(c)])


def _sftp_read_any(cands):
    """真正按候选列表逐个试。与 _sftp_read 分开是为了让前者只管校验。"""
    with SESS_LOCK:
        s = SESS
    if s is None:
        return None, '未连接机器', ''
    own = None
    ssh = getattr(s, 'ssh', None)
    if ssh is None:
        try:
            import paramiko
            own = paramiko.SSHClient()
            own.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            own.connect(s.host, port=s.port, username=s.user,
                        password=(s.pwd or None), timeout=10,
                        allow_agent=not s.pwd, look_for_keys=not s.pwd)
            ssh = own
        except Exception as e:
            return None, 'SSH 连接失败: %s' % (str(e)[:80]), ''
    try:
        sftp = ssh.open_sftp()
        errs = []
        try:
            for name in cands:
                p = '%s/%s/%s' % (MAP_DIR, name, MAP_FILE)
                try:
                    st = sftp.stat(p)
                except IOError:
                    errs.append(name)
                    continue          # 这一个没有，试下一个候选
                if st.st_size > MAX_MAP_BYTES:
                    return (None,
                            '地图文件过大（%.1f MB）' % (st.st_size / 1048576.0),
                            '')
                with sftp.open(p, 'rb') as f:
                    data = f.read()
                return data, '', name
            return (None,
                    '车上没有这些目录里的 %s：%s' % (
                        MAP_FILE,
                        ' / '.join('%s/%s' % (MAP_DIR, n) for n in errs)),
                    '')
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    except Exception as e:
        return None, '读取失败: %s' % (str(e)[:80]), ''
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:
                pass


def _list_maps():
    """列出车上已有的地图目录。

    话题没发布时（建图节点没起）靠这个兜底：让用户手动挑一张看。
    """
    with SESS_LOCK:
        s = SESS
    if s is None or getattr(s, 'ssh', None) is None:
        return None, '未连接机器'
    cmd = ('find %s -maxdepth 1 -mindepth 1 -type d '
           '-printf "%%f\\n" 2>/dev/null | sort' % MAP_DIR)
    try:
        _, out, _ = s.ssh.exec_command(cmd, timeout=10)
        raw = out.read().decode('utf-8', 'replace')
    except Exception as e:
        return None, '列目录失败: %s' % (str(e)[:80])
    names = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return names, ''


# ═══════════════ 彩色相机（原「远程相机实时预览」已并入本页） ═══════════════
#   为什么每路相机一条独立 SSH，而不是复用状态采集那条：
#     状态采集的 stdout 是 JSON 行，图像是二进制流，混在一条连接里
#     两边都解析不了。独立连接虽多一次握手，但互不干扰 —— 相机卡住
#     不会拖垮状态监控，反之亦然。
#
#   为什么门禁只卡相机、不卡整页：
#     状态监控是日常排障工具，全公司都在用；画面涉及现场，才需要
#     登录可追溯。整页加锁会让每次看 CPU 都要输账号，成本高收益低。
# ══════════════════════════════════════════════════════════════════════
#   访问记录为什么从 /tmp 挪到 /home/aoting/user_config：
#     /tmp 在多数系统上是 tmpfs（内存盘），重启即清空；即使在磁盘上，
#     也会被 systemd-tmpfiles 按天清理，且权限 1777 谁都能删 —— 作为
#     "事后可追溯"的留痕根本靠不住。user_config 在 /home 下，无定时清理。
#
#     目录不可写时仍退回 /tmp 再写一次：留一条可能被清的记录，
#     也强过一条都没有。
RGB_LOG_NAME = 'aoting_statusweb_access.log'
RGB_DIR = '/home/aoting/user_config'
RGB_LOG = RGB_DIR + '/' + RGB_LOG_NAME


def _find_root():
    """往上找含 tools/gate.py 的目录（工具箱根目录）。"""
    d = HERE
    for _ in range(5):
        if os.path.isfile(os.path.join(d, 'tools', 'gate.py')):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


_ROOT = _find_root()
GATE = None
if _ROOT:
    sys.path.insert(0, os.path.join(_ROOT, 'tools'))
    try:
        from gate import Gate
        GATE = Gate(_ROOT, cache_tag='gate')
    except Exception:
        GATE = None

_RGB_SESS = {}           # cookie -> {'user':..., 't':时刻}
_RGB_LOCK = threading.Lock()
RGB_COOKIE = 'aoting_rgb'


def rgb_gate_enabled():
    """门禁不可用（单独拷出、缺配置）时一律拒绝 —— fail closed。"""
    return GATE is not None and GATE.enabled()


def rgb_new_session(user):
    import secrets
    v = secrets.token_urlsafe(24)
    with _RGB_LOCK:
        _RGB_SESS[v] = {'user': user, 't': time.time()}
    return v


def rgb_cookie_of(headers):
    try:
        raw = headers.get('Cookie') or ''
    except Exception:
        return ''
    for part in raw.split(';'):
        k, _, v = part.partition('=')
        if k.strip() == RGB_COOKIE:
            return v.strip()
    return ''


def rgb_user(headers):
    """本次请求的登录账号；未登录返回 ''。

    注意是服务端判定，不靠页面有没有弹登录框 —— 页面上的锁可以绕过，
    服务端这道才是真锁。
    """
    if not rgb_gate_enabled():
        return ''
    with _RGB_LOCK:
        return (_RGB_SESS.get(rgb_cookie_of(headers)) or {}).get('user', '')


def _local_who_ips():
    import getpass
    import socket
    try:
        pc = socket.gethostname()
    except Exception:
        pc = 'unknown'
    try:
        who = getpass.getuser()
    except Exception:
        who = 'unknown'
    ips = []
    try:
        for info in socket.getaddrinfo(pc, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
    except Exception:
        pass
    return pc, who, ('+'.join(ips) if ips else 'unknown')


def rgb_record_access(host, port, user, pwd, account):
    """往车端追加一行访问记录：谁、哪台笔记本、什么时候看过这台车。

    只追加不覆盖 —— 多人共用一台车时，覆盖会只留最后一个人，
    前面的人等于没来过。
    """
    # paramiko 是按需导入的（本文件不要求启动时就有），这里单独判一次
    try:
        import paramiko     # noqa: F401
    except Exception:
        return False
    pc, who, ips = _local_who_ips()
    # 刻意用拼接而非 % 格式化：车端 date 的格式串里带 %Y %H，
    # 一旦作为 % 的左值就会被 Python 当占位符，直接 ValueError，
    # 而且发生在连接路径上，表现为"连不上车"，跟日志功能联想不到一起。
    whoami = ('pc=' + pc + ' | user=' + who + ' | account=' + (account or '-') +
              ' | ip=' + ips + ' | car=' + str(user) + '@' + str(host) + ':' +
              str(port) + ' | from=statusweb')
    # 目录不存在就建，建不了或不可写就退回 /tmp —— 宁可留在会被清的地方，
    # 也不要整条记录丢失。
    cmd = ('mkdir -p ' + RGB_DIR + ' 2>/dev/null; mkdir -p /tmp 2>/dev/null; '
           'D=' + RGB_DIR + '; '
           '[ -d "$D" ] && [ -w "$D" ] || D=/tmp; '
           'printf "%s car_time=%s\\n" "' + whoami +
           '" "$(date +%Y-%m-%d_%H:%M:%S_%Z)" >> "$D/' + RGB_LOG_NAME + '"')
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, port=int(port), username=user,
                    password=(pwd or None), timeout=12,
                    allow_agent=not pwd, look_for_keys=not pwd)
        ssh.exec_command('bash -c ' + shell_quote(cmd), timeout=15)
        time.sleep(0.5)      # exec_command 不等待完成，给它一点时间
        ssh.close()
        log('[彩色相机] 登录记录已写入车端 %s（目录不可写时退回 /tmp）'
            '（account=%s）' % (RGB_LOG, account))
        return True
    except Exception as e:
        log('[彩色相机] 登录记录写入失败（不影响取图）: %s' % str(e)[:100])
        return False


def _read_exactly(fh, n):
    """从通道里读满 n 字节；读不到返回 None（连接断了）。"""
    buf = b''
    while len(buf) < n:
        try:
            chunk = fh.read(n - len(buf))
        except Exception:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


# 彩色相机：帧由 stat_agent 通过采集通道回传（{"type":"rgb"} 行）。
# 不再单独开一条 SSH 跑 cam_agent.py —— 那个脚本是裸跑的，没有 ROS
# 环境，import rospy 必失败，表现就是"登录成功、已启动、永远 0 帧"。
RGB_FRAMES = {}      # cam -> (jpeg 字节, 车端时间戳)：只留最新一帧
RGB_LOCK = threading.Lock()
RGB_WANT = set()     # 当前勾选的相机号
RGB_DIAG = {}        # 车端回传的诊断（话题 / 类型 / 帧数 / 错误）


def rgb_stop_all():
    """关掉全部彩色相机：让车端【真退订】。

    取流已经并进 stat_agent，所以这里不再有独立 SSH 要关 —— 只要把
    开关命令发下去即可。两条通道都发（stdin 主路 + 标志文件备份）。
    """
    global RGB_WANT
    with SESS_LOCK:
        s = SESS
    with RGB_LOCK:
        had = bool(RGB_WANT)
        RGB_WANT = set()
        RGB_FRAMES.clear()
    if s is not None and getattr(s, 'stdin', None):
        s.send_cmd({'rgb': []})
    if s is not None and getattr(s, 'ssh', None):
        try:
            s.ssh.exec_command('rm -f /tmp/aoting_rgb_cams.txt', timeout=8)
        except Exception:
            pass
    if had:
        log('[彩色相机] 已全部关闭（车端退订）')


def disconnect_current():
    global SESS
    term_stop()          # 断开机器时终端一起收掉，不留挂着没人看的 shell
    rgb_stop_all()       # 相机流走的是独立 SSH，不断开会一直挂在车上
    with SESS_LOCK:
        s = SESS
        SESS = None
    if not s:
        log('[断开] 当前没有连接')
        return
    log('[断开] 开始，采集线程 alive=%s' % s.thread.is_alive())
    s.stop()
    s.thread.join(timeout=5)
    if s.thread.is_alive():
        # 线程还卡着（极端情况：SSH 已经不通，连 kill 命令都发不出去）。
        # 这时绝不能就这么返回 —— 否则车端进程彻底没人管了。
        log('[断开] 采集线程未退出，主线程兜底清理')
        try:
            s._cleanup()
        except Exception as e:
            log('[断开] 兜底清理异常: %s' % str(e)[:120])
    else:
        log('[断开] 采集线程已退出')


# 页面租约：区分多个标签页，刷新时给新页面接续时间。
PAGE_LEASES = {}
PAGE_LOCK = threading.Lock()
PAGE_GRACE = 8
PAGE_TTL = 180     # 后台标签页定时器可能被节流，不能按几秒无心跳就断开。


def page_touch(page, closing=False):
    """登记页面存活；关闭只缩短本页面租约，不影响其他标签页。"""
    with PAGE_LOCK:
        PAGE_LEASES[page] = time.monotonic() + (PAGE_GRACE if closing else PAGE_TTL)


def page_expire():
    """全部已登记页面过期时释放远程资源；无页面记录则不处理。"""
    with PAGE_LOCK:
        if not PAGE_LEASES:
            return
        now = time.monotonic()
        if any(deadline > now for deadline in PAGE_LEASES.values()):
            return
        PAGE_LEASES.clear()
        # 复用完整清理流程，释放采集、交互 shell、相机的 SSH 连接。
        disconnect_current()
        globals()['LAST_CONN'] = None  # 清掉作业备用凭据，避免无页面时重新连车。
        log('[页面关闭] 所有监控页面已离开，远程连接已释放')


def page_watchdog():
    """后台检查租约，覆盖关闭通知丢失和浏览器异常退出。"""
    while True:
        time.sleep(1)
        try:
            page_expire()  # 到期才触发清理，不以是否正在前台判断。
            database_tools.cleanup()  # 周期回收一小时过期的本地数据库快照。
        except Exception as e:
            log('[页面关闭] 清理失败: %s' % str(e)[:120])


# ═══════════════ glog 日志提取 ═══════════════
#   车上有多个进程各自写 glog（diagnose / tasks_manager ...），排查一个
#   问题时要把它们在「同一段时间」里说的话拼起来看。手工做法是先 ls 找出
#   时间对应的那几个文件，再逐个 grep，很慢还容易漏 —— 这里一键做完。
#
#   为什么在车端筛、而不是把文件拉回本地：
#     一个 glog 目录里常有上百个文件，全量回传既慢又占带宽。车端只回
#     命中时间窗的那些行，通常几 KB。
#
#   为什么用文件名圈候选：
#     glog 文件名自带开始时间（xxx_info_20260921-120349.4292）。先按它
#     排除目标窗口结束后才创建的文件，其余逐行检查时间，避免并行写入漏提。
#
#   为什么默认一个都不勾：
#     十几个目录全勾上车端要扫的文件量成倍涨、提取明显变慢，而排查
#     绝大多数时候只盯其中一两个。让用户自己选，而不是替他全开。
GLOG_BASE = '/home/aoting/user_config/glogs'
# system 目录不在 glogs 下，路径单独给。它跟上面 17 项同级、一样可勾选，
# 但两处格式都跟 glog 不同，GLOG_PY 里额外认：
#   文件名 system_20261019_191533.log（下划线分隔 + .log 后缀）
#   行格式 1792555122 | 2026-10-21 11:58:42  load_average: ...（epoch + ISO）
SYS_DIR = '/home/aoting/user_config/logs/system'
# 来源名直接用车上的目录名：排查时对着终端 ls 的输出找目录，
# 页面上再挂个中文别名反而要翻译一次，而且同音不同名最容易看错。
GLOG_DIRS = [
    {'key': 'diagnose', 'dir': GLOG_BASE + '/diagnose'},
    {'key': 'tasks_manager', 'dir': GLOG_BASE + '/tasks_manager'},
    {'key': 'bags_record', 'dir': GLOG_BASE + '/bags_record'},
    {'key': 'clean_mask_manager', 'dir': GLOG_BASE + '/clean_mask_manager'},
    {'key': 'cloudsync_manager', 'dir': GLOG_BASE + '/cloudsync_manager'},
    {'key': 'deep_perception', 'dir': GLOG_BASE + '/deep_perception'},
    {'key': 'depthcamera', 'dir': GLOG_BASE + '/depthcamera'},
    {'key': 'dynamic_controller', 'dir': GLOG_BASE + '/dynamic_controller'},
    {'key': 'elevatorLora_manager', 'dir': GLOG_BASE + '/elevatorLora_manager'},
    {'key': 'hardware_manger', 'dir': GLOG_BASE + '/hardware_manger'},
    {'key': 'maps_manager', 'dir': GLOG_BASE + '/maps_manager'},
    {'key': 'params_manager', 'dir': GLOG_BASE + '/params_manager'},
    {'key': 'perception', 'dir': GLOG_BASE + '/perception'},
    {'key': 'v3_debug', 'dir': GLOG_BASE + '/v3_debug'},
    {'key': 'BlueSea2LaserUDPLogPath', 'dir': GLOG_BASE + '/BlueSea2LaserUDPLogPath'},
    {'key': 'IMULogPath', 'dir': GLOG_BASE + '/IMULogPath'},
    {'key': 'LineLdaerUARTLogPath', 'dir': GLOG_BASE + '/LineLdaerUARTLogPath'},
    {'key': 'system', 'dir': SYS_DIR},
]
# 默认一个都不勾：十几个目录全勾上车端扫得久。排查通常只盯其中一两个，
# 让用户自己选，而不是替他全开。
GLOG_DEFAULT = set()
GLOG_CAP = 6000          # 最多回传多少行，超了截断并明确告知
GLOG_MAX_FILE = 300 * 1024 * 1024   # 单文件过大就直接跳过并注明
GLOG_TIMEOUT = 180       # 目录多了之后扫的量更大，120 秒不够用
# 目录里所有文件都认不出时间时，最多扫最近修改的这么多份。
# 不设上限的话，遇到一个堆了几百份文件的目录会把提取拖到超时。
GLOG_FALLBACK_SCAN = 30

# 下发到车上执行的提取脚本。
# 单引号禁令：整份会被 shell_quote 用单引号包起来再经 SSH 送到远端 bash，
# 正文里出现单引号会在多层引用后变形（stat_agent 的注释里有完整说明）。
GLOG_PY = r'''
import os, re, sys, json, time, datetime

NAME_RE = re.compile(r"(\d{8})[-_](\d{6})(?:\.\d+)?(?:\.log)?$")
LINE_RE = re.compile(r"^([IWEF])(\d{2})(\d{2})[ ]+(\d{2}):(\d{2}):(\d{2})\.(\d+)[ ]+(\d+)[ ]+(\S+)\][ ]?(.*)$")
# system 目录的行：1792555122 | 2026-10-21 11:58:42  load_average: ...
# 行首那串是 epoch，后面是 ISO。取 ISO：年月日读得出来，不用猜时区。
SYSLINE_RE = re.compile(r"^\s*\d+[ ]*\|[ ]*(\d{4})-(\d{1,2})-(\d{1,2})[ ]+(\d{1,2}):(\d{2}):(\d{2})")

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")

def tstr(ts):
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

def mkt(y, mo, d, h, mi, s, us):
    try:
        return time.mktime(datetime.datetime(y, mo, d, h, mi, s).timetuple()) + us / 1000000.0
    except Exception:
        return None

def run():
    dirs = json.loads(sys.argv[1])
    t0 = float(sys.argv[2])
    t1 = float(sys.argv[3])
    cap = int(sys.argv[4])
    lvs = sys.argv[5] if len(sys.argv) > 5 else ""
    maxf = int(sys.argv[6]) if len(sys.argv) > 6 else 314572800
    fbmax = int(sys.argv[7]) if len(sys.argv) > 7 else 30
    res = {"ok": True, "dirs": [], "lines": [], "trunc": False,
           "nfile": 0, "nline": 0}
    for spec in dirs:
        key = spec.get("key", "")
        dp = spec.get("dir", "")
        ent = {"key": key, "label": spec.get("label", key),
               "short": spec.get("short", key), "dir": dp,
               "total": 0, "picked": [], "files": [], "skip": [], "err": "",
               "nline": 0, "before": None, "after": None, "noscan": 0}
        res["dirs"].append(ent)
        if not os.path.isdir(dp):
            ent["err"] = "目录不存在"
            continue
        try:
            names = sorted(os.listdir(dp))
        except Exception as e:
            ent["err"] = "列目录失败: %s" % str(e)[:80]
            continue
        items = []
        for n in names:
            p = os.path.join(dp, n)
            if not os.path.isfile(p):
                continue
            m = NAME_RE.search(n)
            st = None
            if m:
                try:
                    dt = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
                    st = time.mktime(dt.timetuple())
                except Exception:
                    st = None
            items.append({"name": n, "path": p, "start": st})
        ent["total"] = len(items)
        known = [it for it in items if it["start"] is not None]
        known.sort(key=lambda x: x["start"])
        # xxx.INFO 是软链，通常指向当前正在写的那份；不排掉会扫两遍、重复输出
        seen = set()
        for it in known:
            try:
                seen.add(os.path.realpath(it["path"]))
            except Exception:
                pass
        picked = []
        # 同目录的不同级别、不同进程可能同时写入，下一份文件创建不代表
        # 上一份结束。只排除窗口结束后创建的文件，再按行时间精确筛选。
        for it in known:
            if it["start"] <= t1:
                picked.append(it)
        if not known:
            # 整个目录没有一份文件名带时间（命名规则不同，或压根不是 glog）。
            # 只能靠内容判断，但全扫风险太大：一个堆了几百份文件的目录会把
            # 提取拖到超时。按修改时间倒序取最近若干份 —— 排查要看的几乎
            # 总是最近的日志，而且这样多勾几个目录也不会明显变慢。
            unk = [it for it in items if it["start"] is None]
            try:
                unk.sort(key=lambda x: os.path.getmtime(x["path"]), reverse=True)
            except Exception:
                pass
            for it in unk[:fbmax]:
                try:
                    rp = os.path.realpath(it["path"])
                except Exception:
                    rp = it["path"]
                if rp in seen:
                    continue
                picked.append(it)
            if len(unk) > fbmax:
                ent["noscan"] = len(unk) - fbmax
        before = None
        after = None
        for it in known:
            if it["start"] <= t0:
                before = it
            if it["start"] >= t1 and after is None:
                after = it
        if before is not None:
            ent["before"] = {"name": before["name"], "t": tstr(before["start"])}
        if after is not None:
            ent["after"] = {"name": after["name"], "t": tstr(after["start"])}
        lines = []
        for it in picked:
            try:
                sz = os.path.getsize(it["path"])
            except Exception:
                continue
            if sz > maxf:
                ent["skip"].append(it["name"])
                continue
            fyear = datetime.datetime.now().year
            fmmdd = None
            if it["start"] is not None:
                fd = datetime.datetime.fromtimestamp(it["start"])
                fyear = fd.year
                fmmdd = fd.month * 100 + fd.day
            ent["picked"].append(it["name"])
            res["nfile"] += 1
            n_before = len(lines)
            try:
                fh = open(it["path"], "rb")
            except Exception:
                continue
            last_in = False
            last_t = 0.0
            try:
                for raw in fh:
                    try:
                        ln = raw.decode("utf-8", "replace")
                    except Exception:
                        continue
                    ln = ln.rstrip("\n").rstrip("\r")
                    if not ln.strip():
                        continue
                    m = LINE_RE.match(ln)
                    ms = SYSLINE_RE.match(ln) if not m else None
                    if not m and not ms:
                        # 续行（堆栈、多行 msg）：跟着上一条命中与否一起走，
                        # 否则报错现场最关键的堆栈会被整段丢掉。
                        if last_in:
                            lines.append({"t": last_t, "k": key, "s": "",
                                          "c": 1, "x": ln})
                        continue
                    if ms:
                        # system 目录的行没有 I/W/E/F 级别，级别过滤对它没意义。
                        # 不豁免的话，选「仅错误」会把 system 整段滤掉，
                        # 而排查卡顿时恰恰要看 load/cpu 这些。
                        ts = mkt(int(ms.group(1)), int(ms.group(2)),
                                 int(ms.group(3)), int(ms.group(4)),
                                 int(ms.group(5)), int(ms.group(6)), 0)
                        if ts is None:
                            last_in = False
                            continue
                        if ts < t0 or ts > t1:
                            last_in = False
                            continue
                        last_in = True
                        last_t = ts
                        lines.append({"t": ts, "k": key, "s": "", "c": 0, "x": ln})
                        continue
                    sev = m.group(1)
                    if lvs and sev not in lvs:
                        last_in = False
                        continue
                    mo = int(m.group(2))
                    dd = int(m.group(3))
                    hh = int(m.group(4))
                    mi = int(m.group(5))
                    ss = int(m.group(6))
                    us = int(m.group(7)[:6].ljust(6, "0"))
                    yr = fyear
                    # 只有月日没有年：跨年那份日志的行会突然变小，年份要 +1
                    if fmmdd is not None and (mo * 100 + dd) < fmmdd - 600:
                        yr = fyear + 1
                    ts = mkt(yr, mo, dd, hh, mi, ss, us)
                    if ts is None:
                        last_in = False
                        continue
                    if ts < t0 or ts > t1:
                        last_in = False
                        continue
                    last_in = True
                    last_t = ts
                    lines.append({"t": ts, "k": key, "s": sev, "c": 0, "x": ln})
            finally:
                try:
                    fh.close()
                except Exception:
                    pass
            # 记下这份文件贡献了多少行：命中清单里能一眼看出哪份是「主角」，
            # 而不是只知道扫过它。
            ent["files"].append({"name": it["name"],
                                 "n": len(lines) - n_before})
        ent["nline"] = len(lines)
        res["lines"].extend(lines)
    res["nline"] = len(res["lines"])
    res["lines"].sort(key=lambda x: x["t"])
    if len(res["lines"]) > cap:
        res["lines"] = res["lines"][:cap]
        res["trunc"] = True
    return res

try:
    emit(run())
except Exception as e:
    emit({"ok": False, "err": "车端提取失败: %s" % str(e)[:160]})
'''
# ═══════════════ 系统日志提取（dmesg / 串口 / system 目录） ═══════════════
#   和 glog 分开做成第二个输出框，原因有两个：
#     1) 这三类东西不是 glog，行内时间戳格式各不相同（ISO / syslog /
#        内核 dmesg -T），混进 glog 那套按文件名圈候选的逻辑里会互相污染；
#     2) 排查硬件类问题（掉盘、串口断连、USB 复位）时，要看的往往是
#        内核环缓冲和 system 目录，跟业务 glog 的排查路径完全不同。
#
#   system 目录的命名规则和 glog 不同：system_20261019_191533.log
#   （下划线分隔 + .log 后缀），所以 NAME_RE 要同时认这两种。
SYS_CAP = 4000           # 每组最多回传多少行
SYS_TIMEOUT = 120
SYS_MAX_FILE = 80 * 1024 * 1024
SYS_FALLBACK_SCAN = 30

SYSLOG_PY = r'''
import os, re, sys, json, time, datetime, subprocess

MON = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
       "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
NAME_RE = re.compile(r"(\d{8})[-_](\d{6})(?:\.\d+)?(?:\.log)?$")
ISO_RE = re.compile(r"^\s*\[?\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?")
GLOG_RE = re.compile(r"^([IWEF])(\d{2})(\d{2})[ ]+(\d{2}):(\d{2}):(\d{2})\.(\d+)")
SYSLOG_RE = re.compile(r"^([A-Za-z]{3})[ ]+(\d{1,2})[ ]+(\d{2}):(\d{2}):(\d{2})")
DMESG_RE = re.compile(r"^\[[ ]*[A-Za-z]{3}[ ]+([A-Za-z]{3})[ ]+(\d{1,2})[ ]+(\d{2}):(\d{2}):(\d{2})[ ]+(\d{4})\]")
# 串口/USB 断连的关键词。中文也列进去：车上有些日志是中文写的，
# 只按英文关键词 grep 会把最想看的那几条漏掉。
KEYS = ["usb", "tty", "serial", "disconnect", "disconnected", "reset",
        "ch34", "ftdi", "cp210", "pl230", "cdc_acm", "uart", "removed",
        "attached", "over-current", "串口", "断连", "断开", "复位", "掉线"]

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")

def tstr(ts):
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

def mkt(y, mo, d, h, mi, s, us):
    try:
        return time.mktime(datetime.datetime(y, mo, d, h, mi, s).timetuple()) + us / 1000000.0
    except Exception:
        return None

def parse_ts(ln, yhint):
    m = ISO_RE.match(ln)
    if m:
        us = int(m.group(7).ljust(6, "0")[:6]) if m.group(7) else 0
        return mkt(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                   int(m.group(4)), int(m.group(5)), int(m.group(6)), us)
    m = DMESG_RE.match(ln)
    if m:
        mo = MON.get(m.group(1).lower(), 0)
        if mo:
            return mkt(int(m.group(6)), mo, int(m.group(2)),
                       int(m.group(3)), int(m.group(4)), int(m.group(5)), 0)
    m = SYSLOG_RE.match(ln)
    if m:
        mo = MON.get(m.group(1).lower(), 0)
        if mo:
            return mkt(yhint, mo, int(m.group(2)), int(m.group(3)),
                       int(m.group(4)), int(m.group(5)), 0)
    m = GLOG_RE.match(ln)
    if m:
        us = int(m.group(7)[:6].ljust(6, "0"))
        return mkt(yhint, int(m.group(2)), int(m.group(3)),
                   int(m.group(4)), int(m.group(5)), int(m.group(6)), us)
    return None

def hit_kw(ln):
    low = ln.lower()
    for k in KEYS:
        if k in low:
            return True
    return False

def sh(args, tmo):
    try:
        pr = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        o, e = pr.communicate(timeout=tmo)
        return (o or b"").decode("utf-8", "replace")
    except Exception:
        return ""

def run():
    dp = sys.argv[1]
    t0 = float(sys.argv[2])
    t1 = float(sys.argv[3])
    cap = int(sys.argv[4])
    maxf = int(sys.argv[5]) if len(sys.argv) > 5 else 83886080
    fbmax = int(sys.argv[6]) if len(sys.argv) > 6 else 30
    res = {"ok": True, "t0": t0, "t1": t1, "dmesg": [], "system": [],
           "serial": [], "dm_note": "", "dm_all": 0, "dm_ts": 0,
           "files": [], "total": 0, "picked": [], "noscan": 0,
           "sys_err": "", "nline": 0, "trunc": False}

    # ── dmesg：内核环缓冲，看掉盘/USB 复位/UART 断连最快的一处 ──
    txt = sh(["dmesg", "-T"], 30)
    if not txt.strip():
        txt = sh(["dmesg"], 30)
        if txt.strip():
            res["dm_note"] = "dmesg -T 不可用，已退回 dmesg：时间戳是相对开机的秒数，无法换算成墙钟时间"
    if not txt.strip():
        res["dm_note"] = "dmesg 无输出（权限不足或内核禁用了 dmesg_restrict）"
    yhint = datetime.datetime.now().year
    dmlines = []
    for ln in txt.splitlines():
        if not ln.strip():
            continue
        res["dm_all"] += 1
        ts = parse_ts(ln, yhint)
        if ts is not None:
            res["dm_ts"] += 1
        dmlines.append((ts, ln))
    if res["dm_ts"] > 0:
        for ts, ln in dmlines:
            if ts is not None and t0 <= ts <= t1:
                res["dmesg"].append({"t": ts, "k": "dmesg", "x": ln})
    else:
        # 没有一条能读出墙钟时间：过滤无从谈起，但直接什么都不给也帮不上忙。
        # 全给（按上限截断），并明确注明「未做时间过滤」，避免被当成已过滤。
        res["dm_note"] = (res["dm_note"] + "；" if res["dm_note"] else "") \
            + "dmesg 没有可读的墙钟时间戳，以下为全部输出，未做时间过滤"
        for ts, ln in dmlines:
            res["dmesg"].append({"t": 0.0, "k": "dmesg", "x": ln})

    # ── 串口/USB 断连：从 dmesg 命中行里单独拎一份 ──
    for it in res["dmesg"]:
        if hit_kw(it["x"]):
            res["serial"].append({"t": it["t"], "k": "serial", "x": it["x"]})

    # system 目录已并进 glog 的来源清单（跟 diagnose、tasks_manager 一样勾选），
    # 这里不再扫：两处都出会重复，而且它那套行格式（epoch + ISO）放在
    # 按级别过滤的 glog 里也说不清。本接口只留 dmesg 和串口/USB 断连。
    for g in ("dmesg", "serial"):
        if len(res[g]) > cap:
            res[g] = res[g][:cap]
            res["trunc"] = True
    res["nline"] = len(res["dmesg"]) + len(res["serial"])
    return res

try:
    emit(run())
except Exception as e:
    emit({"ok": False, "err": "车端提取失败: %s" % str(e)[:160]})
'''


def _ssh_for_job():
    """取一条可用 SSH：优先复用采集连接，没有就用上次连接参数现开一条。

    glog 提取不依赖车端采集进程，而采集会话 300 秒到点会自动断开 ——
    不这么做的话，每次提取都得先重新连一次机器，很烦。
    返回 (ssh, own)：own 不为空表示这条是现开的，用完要关。
    """
    with SESS_LOCK:
        s = SESS
    ssh = getattr(s, 'ssh', None) if s else None
    if ssh is not None:
        try:
            tr = ssh.get_transport()
            if tr is not None and tr.is_active():
                return ssh, None
        except Exception:
            pass
    info = LAST_CONN
    if not info:
        return None, None
    try:
        import paramiko
        own = paramiko.SSHClient()
        own.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        own.connect(info['host'], port=info['port'], username=info['user'],
                    password=(info['pwd'] or None), timeout=10,
                    allow_agent=not info['pwd'], look_for_keys=not info['pwd'])
        return own, own
    except Exception as e:
        log('[glog] 现开 SSH 失败: %s' % str(e)[:100])
        return None, None


# ═══════════════ HTTP ═══════════════
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *a):
        pass      # 默认会往 stderr 刷访问日志，和我们的日志混在一起

    def _send(self, code, obj=None, ctype='application/json; charset=utf-8',
              cookie=None, xhdr=None):
        if obj is None:
            body = b''
        elif isinstance(obj, bytes):
            body = obj
        else:
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if cookie:
            # 会话 cookie：登录后才拿得到画面。HttpOnly 挡 JS 读取，
            # SameSite=Lax 足够（页面和接口同在 127.0.0.1:8766，同源）。
            self.send_header(
                'Set-Cookie',
                '%s=%s; Path=/; HttpOnly; SameSite=Lax' % (RGB_COOKIE, cookie))
        # 门户页面在 8899，来探测 8766 属于跨域。没有 CORS 头浏览器会直接
        # 拦截响应，门户永远以为"服务没起来"（failed to fetch），
        # 而黑窗口里其实已经跑起来了。camweb 一直有这几行，所以只有它正常。
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Expose-Headers', 'X-Map-Key')
        # 额外响应头（如地图实际命中的目录名），前端靠它知道走了哪级兜底
        if xhdr:
            for k, v in xhdr.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            if body:
                self.wfile.write(body)
        except Exception:
            pass

    def do_OPTIONS(self):
        self._send(204)

    def _body(self):
        ln = int(self.headers.get('Content-Length') or 0)
        if ln <= 0:
            return {}
        raw = self.rfile.read(ln)
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return {}

    def api_pc3d(self, b):
        """开关深度图订阅：勾了才订阅，取消立刻退订。

        深度图 30Hz 且单帧很大，不能像普通话题那样一直挂着，
        否则关了开关也只是"不显示"，车上还在白跑。

        【踩过的坑】这个接口前端是用 POST 调的，而最早我把它整段写在
        do_GET 里 —— 那里根本没有变量 b，一进去就 NameError，handler
        抛异常后连接被直接掐掉，前端拿到的是 "Failed to fetch"；
        同时 do_POST 里又没有这一条，POST 走到末尾 404。
        两个错叠在一起的表现就是「勾了开关，车端永远显示未开启」。
        现在 GET / POST 都走这里，不再分叉。
        """
        on = bool((b or {}).get('on'))
        try:
            cam = int((b or {}).get('cam') or 1)
        except Exception:
            cam = 1
        if cam not in (1, 2, 3, 4, 5):
            cam = 1
        with SESS_LOCK:
            s3 = SESS
        if not s3 or not s3.running:
            self._send(200, {'ok': False, 'err': '未连接', 'on': on,
                             'cam': cam})
            return
        # 两条通道都发：stdin 是主路，标志文件是备份。任一通即可。
        ok1, e1 = (False, '未就绪')
        if getattr(s3, 'stdin', None):
            ok1, e1 = s3.send_cmd({'pc3d': 1 if on else 0, 'cam': cam})
        ok2, e2 = s3.send_flag('/tmp/aoting_pc3d_on', on)
        ok3, e3 = s3.send_flag_val('/tmp/aoting_pc3d_cam', cam)
        log('深度点云 %s 相机 %02d（stdin=%s %s / 标志文件=%s %s / '
            '相机号=%s %s）'
            % ('开' if on else '关', cam, ok1, e1 or '', ok2, e2 or '',
               ok3, e3 or ''))
        okd = bool(ok1 or ok2)
        self._send(200, {'ok': okd, 'on': on, 'cam': cam,
                         'err': '' if okd else ('stdin:%s 标志文件:%s' % (e1, e2)),
                         'via': {'stdin': ok1, 'flag': ok2, 'cam': ok3}})

    def api_laser(self, b):
        """开关 /scan 与 /depth2laser 订阅。

        默认关：这两路都是高频 LaserScan，回调里逐点做三角函数和 TF
        变换，两路全开时能吃掉相当一部分单核。多数时候只想看 CPU /
        任务，点云是"要看才开"的。关掉时车端【真退订】，不是"不画"。

        与 pc3d 一样走 stdin + 标志文件双通道，且 GET / POST 共用本函数，
        避免再出现"写在某一个方法里导致另一个方法 404"的分叉问题。
        """
        on = bool((b or {}).get('on'))
        topic = str((b or {}).get('topic') or '/scan').strip()
        m = {'/scan': ('scan', '/tmp/aoting_scan_on'),
             '/depth2laser': ('d2l', '/tmp/aoting_d2l_on')}
        if topic not in m:
            topic = '/scan'
        key, flag = m[topic]
        with SESS_LOCK:
            s3 = SESS
        if not s3 or not s3.running:
            self._send(200, {'ok': False, 'err': '未连接', 'on': on,
                             'topic': topic})
            return
        ok1, e1 = (False, '未就绪')
        if getattr(s3, 'stdin', None):
            ok1, e1 = s3.send_cmd({key: 1 if on else 0})
        ok2, e2 = s3.send_flag(flag, on)
        log('激光点云 %s %s（stdin=%s %s / 标志文件=%s %s）'
            % ('开' if on else '关', topic, ok1, e1 or '', ok2, e2 or ''))
        okd = bool(ok1 or ok2)
        self._send(200, {'ok': okd, 'on': on, 'topic': topic,
                         'err': '' if okd else ('stdin:%s 标志文件:%s' % (e1, e2)),
                         'via': {'stdin': ok1, 'flag': ok2}})

    def api_log(self):
        """车端 stderr 原文。

        排查"开关勾了没反应"时看这里：车端收到命令会自己打印
        pc3d want -> True via stdin/flag，没打印就说明命令没到车端。

        【踩过的坑】这接口最早整段写在 do_GET 里，且把会话变量写成了
        SESSION 这个名字 —— 它从未定义过。于是一进来就 NameError，
        handler 抛异常后连接被掐掉，前端拿到的是 Failed to fetch，
        看起来像"服务没响应"，实际是服务端每次都在崩。
        现在 GET / POST 共用，不再分叉。
        """
        with SESS_LOCK:
            s4 = SESS
        lines = list(getattr(s4, 'agent_log', []) or []) if s4 else []
        self._send(200, {'ok': True, 'lines': lines[-120:],
                         'n': len(lines)})

    def api_profiles(self, b=None):
        """已保存机器。GET 取列表，POST 增 / 删 —— 共用一份实现。

        跟 api_log / api_pc3d 一样，不拆成两个方法：拆开之后
        「某个方法里用了不存在的变量」这类错误就会漏掉一个分支，
        而 handler 一抛异常，前端看到的只是 Failed to fetch。
        """
        if b is None:
            items, fp = _profiles_load()
            self._send(200, {'ok': True, 'items': items, 'path': fp})
            return
        act = (b.get('act') or '').strip()
        if act == 'del':
            name = (b.get('name') or '').strip()
            items, fp = _profiles_load()
            keep = [x for x in items if x.get('name') != name]
            if len(keep) == len(items):
                self._send(200, {'ok': False, 'err': '没找到「%s」' % name})
                return
            _profiles_write(keep)
            log('已删除连接记录：%s' % name)
            self._send(200, {'ok': True, 'items': keep, 'path': fp})
            return
        name = (b.get('name') or '').strip()
        host = (b.get('host') or '').strip()
        try:
            port = int(b.get('port') or 0)
        except Exception:
            port = 0
        user = (b.get('user') or 'aoting').strip()
        pwd = b.get('pwd') or ''
        if not name:
            self._send(200, {'ok': False, 'err': '请给这台机器起个名字'})
            return
        if not host or not port:
            self._send(200, {'ok': False, 'err': '主机和端口不能为空'})
            return
        items, fp = _profiles_load()
        rec = {'name': name, 'host': host, 'port': port, 'user': user, 'pwd': pwd}
        replaced = False
        for i, x in enumerate(items):
            if x.get('name') == name:
                items[i] = rec
                replaced = True
                break
        if not replaced:
            items.append(rec)
        _profiles_write(items)
        log('已保存连接记录：%s（%s@%s:%d）共 %d 台' % (name, user, host, port, len(items)))
        self._send(200, {'ok': True, 'items': items, 'path': fp, 'replaced': replaced})

    # ── 彩色相机 ────────────────────────────────────────────
    def api_rgb_state(self):
        """门禁状态 + 当前账号。页面据此决定要不要弹登录框。"""
        self._send(200, {
            'gate': rgb_gate_enabled(),
            'user': rgb_user(self.headers),
        })

    def api_rgb_login(self, b):
        if not rgb_gate_enabled():
            self._send(200, {'ok': True, 'user': '', 'skip': True})
            return
        u = (b.get('user') or '').strip()
        p = (b.get('pwd') or '')
        if not u:
            self._send(200, {'ok': False, 'err': '请填账号'})
            return
        # GATE.check 返回的是 dict（ok / err / src / user），不是元组。
        # 早期按 `ok, msg = GATE.check(...)` 两个值去接，字典的键被当成
        # 可迭代对象拆开 —— 密码明明校验通过了，却在返回时抛
        # "too many values to unpack (expected 2, got 3)"，用户只看到
        # 「无法登录」。现象看着像账号错，实际是解包错，极难排查。
        ok, msg = False, '门禁不可用'
        try:
            r = GATE.check(u, p) or {}
            ok = bool(r.get('ok'))
            msg = (r.get('err') or '').strip()
            src = r.get('src') or ''
            if ok:
                log('[彩色相机] 账号校验通过 user=%s 来源=%s' % (u, src or '-'))
        except Exception as e:
            msg = '校验异常: %s' % str(e)[:80]
        if not ok:
            log('[彩色相机] 登录失败 user=%s: %s' % (u, msg))
            self._send(200, {'ok': False, 'err': msg or '账号或密码不对'})
            return
        ck = rgb_new_session(u)
        log('[彩色相机] 登录成功 user=%s' % u)
        self._send(200, {'ok': True, 'user': u}, cookie=ck)

    def _rgb_denied(self):
        """门禁启用但未登录：画面一律不给，并说清原因。"""
        self._send(200, {'ok': False, 'needLogin': True,
                         'err': '需要先登录账号才能看相机画面'})

    def api_rgb(self, b):
        """启/停指定的相机。cams=[1,3] 这种列表。"""
        if rgb_gate_enabled() and not rgb_user(self.headers):
            return self._rgb_denied()
        with SESS_LOCK:
            s = SESS
        if not s or not s.running:
            self._send(200, {'ok': False, 'err': '未连接机器，请先点「连接」'})
            return
        on = bool(b.get('on'))
        try:
            cams = [int(c) for c in (b.get('cams') or [])]
        except Exception:
            cams = []
        cams = [c for c in cams if 1 <= c <= 5]
        if not on:
            rgb_stop_all()
            self._send(200, {'ok': True, 'on': False, 'cams': []})
            return
        if not cams:
            self._send(200, {'ok': False, 'err': '请至少勾选一路相机'})
            return
        # 换勾选时先清掉旧的：否则上一路还在订阅，白白占车上带宽
        rgb_stop_all()
        width = int(b.get('width') or 640)
        fps = float(b.get('fps') or 5)
        quality = int(b.get('quality') or 70)
        account = rgb_user(self.headers)
        # 车端登录记录：独立线程发，别阻塞"开始取图"的返回
        threading.Thread(
            target=rgb_record_access,
            args=(s.host, s.port, s.user, s.pwd, account),
            daemon=True).start()

        # 开关命令：主路走采集通道 stdin，备份走车上标志文件。
        # 只走一条的话，stdin 不通的车上会表现为"勾了没反应"。
        ok1, e1 = (False, '未就绪')
        if getattr(s, 'stdin', None):
            ok1, e1 = s.send_cmd({'rgb': cams,
                                  'rgb_cfg': {'w': width, 'fps': fps,
                                              'q': quality}})
        ok2, e2 = (False, '未就绪')
        if getattr(s, 'ssh', None):
            try:
                _, o, e = s.ssh.exec_command(
                    'echo %s > /tmp/aoting_rgb_cams.txt'
                    % (','.join(str(c) for c in cams)), timeout=8)
                code = o.channel.recv_exit_status()
                ok2 = (code == 0)
                e2 = '' if ok2 else ('退出码 %s' % code)
            except Exception as ex:
                e2 = '%s' % (type(ex).__name__)
        log('[彩色相机] 开启 %s（stdin=%s %s / 标志文件=%s %s）'
            % (cams, ok1, e1 or '', ok2, e2 or ''))
        with RGB_LOCK:
            RGB_WANT = set(cams)
        okd = bool(ok1 or ok2)
        self._send(200, {'ok': okd, 'on': True, 'cams': cams,
                         'err': '' if okd else ('stdin:%s 标志文件:%s'
                                                % (e1, e2)),
                         'via': {'stdin': ok1, 'flag': ok2},
                         'note': '已下发，车端订阅后 1~2 秒出画面'})

    def api_rgb_frame(self):
        """返回某一路的最新一帧 JPEG。

        没帧时返回 204（不是错误）—— 前端据此显示"等待画面"，
        而不是当作服务异常去重试。
        """
        if rgb_gate_enabled() and not rgb_user(self.headers):
            return self._rgb_denied()
        q = self.path.split('?', 1)[1] if '?' in self.path else ''
        try:
            cam = int(_qval(q, 'cam') or 0)
        except Exception:
            cam = 0
        with RGB_LOCK:
            fr = RGB_FRAMES.get(cam)
            diag = dict(RGB_DIAG.get(str(cam)) or {})
            want = cam in RGB_WANT
        if fr is None:
            # 把车端诊断一起回给页面：只看"等待画面"永远查不出为什么。
            err = diag.get('err') or ''
            if not want:
                err = err or '未开启（勾选「订阅并显示」后车端才订阅）'
            else:
                err = err or '等待车端画面…'
            self._send(200, {'ok': False, 'wait': True,
                             'n': diag.get('n', 0), 'err': err,
                             'diag': diag},
                       ctype='application/json; charset=utf-8')
            return
        data, ts = fr
        age = max(0.0, time.time() - ts) if ts else -1
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Age', '%.2f' % age)
        self.send_header('X-Frame-N', str(diag.get('n', 0)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self):
        p = self.path.split('?')[0]
        if p in ('/', '/index.html'):
            try:
                with open(os.path.join(HERE, 'index.html'), 'rb') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
            except Exception:
                self._send(500, {'err': 'index.html 读取失败'})
            return
        # 仅开放随包发布的终端资源，固定清单避免任意路径读取。
        terminal_assets = {'/vendor/xterm/xterm.js': 'application/javascript',
                           '/vendor/xterm/xterm.css': 'text/css'}
        if p in terminal_assets:
            try:
                with open(os.path.join(HERE, p.lstrip('/')), 'rb') as f:
                    self._send(200, f.read(), terminal_assets[p])
            except OSError:
                self._send(404, {'err': '终端组件文件不存在'})
            return
        if p == '/database.js':
            with open(os.path.join(HERE, 'database.js'), 'rb') as f:
                self._send(200, f.read(), 'application/javascript')
            return
        if p == '/api/database/download':
            try:
                token = _qval(self.path.split('?', 1)[-1], 'token')
                self._send(200, database_tools.download(token), 'application/octet-stream')
            except Exception as e:
                self._send(400, {'ok': False, 'err': str(e)[:600]})
            return
        if p == '/api/version':
            self._send(200, {'ver': VER, 'version': VER, 'maxSec': MAX_SESSION_SEC,
                             'hasDefaultPwd': bool(_default_pwd())})
            return
        if p == '/api/profiles':
            return self.api_profiles()
        if p == '/api/rgb_state':
            return self.api_rgb_state()
        if p == '/api/rgb_frame':
            return self.api_rgb_frame()
        if p == '/api/extend':
            with SESS_LOCK:
                s = SESS
            if not s or not s.running:
                self._send(200, {'ok': False, 'err': '未连接机器'})
                return
            try:
                sec = float(self.path.split('sec=')[1].split('&')[0])
            except Exception:
                sec = EXTEND_SEC
            self._send(200, {'ok': s.extend(sec),
                             'left': max(0.0, s.left())})
            return
        if p == '/api/stat':
            with SESS_LOCK:
                s = SESS
            if not s:
                self._send(200, {'connected': False})
                return
            self._send(200, {
                'connected': s.running,
                'data': s.latest,
                'age': s.age(),
                'left': max(0.0, s.left()),
                'expired': s.expired,
                'err': s.err,
            })
            return
        if p == '/api/agentlog':
            return self.api_log()
        if p == '/api/pc3d':
            # 兜底：也允许 GET ?on=1。真正的调用方走 do_POST。
            q = self.path.split('?', 1)[1] if '?' in self.path else ''
            return self.api_pc3d({'on': ('on=1' in q) or ('on=true' in q)})
        if p == '/api/laser':
            q = self.path.split('?', 1)[1] if '?' in self.path else ''
            return self.api_laser({'on': ('on=1' in q) or ('on=true' in q),
                                   'topic': _qval(q, 'topic') or '/scan'})
        if p == '/api/term':
            with TERM_LOCK:
                t = TERM
            if not t:
                self._send(200, {'running': False, 'txt': '', 'len': 0,
                                 'err': None})
                return
            try:
                since = int(self.path.split('since=')[1].split('&')[0])
            except Exception:
                since = 0
            self._send(200, t.fetch(since))
            return
        if p == '/api/mapupdate':
            with SESS_LOCK:
                s2 = SESS
            if not s2 or not s2.running or not getattr(s2, 'stdin', None):
                self._send(200, {'ok': False, 'err': '未连接机器'})
                return
            try:
                s2.stdin.write(json.dumps({'map_refresh': True}) + '\n')
                s2.stdin.flush()
                self._send(200, {'ok': True})
            except Exception as e:
                self._send(200, {'ok': False, 'err': str(e)[:120]})
            return
        if p == '/api/maps':
            names, err = _list_maps()
            self._send(200, {'ok': names is not None, 'names': names or [],
                             'err': err, 'dir': MAP_DIR})
            return
        if p == '/api/map':
            name = ''
            mid = ''
            if '?' in self.path:
                try:
                    name = self.path.split('name=')[1].split('&')[0]
                except Exception:
                    name = ''
                try:
                    mid = self.path.split('id=')[1].split('&')[0]
                except Exception:
                    mid = ''
            try:
                import urllib.parse
                name = urllib.parse.unquote_plus(name or '')
                mid = urllib.parse.unquote_plus(mid or '')
            except Exception:
                pass
            # name 优先，找不到再用 id —— 见 _sftp_read 里的说明。
            data, err, used = _sftp_read([(name or '').strip(),
                                          (mid or '').strip()])
            if data is None:
                self._send(404, {'ok': False, 'err': err})
            else:
                self._send(200, data, 'image/png',
                           xhdr={'X-Map-Key': used})
            return
        if p == '/api/logs':
            since = 0
            if '?' in self.path:
                try:
                    since = int(self.path.split('since=')[1].split('&')[0])
                except Exception:
                    since = 0
            with _LOG_LOCK:
                items = list(LOG_BUF)
                seq = LOG_SEQ
            # 客户端已拿到 since 条，只回增量
            if since <= seq:
                keep = max(0, len(items) - (seq - since))
                items = items[keep:]
            self._send(200, {'lines': items, 'seq': seq})
            return
        self._send(404, {'err': 'not found'})

    def do_POST(self):
        p = self.path.split('?')[0]
        b = self._body()
        if p == '/api/database':
            ssh = own = None
            try:
                act = b.get('act')
                if act in ('capture', 'list'):
                    ssh, own = _ssh_for_job()  # 复用机器连接，必要时创建短时作业连接。
                    if ssh is None:
                        raise ValueError('请先连接机器')
                    result = (database_tools.capture(ssh, b.get('name'), shell_quote)
                              if act == 'capture' else database_tools.listing(ssh, shell_quote))
                    result['machine'] = '%s:%s' % (getattr(ssh.get_transport(), 'getpeername', lambda: ('', ''))())
                    self._send(200, result)
                elif act == 'browse':
                    self._send(200, database_tools.browse(b))
                elif act == 'csv':
                    self._send(200, database_tools.browse(b, export=True), 'text/csv; charset=utf-8')
                elif act == 'zip':
                    self._send(200, database_tools.archive(b.get('tokens')), 'application/zip')
                else:
                    raise ValueError('数据库操作无效')
            except Exception as e:
                self._send(400, {'ok': False, 'err': str(e)[:600]})
            finally:
                if own:
                    own.close()
            return
        if p == '/api/page_lifecycle':
            page = b.get('page')
            if not isinstance(page, str) or not page or len(page) > 100:
                self._send(400, {'ok': False, 'err': '页面标识无效'})
                return
            # 每个标签页独立续租；离开事件只关闭自己的租约。
            page_touch(page, b.get('act') == 'bye')
            self._send(200, {'ok': True})
            return
        if p == '/api/agentlog':
            return self.api_log()
        if p == '/api/profiles':
            return self.api_profiles(b)
        if p == '/api/connect':
            return self.api_connect(b)
        if p == '/api/set_hz':
            with SESS_LOCK:
                s = SESS
            if not s:
                self._send(200, {'ok': False, 'err': '未连接'})
                return
            okd, err = s.set_hz(b.get('hz'))
            self._send(200, {'ok': okd, 'err': err, 'hz': b.get('hz')})
            return
        if p == '/api/pc3d':
            return self.api_pc3d(b)
        if p == '/api/laser':
            return self.api_laser(b)
        if p == '/api/rgb_login':
            return self.api_rgb_login(b)
        if p == '/api/rgb':
            return self.api_rgb(b)
        if p == '/api/glog':
            return self.api_glog(b)
        if p == '/api/syslog':
            return self.api_syslog(b)
        if p == '/api/custom':
            # 自定义话题：订阅 / 取消 / 重新统计目录大小
            with SESS_LOCK:
                s = SESS
            if not s:
                self._send(200, {'ok': False, 'err': '未连接'})
                return
            act = (b.get('act') or '').strip()
            topic = (b.get('topic') or '').strip()
            if act == 'du':
                okd, err = s.send_cmd({'du': 1})
                self._send(200, {'ok': okd, 'err': err})
                return
            if not topic:
                self._send(400, {'ok': False, 'err': '话题名为空'})
                return
            if not topic.startswith('/'):
                topic = '/' + topic
            if act == 'add':
                okd, err = s.send_cmd({'add_topic': topic})
                log('订阅自定义话题 %s' % topic)
            elif act == 'del':
                okd, err = s.send_cmd({'del_topic': topic})
                log('取消自定义话题 %s' % topic)
            else:
                self._send(400, {'ok': False, 'err': '未知动作'})
                return
            self._send(200, {'ok': okd, 'err': err, 'topic': topic})
            return
        if p == '/api/term':
            act = (b.get('act') or '').strip()
            if act == 'start':
                with SESS_LOCK:
                    s = SESS
                if not s:
                    self._send(200, {'ok': False, 'err': '未连接机器'})
                    return
                term_stop()
                t = TermSession(s.host, s.port, s.user, s.pwd)
                with TERM_LOCK:
                    globals()['TERM'] = t
                log('终端启动中')
                self._send(200, {'ok': True})
                return
            with TERM_LOCK:
                t = TERM
            if not t:
                self._send(200, {'ok': False, 'err': '终端未启动'})
                return
            if act == 'send':
                okd, err = t.send(b.get('cmd') or '')
                self._send(200, {'ok': okd, 'err': err})
                return
            if act == 'resize':
                try:
                    cols = max(20, min(400, int(b.get('cols', 100))))
                    rows = max(5, min(120, int(b.get('rows', 18))))
                    # 同步远程 PTY 尺寸，让补全列表和长命令按可见区域换行。
                    t.chan.resize_pty(width=cols, height=rows)
                    self._send(200, {'ok': True})
                except Exception as e:
                    self._send(200, {'ok': False, 'err': '终端尺寸调整失败: %s' % str(e)[:80]})
                return
            if act == 'stop':
                term_stop()
                log('终端已关闭')
                self._send(200, {'ok': True})
                return
            self._send(400, {'ok': False, 'err': '未知动作'})
            return
        if p == '/api/disconnect':
            disconnect_current()
            log('已断开')
            self._send(200, {'ok': True})
            return
        self._send(404, {'err': 'not found'})

    def api_glog(self, b):
        """glog 日志提取：把多个日志目录里同一时间窗内的行按时间合并。

        返回前会把「为什么没有」讲清楚 —— 只回一句"未找到"的话，用户
        无法判断是时间填错了、还是车上根本没存到那段时间。
        """
        try:
            t = float(b.get('t') or 0)
            win = float(b.get('win') or 0)
        except Exception:
            self._send(400, {'ok': False, 'err': '时间或窗口不是数字'})
            return
        if t <= 0:
            self._send(400, {'ok': False,
                             'err': '请先填基准时间（格式 2026-10-20 11:18:00）'})
            return
        if win <= 0:
            self._send(400, {'ok': False, 'err': '时间窗必须大于 0'})
            return
        lv = (b.get('lv') or '').strip().upper()
        lv = ''.join(c for c in lv if c in 'IWEF')
        srcs = b.get('srcs') or []
        picked = [d for d in GLOG_DIRS if d['key'] in srcs]
        if not picked:
            self._send(400, {'ok': False, 'err': '至少勾选一个日志来源'})
            return
        ssh, own = _ssh_for_job()
        if ssh is None:
            self._send(200, {'ok': False, 'err': '未连接机器，请先连接'})
            return
        t0 = t - win
        t1 = t + win
        # 只传 key：页面上的来源名就是目录名，服务端不再另存一份中文别名，
        # 两边一旦不同步，输出里的前缀就会和勾选框对不上。
        spec = [{'key': d['key'], 'dir': d['dir']} for d in picked]
        cmd = ('python3 -u -c %s %s %.3f %.3f %d %s %d %d'
               % (shell_quote(GLOG_PY), shell_quote(json.dumps(spec)),
                  t0, t1, GLOG_CAP, shell_quote(lv), GLOG_MAX_FILE,
                  GLOG_FALLBACK_SCAN))
        try:
            _, o, _e = ssh.exec_command(cmd, timeout=GLOG_TIMEOUT)
            try:
                o.channel.settimeout(GLOG_TIMEOUT)
            except Exception:
                pass
            raw = o.read().decode('utf-8', 'replace')
        except Exception as e:
            if own is not None:
                try:
                    own.close()
                except Exception:
                    pass
            self._send(200, {'ok': False, 'err': '车端执行失败: %s' % str(e)[:120]})
            return
        if own is not None:
            try:
                own.close()
            except Exception:
                pass
        # 脚本前面可能打印别的东西，只认最后一行完整 JSON
        j = None
        for ln in reversed([x.strip() for x in raw.splitlines()]):
            if not ln:
                continue
            try:
                j = json.loads(ln)
                break
            except Exception:
                continue
        if j is None:
            log('[glog] 车端无有效输出，原始前 200 字符: %s' % raw[:200])
            self._send(200, {'ok': False,
                             'err': '车端没有返回结果（python3 不可用？）'})
            return
        j['t0'] = t0
        j['t1'] = t1
        log('[glog] %s ~ %s -> 命中 %s 行 / 扫 %s 个文件'
            % (time.strftime('%m-%d %H:%M:%S', time.localtime(t0)),
               time.strftime('%H:%M:%S', time.localtime(t1)),
               j.get('nline'), j.get('nfile')))
        self._send(200, j)

    def api_syslog(self, b):
        """系统日志提取：dmesg + 串口/USB 断连 + system 目录下同期日志。

        和 glog 分成两个接口，是因为这三类不是 glog：行内时间戳格式各不相同
        （ISO / syslog / 内核 dmesg -T），硬塞进 glog 那套「按文件名圈候选」
        的逻辑里只会互相污染，排查时也更习惯分开看。
        """
        try:
            t = float(b.get('t') or 0)
            win = float(b.get('win') or 0)
        except Exception:
            self._send(400, {'ok': False, 'err': '时间或窗口不是数字'})
            return
        if t <= 0:
            self._send(400, {'ok': False, 'err': '请先填基准时间'})
            return
        if win <= 0:
            self._send(400, {'ok': False, 'err': '时间窗必须大于 0'})
            return
        ssh, own = _ssh_for_job()
        if ssh is None:
            self._send(200, {'ok': False, 'err': '未连接机器，请先连接'})
            return
        t0 = t - win
        t1 = t + win
        cmd = ('python3 -u -c %s %s %.3f %.3f %d %d %d'
               % (shell_quote(SYSLOG_PY), shell_quote(SYS_DIR),
                  t0, t1, SYS_CAP, SYS_MAX_FILE, SYS_FALLBACK_SCAN))
        try:
            _, o, _e = ssh.exec_command(cmd, timeout=SYS_TIMEOUT)
            try:
                o.channel.settimeout(SYS_TIMEOUT)
            except Exception:
                pass
            raw = o.read().decode('utf-8', 'replace')
        except Exception as e:
            if own is not None:
                try:
                    own.close()
                except Exception:
                    pass
            self._send(200, {'ok': False, 'err': '车端执行失败: %s' % str(e)[:120]})
            return
        if own is not None:
            try:
                own.close()
            except Exception:
                pass
        j = None
        for ln in reversed([x.strip() for x in raw.splitlines()]):
            if not ln:
                continue
            try:
                j = json.loads(ln)
                break
            except Exception:
                continue
        if j is None:
            log('[syslog] 车端无有效输出，原始前 200 字符: %s' % raw[:200])
            self._send(200, {'ok': False,
                             'err': '车端没有返回结果（python3 不可用？）'})
            return
        log('[syslog] %s ~ %s -> dmesg %s / system %s / 串口 %s'
            % (time.strftime('%m-%d %H:%M:%S', time.localtime(t0)),
               time.strftime('%H:%M:%S', time.localtime(t1)),
               len(j.get('dmesg') or []), len(j.get('system') or []),
               len(j.get('serial') or [])))
        self._send(200, j)

    def api_connect(self, b):
        global SESS
        host = (b.get('host') or '').strip()
        pwd = (b.get('pwd') or '').strip()
        if not pwd:
            pwd = _default_pwd()          # 管理员预置的，使用者不用输
        user = (b.get('user') or 'aoting').strip()
        try:
            port = int(b.get('port') or 0)
        except Exception:
            port = 0
        if not host or not port:
            self._send(400, {'ok': False, 'err': '请填写主机和端口'})
            return
        disconnect_current()
        log('──── 连接请求 ────')
        log('  目标 : %s@%s:%d' % (user, host, port))
        try:
            AGENT = load_agent()
        except Exception as e:
            self._send(500, {'ok': False, 'err': str(e)})
            return
        globals()['AGENT_SRC'] = AGENT
        # 记下来：glog 提取这类作业在采集会话断开后也要能用
        globals()['LAST_CONN'] = {'host': host, 'port': port,
                                  'user': user, 'pwd': pwd}
        s = StatSession(host, port, user, pwd)
        with SESS_LOCK:
            SESS = s
        # 等首帧，最多 8 秒；等不到也先返回，页面会继续轮询
        t_end = time.time() + 8
        while time.time() < t_end:
            if s.latest:
                break
            if s.err:
                break
            if not s.running:
                break
            time.sleep(0.2)
        if s.err:
            self._send(200, {'ok': False, 'err': s.err,
                             'needPwd': bool(getattr(s, 'need_pwd', False))})
        else:
            self._send(200, {'ok': True, 'got': bool(s.latest)})


class ThreadedHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global AGENT_SRC
    try:
        AGENT_SRC = load_agent()
    except Exception as e:
        sys.stderr.write('[ERROR] %s\n' % e)
        return 1
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    srv = ThreadedHTTP(('127.0.0.1', port), Handler)
    log('服务已启动: http://127.0.0.1:%d' % port)
    log('单次监控上限: %.0f 秒（到点自动断开，可再点连接）' % MAX_SESSION_SEC)
    sys.stderr.flush()
    # 页面全部关闭后释放远程资源，本地服务继续等待下次打开。
    threading.Thread(target=page_watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        disconnect_current()
    return 0


if __name__ == '__main__':
    sys.exit(main())
