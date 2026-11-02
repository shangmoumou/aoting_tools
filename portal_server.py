#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门户静态服务 —— 与 `python -m http.server` 的唯一区别：
【关掉浏览器页面后，它会自动退出，黑窗口跟着关闭。】

为什么不能直接用 http.server：
    它只是个静态服务器，页面关没关它完全不知道，所以会一直挂着。

机制（两步）
────────────────────────────────────────────────────────
  心跳  POST /__portal_ping   页面每 4 秒发一次，证明"我还活着"
  告别  POST /__portal_bye    页面卸载时发，请求"我走了"

收到告别后等 GRACE 秒，期间若没有新心跳 -> 退出。

为什么必须两步，不能只用告别：
    点进 bag 播放器时，门户页面也会卸载并发出告别。
    如果收到告别就立即退出，播放器还没加载完就 404 了。
    留一个宽限期，让新页面有机会接上心跳、取消退出。

为什么宽限 5 秒、兜底 120 秒：
    浏览器会把后台标签页的 setInterval 节流到 60 秒一次。
    兜底超时若小于 60 秒，你把门户切到后台干活时会被误杀。
    120 秒 > 60 秒，安全。且只在收到过至少一次心跳后才启用兜底，
    否则"双击 bat 后走开"也会被关掉。
"""
import os
import sys
import json
import time
import threading
import http.server
import socketserver
import subprocess
import urllib.request
import urllib.error
import hashlib
import hmac
import ssl
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools'))
from gate import Gate          # 与 camweb 共用同一份门禁实现

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = Gate(HERE, cache_tag='gate')
PORT = int(os.environ.get('PORTAL_PORT', '8899'))
GRACE = 20.0         # 收到告别后的宽限
# v1.0.31：不再按"空闲超时"自动退出（见 watchdog 处的说明）。
# 门户只是个静态服务，常驻几乎不占资源；误杀的代价是页面还在
# （浏览器离线缓存）而点卡片一律 failed to fetch，用户完全无从判断。
# 现在只在【页面发来告别】时退出。
#
# 各子工具自己的超时不受影响 —— 例如状态监控单次会话 30 分钟自动断开，
# 那是工具内部的限制，跟门户无关。

_bye_at = 0.0             # 收到告别的时刻（唯一会触发自动退出的条件）
_current_gate_user = {}   # 最近一次解锁成功的账号（用于写到车端日志）
_lock = threading.Lock()
_shutdown = threading.Event()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *a):
        pass   # 太吵

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def do_GET(self):
        # 前端用它判断"门户后台还在不在"。为什么不能用 version.json：
        # 那个被 Service Worker 缓存了，门户退出后照样返回 200，
        # 会把"服务已死"误判成"活着"，诊断方向全错。
        if self.path.split('?')[0] == '/__portal_ping':
            return self._json({'ok': True})
        super().do_GET()

    def do_HEAD(self):
        super().do_HEAD()

    def do_POST(self):
        global _bye_at
        p = self.path.split('?')[0]

        if p == '/__portal_ping':
            # 还有页面在（或新页面刚接上）—— 取消退出倒计时
            with _lock:
                _bye_at = 0.0
            return self._json({'ok': True})

        if p == '/__portal_bye':
            with _lock:
                _bye_at = time.time()
            return self._json({'ok': True})

        if p == '/api/launch':
            ln = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(ln).decode('utf-8', 'replace') if ln else ''
            j = {}
            try:
                j = json.loads(body or '{}')
            except Exception:
                j = {}
            # 解锁账号带下去，写进车端登录日志（谁连的哪台车，事后可查）
            u = (j.get('gateUser') or '').strip()
            if u:
                os.environ['CAMWEB_USER'] = u
                _current_gate_user['u'] = u
            obj, code = handle_launch(body)
            return self._json(obj, code)

        if p == '/api/gate_check':
            ln = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(ln).decode('utf-8', 'replace') if ln else ''
            obj, code = handle_gate_check(body)
            return self._json(obj, code)

        return self._json({'ok': False, 'err': 'unknown'}, 404)

    def end_headers(self):
        # HTML 与版本相关资源一律禁缓存：
        # 否则改了代码刷新看不到，页面显示的版本号也是旧的，
        # 极难判断到底跑的是哪一份。
        try:
            path = self.path.split('?')[0]
            if path.endswith(('.html', '.json')) or path in ('/', '/sw.js'):
                self.send_header('Cache-Control',
                                 'no-store, no-cache, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Expires', '0')
        except Exception:
            pass
        super().end_headers()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ── 拉起本地服务型工具（如 camweb）──────────────────────
# 浏览器不能启动本地程序，但门户后端就跑在本机，可以代劳。
# 安全约束（不能省）：
#   1. 只能启动 apps/<id>/ 下 app.json 里【已声明】的 start 文件，
#      不接受任何来自请求的路径参数
#   2. 路径 realpath 必须仍在工具箱目录内
#   3. 只认 .bat/.cmd/.py/.exe，防止把数据文件当程序跑
_launched = {}          # id -> 启动时刻，用于避免重复拉起
ALLOWED_EXT = ('.bat', '.cmd', '.py', '.exe')


def _app_dir(app_id):
    """校验 app 目录合法，返回绝对路径；不合法返回 None。"""
    if not app_id or not isinstance(app_id, str):
        return None
    # 不接受路径分隔符，杜绝 ../ 穿越
    if '/' in app_id or '\\' in app_id or app_id.startswith('.'):
        return None
    d = os.path.realpath(os.path.join(HERE, 'apps', app_id))
    root = os.path.realpath(HERE)
    if d != root and not d.startswith(root + os.sep):
        return None
    return d if os.path.isdir(d) else None


def _declared_start(app_id, name):
    """返回 app.json 声明的启动脚本绝对路径，未声明则 None。"""
    d = _app_dir(app_id)
    if not d:
        return None
    cfg = os.path.join(d, 'app.json')
    if not os.path.isfile(cfg):
        return None
    try:
        with open(cfg, 'r', encoding='utf-8') as f:
            j = json.load(f)
    except Exception:
        return None
    svc = j.get('service') or {}
    want = (svc.get('start') or '').strip()
    if not want or os.path.basename(want) != want:
        return None
    if not want.lower().endswith(ALLOWED_EXT):
        return None
    if want != name:
        return None
    target = os.path.realpath(os.path.join(d, want))
    if not target.startswith(os.path.realpath(d) + os.sep):
        return None
    return target if os.path.isfile(target) else None


def launch_service(app_id, name, handoff=''):
    """拉起本地服务。返回 (ok, msg)。"""
    target = _declared_start(app_id, name)
    if not target:
        return False, '该工具没有声明可启动的本地服务'
    d = os.path.dirname(target)

    # 防止重复拉起：20 秒内点多次只起一次。
    # 否则每次点击都开一个黑窗口，paramiko 安装也可能跑好几遍。
    now = time.time()
    if now - _launched.get(app_id, 0) < 20:
        return True, 'already'

    env = os.environ.copy()
    # 告诉 start.bat 不要自己弹浏览器，由门户探测到就绪后再打开
    env['CAMWEB_NO_BROWSER'] = '1'
    env['PORTAL_NO_BROWSER'] = '1'
    # 解锁账号传下去（start.bat -> server.py -> 车端登录日志）
    gu = (_current_gate_user.get('u') or '').strip()
    if gu:
        env['CAMWEB_USER'] = gu
    # 交接令牌：本地服务启动后只认这一个值。从门户进来的人带着它，
    # 不用登录第二次；直接双击 start.bat 的人拿不到，必须登录。
    if handoff:
        env['AOTING_GATE_TK'] = handoff

    try:
        if os.name == 'nt':
            # 后台启动批处理，输出落盘，既不弹控制台也保留启动失败原因。
            cmd = ['cmd', '/d', '/c', target]
            flags = subprocess.CREATE_NO_WINDOW
        else:
            cmd = ['bash', target]
            flags = 0
        if os.name == 'nt':
            log_dir = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
                                   'aoting_tools')
            # makedirs 创建用户日志目录；关闭父进程句柄不影响子进程持续写入。
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, app_id + '.log'), 'ab') as output:
                subprocess.Popen(cmd, cwd=d, env=env, creationflags=flags,
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=output)
        else:
            subprocess.Popen(cmd, cwd=d, env=env, creationflags=flags)
        _launched[app_id] = now
        return True, 'started'
    except Exception as e:
        return False, str(e)


def handle_launch(body):
    """POST /api/launch  {"app":"camweb","start":"start.bat"}"""
    try:
        j = json.loads(body or '{}')
    except Exception:
        return {'ok': False, 'err': 'bad json'}, 400
    app_id = j.get('app', '')
    name = j.get('start', 'start.bat')
    if not isinstance(app_id, str) or not isinstance(name, str):
        return {'ok': False, 'err': 'bad param'}, 400
    # 门户解锁成功时下发的一次性令牌。必须是门户自己签发的，
    # 否则任何人都能捏造一个 tk 让 camweb 免登录。
    tk = (j.get('tk') or '').strip()
    # 不认识就当没带（不做硬性拒绝）：最坏结果是 camweb 要求登录一次，
    # 总比"点了没反应还报启动失败"好。
    if tk and tk not in _handoff:
        tk = ''
    ok, msg = launch_service(app_id, name, handoff=tk)
    return ({'ok': True, 'status': msg} if ok
            else {'ok': False, 'err': msg}), 200 if ok else 400


# ─────────────────────────────────────────────────────────────
# 门禁（gate）
# ─────────────────────────────────────────────────────────────
# 实现在 tools/gate.py，与 camweb 共用 —— 门户和相机服务必须校验同一套
# 账号，否则"直接双击 camweb/start.bat"就能绕过门禁（这正是 v1.0.15 修的洞）。

# 一次性交接令牌：门户解锁成功后生成，通过环境变量传给 camweb。
# camweb 启动后只认这一个值，浏览器带 ?tk= 来访即可换取会话，
# 这样"从门户进"不用登录第二次；直接开 8765 的人没有 tk，必须登录。
_handoff = {}          # tk -> 生成时刻


def _new_handoff():
    import secrets
    tk = secrets.token_urlsafe(32)
    _handoff[tk] = time.time()
    # 只保留 5 分钟内的，避免无限堆积
    for k in list(_handoff):
        if time.time() - _handoff[k] > 300:
            _handoff.pop(k, None)
    return tk


def handle_gate_check(body):
    """POST /api/gate_check  {"user":"zhangsan","pwd":"..."}"""
    try:
        j = json.loads(body or '{}')
    except Exception:
        return {'ok': False, 'err': 'bad json'}, 400
    user = (j.get('user') or '').strip()
    pwd = j.get('pwd') or ''
    r = GATE.check(user, pwd)
    if r.get('ok') and j.get('wantHandoff'):
        r['tk'] = _new_handoff()
    return r, 200


def watchdog(httpd):
    """只在【页面发来告别】时关闭监听，让主线程退出。

    曾经还有一条"空闲 N 秒自动退出"，v1.0.31 去掉了。原因：
      1) 浏览器会把后台标签页的定时器节流到 60 秒一次，用户切去别的
         标签页干活、或合盖休眠再唤醒，心跳就会迟到一次 —— 服务悄悄
         退出，而页面还在（浏览器离线缓存），点卡片只会得到
         failed to fetch，用户想不到是门户没了。
      2) 门户只是静态服务，常驻几乎不占资源，误杀的代价远大于残留。
    去掉之后唯一的代价是：浏览器崩溃时黑窗口不会自动关，
    手动关掉即可 —— 这比误杀好排查得多。
    """
    while not _shutdown.is_set():
        time.sleep(1.0)
        with _lock:
            bye = _bye_at
        if bye > 0 and (time.time() - bye) >= GRACE:
            log('页面已全部关闭，服务自动退出')
            break

    try:
        httpd.shutdown()
    except Exception:
        pass


def log(msg):
    sys.stdout.write('[portal] %s\n' % msg)
    sys.stdout.flush()


def main():
    try:
        httpd = Server(('0.0.0.0', PORT), Handler)
    except OSError as e:
        if 'Address already in use' in str(e) or e.errno in (98, 10048):
            sys.stdout.write(
                '\n[ERROR] 端口 %d 已被占用\n'
                '        多半是之前启动的门户服务还在运行。\n'
                '        请关闭那个黑窗口后重试。\n\n' % PORT)
            sys.stdout.flush()
            sys.exit(1)
        raise

    log('服务已启动: http://127.0.0.1:%d' % PORT)
    log('关掉浏览器页面后本窗口会自动关闭')

    # 启动自检：令牌找没找到，黑窗口里直接说，省得等到点相机才发现
    try:
        _cfg = gate_cfg()
        if _cfg.get('enabled', True):
            _t = gate_token(_cfg)
            if _t:
                log('账号校验: 已载入访问令牌 (%s... 共%d位)' % (_t[:6], len(_t)))
            else:
                log('账号校验: [未找到令牌] %s' % gate_token_diag(_cfg))
        else:
            log('账号校验: 已关闭')
    except Exception:
        pass

    threading.Thread(target=watchdog, args=(httpd,), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log('收到 Ctrl+C，退出')
    finally:
        _shutdown.set()
        try:
            httpd.server_close()
        except Exception:
            pass
    sys.exit(0)      # 正常退出 -> bat 不 pause，窗口直接关


if __name__ == '__main__':
    main()
