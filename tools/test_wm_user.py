#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
水印账号端到端测试

    python3 tools/test_wm_user.py

测的是 v1.0.17 修的那个问题：
    水印里只显示"时间 + 未登录"，明明是用账号登录的。

    根因：服务端 /api/gate_state 一直有返回 user，但前端的 WM_USER
    只有一行声明、从来没有赋值语句 —— 于是所有人水印都是"未登录"，
    水印等于白做（而绘制逻辑的测试全绿，因为它测的是"传什么画什么"）。

    修复：server.py 在返回 HTML 时把账号烧进 window.__WM_USER__，
    前端以它为初值；checkGate 再兜底同步一次。

真起 camweb 进程 + 假 git 服务，验证两条进入路径：
    1) 页面直接登录（账号密码）
    2) 门户交接（?tk= + CAMWEB_USER）
以及安全性：
    3) 未登录 / 错误 tk 不得注入账号
"""
import os
import sys
import json
import time
import shutil
import hashlib
import secrets
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
import http.server
import socketserver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = []
FAIL = []


def chk(name, ok, note=''):
    (PASS if ok else FAIL).append(name)
    print('  %s %s %s' % ('ok  ' if ok else 'FAIL', name, note))


TMP = tempfile.mkdtemp(prefix='aoting_wm_')
SRC = os.path.join(TMP, 'repo')
shutil.copytree(ROOT, SRC,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))


def mk_gate(users):
    codes = {}
    for u, p in users.items():
        salt = secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac('sha256', p.encode(), bytes.fromhex(salt),
                                 200000)
        codes[u] = {'salt': salt, 'hash': dk.hex()}
    return {'version': 2, 'algo': 'pbkdf2-sha256', 'iterations': 200000,
            'updated': '2026-09-11', 'codes': codes}


GATE = mk_gate({'zhangsan': 'Km7vR2nQ'})


class FakeGit(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps(GATE).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


gsrv = socketserver.TCPServer(('127.0.0.1', 0), FakeGit)
GPORT = gsrv.server_address[1]
threading.Thread(target=gsrv.serve_forever, daemon=True).start()

# 把副本指向假 git，避免 touching 真实仓库
_pj = os.path.join(SRC, 'portal.json')
_cfg = json.load(open(_pj, encoding='utf-8'))
_cfg.setdefault('gate', {})['api'] = 'http://127.0.0.1:%d/gate.json' % GPORT
_cfg['gate']['enabled'] = True
json.dump(_cfg, open(_pj, 'w', encoding='utf-8'), ensure_ascii=False)
open(os.path.join(SRC, 'tools', 'gate_token.txt'), 'w').write('faketoken123456')

PORT = 8811


class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedir)


def fetch(path, cookie=None, base=None):
    base = base or ('http://127.0.0.1:%d' % PORT)
    r = urllib.request.Request(base + path)
    if cookie:
        r.add_header('Cookie', cookie)
    try:
        z = OPENER.open(r, timeout=30)
        return z.status, dict(z.headers), z.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), (e.read() if e.fp else b'')


def start(**extra):
    env = dict(os.environ)
    env['PYTHONPATH'] = SRC
    env.update(extra)
    p = subprocess.Popen([sys.executable, 'server.py', str(PORT)],
                         env=env, cwd=os.path.join(SRC, 'apps', 'camweb'),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(3)
    return p


def main():
    print('── A. 页面直接登录 ──')
    p = start()
    try:
        st, hd, b0 = fetch('/api/gate_state')
        j0 = json.loads(b0.decode() or '{}')
        chk('未登录时 gate_state 无 user',
            st == 200 and not (j0.get('user') or '').strip())

        r = urllib.request.Request('http://127.0.0.1:%d/api/gate_login' % PORT,
                                   data=json.dumps({'user': 'zhangsan',
                                                    'pwd': 'Km7vR2nQ'}).encode(),
                                   headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(r, timeout=60) as z:
                j = json.loads(z.read().decode())
                ck = z.headers.get('Set-Cookie')
            ok_login = bool(j.get('ok')) and j.get('user') == 'zhangsan'
        except Exception as e:
            ok_login, ck = False, None
            j = {'err': str(e)}
        chk('账号密码登录成功', ok_login, str(j)[:60])

        cookie = (ck or '').split(';')[0]
        _, _, html = fetch('/', cookie=cookie)
        inj = ('<script>window.__WM_USER__="zhangsan";</script>'
               .encode() in html)
        chk('登录后 HTML 注入账号', inj)

        _, _, b2 = fetch('/api/gate_state', cookie=cookie)
        chk('gate_state 返回账号',
            '"user": "zhangsan"' in b2.decode().replace("'", '"')
            or 'zhangsan' in b2.decode())

        _, _, h0 = fetch('/')
        chk('未登录页面不注入账号',
            b'<script>window.__WM_USER__=' not in h0)
    finally:
        p.terminate()

    time.sleep(1)
    print('── B. 门户交接（?tk= + CAMWEB_USER）──')
    p2 = start(AOTING_GATE_TK='TK1234567890', CAMWEB_USER='zhangsan')
    try:
        st, hd, _ = fetch('/?tk=TK1234567890')
        chk('正确 tk 触发 302', st == 302)
        ck = hd.get('Set-Cookie')
        chk('交接下发会话 cookie', bool(ck))
        cookie = (ck or '').split(';')[0]
        _, _, html = fetch('/', cookie=cookie)
        chk('交接后 HTML 注入账号',
            b'<script>window.__WM_USER__="zhangsan";</script>' in html)
        st5, hd5, b5 = fetch('/?tk=WRONG')
        chk('错误 tk 不下发 cookie', hd5.get('Set-Cookie') is None)
        chk('错误 tk 页面不注入账号',
            b'<script>window.__WM_USER__=' not in b5)
    finally:
        p2.terminate()

    print('── C. 前端取值链 ──')
    src = open(os.path.join(ROOT, 'apps', 'camweb', 'index.html'),
               encoding='utf-8').read()
    chk('WM_USER 以服务端注入为初值',
        "window.__WM_USER__" in src and 'let WM_USER =' in src)
    chk('WM_USER 对无 window 环境有保护（node 测试会 eval 这段）',
        "typeof window !== 'undefined'" in src)
    chk('checkGate 兜底同步账号',
        'if (r && r.user) WM_USER =' in src)

    print('')
    print('通过 %d，失败 %d' % (len(PASS), len(FAIL)))
    if FAIL:
        print('失败项：' + ', '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
