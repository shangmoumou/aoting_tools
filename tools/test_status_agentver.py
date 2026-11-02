# -*- coding: utf-8 -*-
"""
验证：车端采集脚本的版本号能被前端比对上。

背景（本次修的那个 bug）：
    采集脚本的源码是 PC 端经 SSH 下发到车上执行的，车上不留文件。
    所以页面上写着"有导航状态"、车上跑的却是旧脚本时，前端拿不到
    nav 字段 —— 而旧前端在字段缺失时整栏不画，用户看到的就是
    "压根没这个功能"，完全想不到是本地服务没重启。

    现在脚本每帧上报 agent_ver，前端拿它和 PAGE_VER 比对。
    三者（app.json / AGENT_VER / PAGE_VER）必须一致，否则这套
    比对本身就是错的。
"""
import io
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, '..', 'apps', 'statusweb')
PASS = FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('ok   ' + msg)
    else:
        FAIL += 1
        print('FAIL ' + msg)


def read(p):
    with io.open(p, encoding='utf-8') as f:
        return f.read()


app = json.loads(read(os.path.join(APP, 'app.json')))
agent = read(os.path.join(APP, 'stat_agent.py'))
html = read(os.path.join(APP, 'index.html'))

print('=' * 60)
print(' 车端采集脚本版本号一致性')
print('=' * 60)

m = re.search(r'^AGENT_VER\s*=\s*"([^"]+)"', agent, re.M)
ok(m is not None, 'stat_agent.py 定义了 AGENT_VER')
ver_agent = m.group(1) if m else None

m2 = re.search(r"const PAGE_VER = '([^']+)';", html)
ok(m2 is not None, 'index.html 定义了 PAGE_VER')
ver_page = m2.group(1) if m2 else None

ok(ver_agent == app['ver'],
   'AGENT_VER(%s) == app.json ver(%s)' % (ver_agent, app['ver']))
ok(ver_page == app['ver'],
   'PAGE_VER(%s) == app.json ver(%s)' % (ver_page, app['ver']))

ok('"agent_ver": AGENT_VER' in agent, '采集脚本每帧上报 agent_ver')
ok('renderAgentVer(d.agent_ver)' in html, 'render() 会做版本比对')
ok('id="agentver"' in html, '页面有 agentver 容器')

print('=' * 60)
print(' 通过 %d  失败 %d' % (PASS, FAIL))
print('=' * 60)
sys.exit(1 if FAIL else 0)
