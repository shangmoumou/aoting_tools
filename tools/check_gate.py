#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门禁自检 —— 解压后先跑这个，别等到点相机才发现连不上。

    python tools/check_gate.py

它按真实顺序检查 5 件事，任何一步失败都直接告诉你怎么修：

  1. 令牌文件在不在（Windows 隐藏扩展名会让文件实际叫 .txt.txt）
  2. 令牌读出来对不对（BOM / UTF-16 是 Windows 上的头号杀手）
  3. 能不能连上公司 git
  4. 拿到的 gate.json 格式对不对
  5. 账号密码能不能通过校验

纯标准库，不需要装任何东西。
"""
import os
import sys
import json
import ssl
import hmac
import hashlib
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TIMEOUT = 15


def ok(m):
    print('  [OK]   %s' % m)


def bad(m):
    print('  [FAIL] %s' % m)


def warn(m):
    print('  [注意] %s' % m)


def clean(s):
    if not s:
        return ''
    out = []
    for ch in s:
        o = ord(ch)
        if ch.isspace() or o < 32 or o == 127 or ch == '﻿':
            continue
        out.append(ch)
    return ''.join(out).strip().strip('"').strip("'").strip()


def looks(t):
    if len(t) < 8:
        return False
    for ch in t:
        if ord(ch) > 126 or ch == '�':
            return False
    return True


def read_token(p):
    for enc in ('utf-8-sig', 'utf-16', 'utf-8', 'gbk', 'latin-1'):
        try:
            with open(p, 'r', encoding=enc) as f:
                v = clean(f.read())
        except Exception:
            continue
        if looks(v):
            return v, enc
    return '', None


def main():
    print('=' * 64)
    print(' 门禁自检')
    print('=' * 64)
    print(' 工具箱目录: %s' % ROOT)

    # ── 0. 读配置 ────────────────────────────────────────────
    cfg = {}
    pj = os.path.join(ROOT, 'portal.json')
    try:
        with open(pj, 'r', encoding='utf-8') as f:
            cfg = (json.load(f).get('gate') or {})
        ok('读到 portal.json 的 gate 配置')
    except Exception as e:
        bad('读 portal.json 失败: %r' % e)
        return 2
    if not cfg.get('enabled', True):
        warn('gate.enabled=false，门禁未启用')
        return 0
    api = (cfg.get('api') or '').strip()
    if not api:
        bad('portal.json 里没有 gate.api')
        return 2

    # ── 1. 令牌文件在不在 ────────────────────────────────────
    print('\n[1/5] 令牌文件')
    rel = (cfg.get('tokenFile') or 'tools/gate_token.txt').strip()
    cands = []
    for r in (rel, 'tools/gate_token.txt', 'gate_token.txt'):
        p = os.path.normpath(os.path.join(ROOT, r))
        if p not in cands:
            cands.append(p)
    tools = os.path.join(ROOT, 'tools')
    if os.path.isdir(tools):
        hit = [n for n in sorted(os.listdir(tools)) if 'token' in n.lower()]
        print('       tools/ 下含 token 的文件: %s' % (', '.join(hit) if hit else '(无)'))
        for n in hit:
            low = n.lower()
            # 示例文件不算异常，别吓唬人
            if low == 'gate_token.txt' or 'example' in low or low.endswith('.bak'):
                continue
            warn('"%s" 不是期望的文件名 —— 多半是 Windows「隐藏已知扩展名」导致'
                 '实际叫 gate_token.txt.txt，请重命名（去掉多余的 .txt）' % n)

    token = ''
    src = ''
    env = clean(os.environ.get('AOTING_GATE_TOKEN') or '')
    if env:
        token, src = env, '环境变量 AOTING_GATE_TOKEN'
        ok('从环境变量读到令牌')
    else:
        for p in cands:
            if not os.path.exists(p):
                print('       [缺失] %s' % p)
                continue
            v, enc = read_token(p)
            if v:
                token, src = v, p
                ok('读到令牌: %s... 共%d位（编码 %s）' % (v[:6], len(v), enc))
                break
            bad('%s 存在但内容读不出来（编码异常），请用记事本另存为 UTF-8' % p)
    if not token:
        bad('没找到令牌。请把令牌写进:\n         %s\n'
            '         一行，不要引号、不要空格。' % os.path.join(ROOT, 'tools', 'gate_token.txt'))
        return 2

    # ── 2. 连 git ────────────────────────────────────────────
    print('\n[2/5] 连接公司 git')
    req = urllib.request.Request(api)
    req.add_header('PRIVATE-TOKEN', token)
    req.add_header('User-Agent', 'aoting-tools-check')
    raw = None
    for verify in (True, False):
        try:
            ctx = None
            if not verify:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                raw = r.read().decode('utf-8', 'replace')
            ok('HTTP 200%s' % ('' if verify else '（已忽略证书）'))
            break
        except urllib.error.HTTPError as e:
            bad('HTTP %s —— 多半是令牌权限不够（Role 选 Reporter）或已撤销' % e.code)
            return 2
        except Exception as e:
            if verify:
                print('       SSL 校验失败，重试（内网自签证书常见）: %r' % e)
                continue
            bad('连不上: %r —— 检查网络 / 是否要连 VPN' % e)
            return 2
    if raw is None:
        return 2

    # ── 3. 内容是不是 json ───────────────────────────────────
    print('\n[3/5] gate.json 内容')
    try:
        g = json.loads(raw)
    except Exception:
        bad('返回的不是 JSON（多半是 GitLab 登录页）—— 令牌无效或路径不对。'
            '前 80 字符: %s' % raw[:80])
        return 2
    ok('JSON 有效, algo=%s' % g.get('algo'))
    codes = g.get('codes') or {}
    print('       账号数 = %d' % len(codes))
    for k in sorted(codes):
        print('         - %s' % k)
    if not codes:
        warn('账号库是空的，任何人都解锁不了')
    algo = (g.get('algo') or '').lower()
    if 'pbkdf2' not in algo:
        warn('gate.json 是旧格式(%s)，工具箱只支持 pbkdf2-sha256 —— '
             '请用 tools/make_gate.py 重建' % (g.get('algo') or '?'))

    # ── 4. 校验一个账号 ──────────────────────────────────────
    print('\n[4/5] 账号校验（可跳过，直接回车）')
    try:
        u = input('       账号: ').strip()
    except EOFError:
        u = ''
    if u:
        try:
            import getpass
            pw = getpass.getpass('       密码: ')
        except Exception:
            pw = input('       密码: ')
        rec = codes.get(u)
        if not isinstance(rec, dict):
            bad('账号 "%s" 不在库里' % u)
            return 1
        if 'pbkdf2' not in algo:
            bad('库是旧格式，无法校验')
            return 1
        iters = int(g.get('iterations') or 200000)
        try:
            dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'),
                                     bytes.fromhex(rec.get('salt') or ''), iters)
        except Exception as e:
            bad('salt 格式不对: %r' % e)
            return 1
        if hmac.compare_digest(dk.hex().lower(), (rec.get('hash') or '').lower()):
            ok('账号密码正确')
        else:
            bad('密码不正确')
            return 1
    else:
        print('       (已跳过)')

    print('\n[5/5] 结论')
    ok('门禁链路正常 —— 工具箱可以打包外发')
    print('\n 提示: 令牌来自 %s' % src)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n(已取消)')
        sys.exit(130)
