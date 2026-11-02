#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gate.py —— 账号校验（门禁）共享模块

【为什么必须是共享模块】
    门禁最初只做在门户（8899）的页面上 —— 那是【界面层】的锁：
    apps/camweb/start.bat 可以直接双击运行，服务起在 8765，
    浏览器直接开 8765 就能用，门禁形同虚设。

    真正的原则是：能力在哪一层，锁就得在哪一层。
    相机能力在 camweb（8765），所以 camweb 自己也必须校验。

    门户和 camweb 共用这一份实现，避免两边各写一套之后慢慢跑偏
    （历史上"内置 make_gate.py 是旧格式"就是这样出的事）。

【为什么不校验就放行】
    令牌缺失、连不上服务器、格式不对 —— 一律拒绝（fail closed）。
    宁可让人用不了，也不能让门禁被绕过，否则做了等于没做。
"""
from __future__ import print_function
import os
import re
import sys
import json
import time
import hmac
import ssl
import hashlib
import tempfile
import urllib.request

GATE_TIMEOUT = 8.0     # 联网超时。别让"解锁"这个动作卡住太久


class Gate(object):
    """绑定到某个工具箱根目录的门禁。"""

    def __init__(self, root, cfg_name='portal.json', cache_tag='gate'):
        self.root = os.path.abspath(root)
        self.cfg_path = os.path.join(self.root, cfg_name)
        self.cache = os.path.join(
            tempfile.gettempdir(), 'aoting_%s_cache.json' % cache_tag)

    # ── 配置 ──────────────────────────────────────────────
    def cfg(self):
        try:
            with open(self.cfg_path, 'r', encoding='utf-8') as f:
                j = json.load(f)
            g = j.get('gate') or {}
            return g if isinstance(g, dict) else {}
        except Exception:
            return {}

    def enabled(self):
        c = self.cfg()
        return bool(c.get('enabled', True))

    # ── 令牌 ──────────────────────────────────────────────
    @staticmethod
    def _clean_token(s):
        """清洗令牌：去 BOM、去所有空白/不可见字符、去包裹引号。

        必须逐字符过滤而不是 strip()：
          · U+FEFF(BOM) 不是空白，strip() 去不掉，会原样拼进 HTTP 头导致 403；
          · UTF-16 存出来的文件用 utf-8 读会得到 \\x00 夹杂的乱码。
        """
        if not s:
            return ''
        out = []
        for ch in s:
            o = ord(ch)
            if ch.isspace() or o < 32 or o == 127 or ch == '﻿':
                continue
            out.append(ch)
        return ''.join(out).strip().strip('"').strip("'").strip()

    @staticmethod
    def _looks_like_token(t):
        """排除"解码成功但是乱码"的情况（如 utf-8 去读 utf-16 文件）。"""
        if len(t) < 8:
            return False
        for ch in t:
            if ord(ch) > 126 or ch == '�':
                return False
        return True

    @classmethod
    def _pick_token(cls, text):
        """逐行找令牌：跳过空行、# 注释，容忍 token=xxx 写法。

        为什么不能 f.read() 一把梭：拿到包的人要自己改这个值，很可能顺手
        在文件里写个备注。整串内容被当成令牌拼进 HTTP 头之后，服务器回的
        是 403 —— 而 403 看起来像"权限/密码有问题"，几乎没人会往
        "文件里多了几个字"这个方向想。
        """
        for line in (text or '').splitlines():
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            if '=' in s:
                s = s.split('=', 1)[1].strip()
            t = cls._clean_token(s)
            if cls._looks_like_token(t):
                return t
        return ''

    @classmethod
    def _read_token_file(cls, p):
        """多编码尝试。Windows 记事本/PowerShell 重定向常写出 BOM 或 UTF-16。"""
        for enc in ('utf-8-sig', 'utf-16', 'utf-8', 'gbk', 'latin-1'):
            try:
                with open(p, 'r', encoding=enc) as f:
                    v = cls._pick_token(f.read())
            except Exception:
                continue
            if cls._looks_like_token(v):
                return v
        return ''

    def _token_paths(self):
        cfg = self.cfg()
        rel = (cfg.get('tokenFile') or 'tools/gate_token.txt').strip()
        cands = []
        for r in (rel, 'tools/gate_token.txt', 'gate_token.txt'):
            p = os.path.normpath(os.path.join(self.root, r))
            if p not in cands:
                cands.append(p)
        return cands

    def token(self, diag=None):
        """令牌来源：环境变量优先，其次 tools/ 下的文件。
        这两个来源都由使用者自己提供，不随工具包分发。"""
        t = self._clean_token(os.environ.get('AOTING_GATE_TOKEN') or '')
        if t:
            if diag is not None:
                diag.append('来源: 环境变量 AOTING_GATE_TOKEN')
            return t
        for p in self._token_paths():
            v = self._read_token_file(p)
            if v:
                if diag is not None:
                    diag.append('来源: %s' % p)
                return v
        return ''

    def token_diag(self):
        """排查信息：Windows"隐藏已知扩展名"会让文件实际叫 .txt.txt。"""
        lines = []
        for p in self._token_paths():
            lines.append('%s %s' % ('[存在]' if os.path.exists(p) else '[缺失]', p))
        d = os.path.join(self.root, 'tools')
        if os.path.isdir(d):
            try:
                names = sorted(os.listdir(d))
            except Exception:
                names = []
            hit = [n for n in names if 'token' in n.lower()]
            lines.append('tools/ 下含 token 的文件: %s'
                         % (', '.join(hit) if hit else '(无)'))
        else:
            lines.append('tools/ 目录不存在: %s' % d)
        return ' ｜ '.join(lines)

    # ── 联网取账号库 ──────────────────────────────────────
    def _http_get(self, url, token, verify=True):
        req = urllib.request.Request(url)
        # 实测：这个 GitLab 只认 PRIVATE-TOKEN；raw 直链会返回 200 但
        # 内容是登录页 HTML，必须能识别出来（见 fetch 里的 json 校验）。
        req.add_header('PRIVATE-TOKEN', token)
        req.add_header('User-Agent', 'aoting-tools')
        ctx = None
        if not verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=GATE_TIMEOUT, context=ctx) as r:
            return r.read().decode('utf-8', 'replace')

    def fetch(self, token):
        """取 gate.json，返回 (data, err)。"""
        cfg = self.cfg()
        url = (cfg.get('api') or '').strip()
        if not url:
            return None, '未配置 gate.api（请用完整工具箱，不要单独跑 camweb）'
        if not token:
            return None, '未找到访问令牌'
        raw = None
        err = None
        for verify in (True, False):
            try:
                raw = self._http_get(url, token, verify)
                break
            except Exception as e:
                err = str(e)
                raw = None
        if raw is None:
            return None, '无法连接账号服务器：%s' % (err or '未知错误')
        try:
            data = json.loads(raw)
        except Exception:
            return None, '账号服务器返回的不是 JSON（可能是登录页）'
        if not isinstance(data, dict) or not isinstance(data.get('codes'), dict):
            return None, '账号文件格式不对'
        return data, None

    # ── 缓存（仅断网兜底） ────────────────────────────────
    def _cache_write(self, data):
        try:
            with open(self.cache, 'w', encoding='utf-8') as f:
                json.dump({'ts': time.time(), 'data': data}, f)
        except Exception:
            pass

    def _cache_read(self, max_days):
        try:
            with open(self.cache, 'r', encoding='utf-8') as f:
                c = json.load(f)
            if time.time() - float(c.get('ts', 0)) > max_days * 86400.0:
                return None, True
            d = c.get('data')
            if isinstance(d, dict) and isinstance(d.get('codes'), dict):
                return d, False
        except Exception:
            pass
        return None, False

    # ── 校验 ──────────────────────────────────────────────
    @staticmethod
    def verify_pwd(data, user, pwd):
        """PBKDF2-sha256 校验，与 make_gate.py 的格式保持一致。

        用 compare_digest 而不是 ==：普通字符串比较会在第一个不同字节
        就返回，耗时差异可被用来逐字节猜密码（时序攻击）。
        """
        rec = (data.get('codes') or {}).get(user)
        if not isinstance(rec, dict):
            return False
        algo = (data.get('algo') or '').lower()
        if 'pbkdf2' not in algo:
            return False                  # 只接受 PBKDF2，不支持 v1 的 sha256
        iters = int(data.get('iterations') or 200000)
        salt = rec.get('salt') or ''
        want = (rec.get('hash') or '').lower()
        if not salt or not want:
            return False
        try:
            dk = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'),
                                     bytes.fromhex(salt), iters)
        except Exception:
            return False
        return hmac.compare_digest(dk.hex().lower(), want)

    def check(self, user, pwd):
        """校验账号密码。返回 dict: ok / err / src / user。"""
        user = (user or '').strip()
        if not user or not isinstance(pwd, str) or not pwd:
            return {'ok': False, 'err': '请输入账号和密码'}
        cfg = self.cfg()
        if not cfg.get('enabled', True):
            return {'ok': True, 'note': 'gate disabled', 'user': user}
        max_days = float(cfg.get('cacheDays', 3) or 3)
        diag = []
        token = self.token(diag)
        if not token:
            return {'ok': False,
                    'err': '未找到访问令牌。请把它放进 %s（一行，不要引号）。排查: %s'
                           % (os.path.join(self.root, 'tools', 'gate_token.txt'),
                              self.token_diag())}
        # 每次都先联网取最新，缓存只作断网兜底
        data, err = self.fetch(token)
        if data is None:
            data, expired = self._cache_read(max_days)
            if data is None:
                if expired:
                    return {'ok': False,
                            'err': '无法连接账号服务器，且本地缓存已过期（%g 天）。'
                                   '请连上公司网络后重试。' % max_days}
                return {'ok': False, 'err': '无法连接账号服务器：%s' % (err or '')}
            src = 'cache'
        else:
            self._cache_write(data)
            src = 'net'
        if not self.verify_pwd(data, user, pwd):
            return {'ok': False, 'err': '账号或密码不正确', 'src': src}
        return {'ok': True, 'user': user, 'src': src}
