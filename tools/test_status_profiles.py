#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.1.0「已保存机器（连接记录）」验收。

为什么要落盘写测试：之前几轮都是临时目录里现写、改完就丢，
下一轮又重写一遍，出了问题也无从回溯。

原则：能真跑的一律真跑，不查源码字符串。
  - 密码往返：真调 _prof_enc / _prof_dec
  - 增 / 查 / 删：真写临时文件再读回来
  - handler 分支：AST 扫一遍，确认没有引用未定义的名字
    （防 `s4 = SESSION` 那类 —— handler 一崩，前端只看得到
     Failed to fetch，现象和原因隔着十万八千里）
"""
import os
import re
import ast
import sys
import json
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SW = os.path.join(ROOT, 'apps', 'statusweb')

FAILED = []


def chk(cond, msg):
    if cond:
        print('  ok   %s' % msg)
    else:
        print('  FAIL %s' % msg)
        FAILED.append(msg)
    return bool(cond)


# ── 载入 server 模块（用临时文件做记录库，不碰真机的 ~/.aoting_tools）──
TMPD = tempfile.mkdtemp(prefix='_pf_')
os.environ['AOTING_CONN_PROFILES'] = os.path.join(TMPD, 'conn_profiles.json')
sys.path.insert(0, SW)
import server  # noqa: E402


def t_enc():
    print('[1] 密码混淆往返')
    for raw in ('', 'aoting', 'P@ssw0rd!', '密码 123', 'x' * 500):
        chk(server._prof_dec(server._prof_enc(raw)) == raw,
            '往返一致 len=%d' % len(raw))
    # 坏数据不能抛异常
    chk(server._prof_dec('!!!not-base64!!!') == '', '坏 base64 返回空串不抛异常')
    chk(server._prof_dec(None) == '', 'None 返回空串不抛异常')


def t_crud():
    print('[2] 增 / 查 / 删（真写文件真读回）')
    fp = server._prof_path()
    items = server._profiles_load()[0]
    chk(items == [], '初始为空')

    server._profiles_write([
        {'name': '一号车', 'host': 'frp.aotbot.com', 'port': 21723,
         'user': 'aoting', 'pwd': 'pw1'},
        {'name': '二号车', 'host': 'frp.aotbot.com', 'port': 21888,
         'user': 'root', 'pwd': ''},
        {'name': '坏记录', 'host': '', 'port': 22, 'user': 'x', 'pwd': ''},
    ])
    got = server._profiles_load()[0]
    chk(len(got) == 2, '缺 host 的坏记录被过滤（拿到 %d 条）' % len(got))
    a = [x for x in got if x['name'] == '一号车']
    chk(bool(a), '一号车读回')
    if a:
        chk(a[0]['host'] == 'frp.aotbot.com' and a[0]['port'] == 21723,
            '主机端口一致')
        chk(a[0]['user'] == 'aoting', '用户名一致')
        chk(a[0]['pwd'] == 'pw1', '密码解密一致')
    b = [x for x in got if x['name'] == '二号车']
    chk(bool(b) and b[0]['pwd'] == '', '免密记录密码为空')

    # 文件里不能直接出现明文密码（轻度混淆的意义所在）
    raw = open(fp, encoding='utf-8').read()
    chk('pw1' not in raw, '文件里没有明文密码 pw1')
    chk('pwd64' in raw, '密码以 pwd64 字段存放')

    # 同名覆盖
    items = server._profiles_load()[0]
    for i, x in enumerate(items):
        if x['name'] == '一号车':
            items[i] = dict(x, port=29999)
    server._profiles_write(items)
    got = server._profiles_load()[0]
    one = [x for x in got if x['name'] == '一号车']
    chk(bool(one) and one[0]['port'] == 29999, '同名覆盖后端口已更新')
    chk(len(got) == 2, '覆盖不会攒出重复项（共 %d 条）' % len(got))

    # 删除
    keep = [x for x in server._profiles_load()[0] if x['name'] != '二号车']
    server._profiles_write(keep)
    got = server._profiles_load()[0]
    chk(len(got) == 1 and got[0]['name'] == '一号车', '删除生效')

    # 损坏文件不能让服务挂掉
    open(fp, 'w', encoding='utf-8').write('{{{ not json')
    chk(server._profiles_load()[0] == [], '损坏文件返回空列表不抛异常')


def t_path():
    print('[3] 存储位置')
    os.environ.pop('AOTING_CONN_PROFILES', None)
    p = server._prof_path()
    home = os.path.expanduser('~')
    chk(p.startswith(home), '默认落在用户主目录下：%s' % p)
    chk(p.endswith('conn_profiles.json'), '文件名 conn_profiles.json')
    chk('.aoting_tools' in p, '在 .aoting_tools 子目录（跨版本保留，不随解压目录走）')
    os.environ['AOTING_CONN_PROFILES'] = os.path.join(TMPD, 'conn_profiles.json')


def t_route():
    print('[4] 路由与 handler 未定义名扫描')
    src = open(os.path.join(SW, 'server.py'), encoding='utf-8').read()
    chk("if p == '/api/profiles':" in src, '存在 /api/profiles 分支')
    tree = ast.parse(src)
    cls = [n for n in tree.body
           if isinstance(n, ast.ClassDef) and n.name == 'Handler']
    chk(bool(cls), '找到 Handler 类')
    if not cls:
        return
    # 用 symtable 判定作用域：自己 walk AST 去收集 for / with / import
    # 绑定的名字太容易漏（第一轮就误报了 k / v / i / f / urllib）。
    # symtable 直接告诉我哪些名字是"引用到全局"，这才是要查的那一类。
    import symtable
    known = set(dir(server)) | set(dir(__builtins__))
    st = symtable.symtable(src, 'server.py', 'exec')
    htab = None
    for ch in st.get_children():
        if ch.get_name() == 'Handler':
            htab = ch
            break
    chk(htab is not None, 'symtable 找到 Handler 作用域')
    bad = []
    if htab is not None:
        for fnt in htab.get_children():
            for sym in fnt.get_symbols():
                if sym.is_global() and sym.get_name() not in known:
                    bad.append('%s: %s' % (fnt.get_name(), sym.get_name()))
    chk(not bad, 'handler 无未定义名引用' + ('' if not bad else ' → %s' % sorted(set(bad))))


def t_html():
    print('[5] 前端接线')
    html = open(os.path.join(SW, 'index.html'), encoding='utf-8').read()
    for need in ('id="profsel"', 'id="bsave"', 'id="bdelprof"', 'id="profnote"',
                 'function profLoad', 'function profSave', 'function profDel',
                 "/api/profiles", "$('bsave').onclick = profSave;", "profLoad('')"):
        chk(need in html, '含 %s' % need)
    chk('bs.disabled = !on' in html, '保存按钮随连接状态启用/禁用')
    # 静态区 div 配对（JS 字符串里的不算，只看到 <script 之前）
    head = html.split('<script')[0]
    chk(head.count('<div') == head.count('</div>'),
        '静态区 div 配对 %d/%d' % (head.count('<div'), head.count('</div>')))
    # JS 语法
    js = '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', html, re.S))
    tf = os.path.join(TMPD, 'page.js')
    open(tf, 'w', encoding='utf-8').write(js)
    r = subprocess.run(['node', '--check', tf], capture_output=True, text=True)
    chk(r.returncode == 0, 'JS 语法（node --check）' + ('' if r.returncode == 0
                                                    else ': %s' % r.stderr[:200]))


def t_ver():
    print('[6] 版本')
    # 版本号从 version.json 动态取，不写死 ——
    # 写死的话每次发版都误报，反而把「版本四处是否一致」这个真检查点盖住。
    vj = json.load(open(os.path.join(ROOT, 'version.json'), encoding='utf-8'))
    suite = vj.get('suite')
    pv = (vj.get('config') or {}).get('portalVer')
    chk(bool(suite), 'version.json 有 suite（实际 %s）' % suite)
    # 门户版本号自成一条线（v1.1.0 起号），不再与工具箱总版本同步
    chk(bool(pv) and re.match(r'^\d+\.\d+\.\d+$', str(pv)) is not None,
        'portalVer 合法（实际 %s）' % pv)
    aj = json.load(open(os.path.join(SW, 'app.json'), encoding='utf-8'))
    sv = aj.get('ver')
    html = open(os.path.join(SW, 'index.html'), encoding='utf-8').read()
    ag = open(os.path.join(SW, 'stat_agent.py'), encoding='utf-8').read()
    chk("const PAGE_VER = '%s';" % sv in html,
        'PAGE_VER = %s' % sv)
    chk('AGENT_VER = "%s"' % sv in ag, 'AGENT_VER = %s' % sv)
    cl = open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8').read()
    chk(cl.lstrip().startswith('## v' + suite),
        'CHANGELOG 首节为 v%s' % suite)


def main():
    t_enc()
    t_crud()
    t_path()
    t_route()
    t_html()
    t_ver()
    shutil.rmtree(TMPD, ignore_errors=True)
    print('\n%s  FAIL=%d' % ('ALL PASS' if not FAILED else 'FAILED', len(FAILED)))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
