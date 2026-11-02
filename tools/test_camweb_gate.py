#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
camweb 门禁端到端测试

    python3 tools/test_camweb_gate.py

测的是 v1.0.15 修的那个洞：
    门禁原本只做在门户页面上（界面层的锁）。直接双击
    apps/camweb/start.bat，服务起在 8765，浏览器直接开 8765
    就能连车取图 —— 门户那道锁形同虚设。

    原则：能力在哪一层，锁就得在哪一层。所以相机服务自己必须校验。

真起一个 camweb 进程 + 一个假 git 服务，用真实 HTTP 请求验证：
  1. 探测接口保持开放（否则门户永远以为服务没起）
  2. 未登录访问业务接口 -> 401
  3. 直接开 8765（没有 tk）-> 必须登录
  4. 带 ?tk= 交接 -> 发 cookie，之后免登录
  5. 密码错 / 对
  6. 单独拷出来的 camweb（无 tools/gate.py）-> 一律拒绝，不放行
"""
import os
import sys
import json
import time
import shutil
import hashlib
import hmac
import secrets
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
import http.server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = []
FAIL = []


def chk(name, ok, note=''):
    (PASS if ok else FAIL).append(name)
    print('  %s %s %s' % ('ok  ' if ok else 'FAIL', name, note))


TMP = tempfile.mkdtemp(prefix='aoting_gate_test_')


# ── 造一个账号库（与 make_gate.py 同格式） ─────────────────
def mk_gate(users):
    codes = {}
    for u, p in users.items():
        salt = secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac('sha256', p.encode(), bytes.fromhex(salt), 200000)
        codes[u] = {'salt': salt, 'hash': dk.hex()}
    return {'version': 2, 'algo': 'pbkdf2-sha256', 'iterations': 200000,
            'updated': '2026-09-11', 'codes': codes}


GATE_JSON = json.dumps(mk_gate({'zhangsan': 'Km7#vR2n', 'lisi': 'q4pEyEsd'}))


class FakeGit(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = GATE_JSON.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def start_fake_git():
    srv = http.server.HTTPServer(('127.0.0.1', 0), FakeGit)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None          # 不跟随 302，否则拿不到 Set-Cookie


def req(url, cookie=None, method='GET', body=None, raw=False, nofollow=False):
    r = urllib.request.Request(url, method=method)
    if cookie:
        r.add_header('Cookie', 'aoting_cam=' + cookie)
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        r.add_header('Content-Type', 'application/json')
    op = urllib.request.build_opener(NoRedirect) if nofollow else None
    try:
        with (op.open(r, data=data, timeout=10) if op
              else urllib.request.urlopen(r, data=data, timeout=10)) as resp:
            if raw:
                return resp
            return resp.status, json.loads(resp.read().decode('utf-8')), \
                resp.headers.get_all('Set-Cookie') or []
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode('utf-8'))
        except Exception:
            j = {}
        return e.code, j, []
    except Exception as e:
        return 0, {'err': str(e)}, []


def req_nofollow(url):
    """不跟随 302，返回 (status, cookie值)。"""
    op = urllib.request.build_opener(NoRedirect)
    try:
        with op.open(urllib.request.Request(url), timeout=10) as r:
            ck = r.headers.get_all('Set-Cookie') or []
            return r.status, _ck(ck)
    except urllib.error.HTTPError as e:
        return e.code, _ck(e.headers.get_all('Set-Cookie') or [])


def _ck(cookies):
    for x in cookies:
        if 'aoting_cam=' in x:
            return x.split('aoting_cam=')[1].split(';')[0]
    return ''


def main():
    git, gport = start_fake_git()

    # 临时改写 portal.json 的 gate.api 指向假 git（测完还原）
    pj = os.path.join(ROOT, 'portal.json')
    orig = open(pj, encoding='utf-8').read()
    cfg = json.loads(orig)
    cfg.setdefault('gate', {})['api'] = \
        'http://127.0.0.1:%d/gate.json' % gport
    cfg['gate']['enabled'] = True
    open(pj, 'w', encoding='utf-8').write(
        json.dumps(cfg, ensure_ascii=False, indent=2))

    # 令牌文件
    tkf = os.path.join(ROOT, 'tools', 'gate_token.txt')
    tk_orig = open(tkf, encoding='utf-8').read() if os.path.exists(tkf) else None
    open(tkf, 'w', encoding='utf-8').write('# test\nglpat-testtoken123456\n')

    PORT = 8799
    env = os.environ.copy()
    env['AOTING_GATE_TK'] = 'BOOTTK-SECRET'
    env['CAMWEB_USER'] = 'zhangsan'
    proc = subprocess.Popen([sys.executable, 'server.py', str(PORT)],
                            cwd=os.path.join(ROOT, 'apps', 'camweb'),
                            env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = 'http://127.0.0.1:%d' % PORT
    try:
        # 等服务起来
        up = False
        for _ in range(60):
            try:
                c, j, _ = req(base + '/api/version')
                if c == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)
        chk('服务能起来', up)

        # 1) 探测接口必须开放，否则门户永远以为服务没起
        c, j, _ = req(base + '/api/version')
        chk('探测接口开放（门户要探测）', c == 200, 'code=%s' % c)

        # 2) 未登录访问业务接口 -> 401
        c, j, _ = req(base + '/api/status')
        chk('未登录取状态被拒', c == 401 and j.get('needLogin'),
            'code=%s' % c)
        c, j, _ = req(base + '/api/connect', method='POST',
                      body={'topic': '/camera_01/color/image_raw'})
        chk('未登录连接相机被拒', c == 401, 'code=%s' % c)

        # 3) 直接开 8765（无 tk）-> 不发证
        c, j, _ = req(base + '/api/gate_state')
        chk('直接打开需要登录', c == 200 and j.get('need') is True,
            'need=%s' % j.get('need'))

        # 4) 错误的 tk 不能换 cookie
        c0, got0 = req_nofollow(base + '/?tk=WRONG')
        chk('错误 tk 不发会话', not got0, 'code=%s' % c0)

        # 5) 正确 tk（门户交接）-> 302 + cookie，之后免登录
        code, got = req_nofollow(base + '/?tk=BOOTTK-SECRET')
        chk('正确 tk 发放会话', bool(got), 'code=%s' % code)
        c, j, _ = req(base + '/api/logs?since=0', cookie=got)
        chk('带会话可访问业务接口', c == 200, 'code=%s' % c)

        # 6) 密码错
        c, j, _ = req(base + '/api/gate_login', method='POST',
                      body={'user': 'zhangsan', 'pwd': 'wrong'})
        chk('密码错被拒', c == 200 and not j.get('ok'), str(j.get('err'))[:30])

        # 7) 密码对 -> 发 cookie
        c, j, cookies = req(base + '/api/gate_login', method='POST',
                            body={'user': 'zhangsan', 'pwd': 'Km7#vR2n'})
        ok_cookie = ''
        for x in cookies:
            if 'aoting_cam=' in x:
                ok_cookie = x.split('aoting_cam=')[1].split(';')[0]
        chk('密码对可登录', bool(j.get('ok')) and bool(ok_cookie),
            'user=%s' % j.get('user'))
        c, j, _ = req(base + '/api/logs?since=0', cookie=ok_cookie)
        chk('登录后可用', c == 200, 'code=%s' % c)

        # 8) 不存在的人
        c, j, _ = req(base + '/api/gate_login', method='POST',
                      body={'user': 'hacker', 'pwd': 'Km7#vR2n'})
        chk('账号库中不存在的人被拒', not j.get('ok'))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        open(pj, 'w', encoding='utf-8').write(orig)
        if tk_orig is None:
            os.remove(tkf)
        else:
            open(tkf, 'w', encoding='utf-8').write(tk_orig)
        git.shutdown()

    # ── 9) 单独拷出来的 camweb：没有 tools/gate.py，必须拒绝 ──
    solo = os.path.join(TMP, 'camweb')
    os.makedirs(solo)
    for f in ('server.py', 'index.html', 'cam_agent.py'):
        src = os.path.join(ROOT, 'apps', 'camweb', f)
        if os.path.exists(src):
            shutil.copy(src, solo)
    env2 = os.environ.copy()
    env2.pop('AOTING_GATE_TK', None)
    p2 = subprocess.Popen([sys.executable, 'server.py', '8798'], cwd=solo,
                          env=env2, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    b2 = 'http://127.0.0.1:8798'
    try:
        up = False
        for _ in range(60):
            try:
                c, j, _ = req(b2 + '/api/version')
                if c == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)
        chk('单独 camweb 能起来', up)
        c, j, _ = req(b2 + '/api/gate_login', method='POST',
                      body={'user': 'zhangsan', 'pwd': 'Km7#vR2n'})
        chk('单独 camweb 一律拒绝（不放行）', c == 403 and not j.get('ok'),
            'code=%s' % c)
    finally:
        p2.terminate()
        try:
            p2.wait(timeout=5)
        except Exception:
            p2.kill()

    print('\n==================================================')
    print('通过 %d / 失败 %d' % (len(PASS), len(FAIL)))
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print('  -', f)
    return 1 if FAIL else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
