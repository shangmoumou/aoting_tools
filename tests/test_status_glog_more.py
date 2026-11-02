#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""glog 日志提取 v2：多目录 / 时间六框 / 命中文件清单。

为什么每块都"真跑"而不是查源码字符串：
  车端脚本是整份下发到车上执行的，静态看没问题、跑起来才崩的坑已经踩过
  好几次（s4 = SESSION、脚本超 128KB）。时间窗、无时间名文件的兜底扫描、
  每份文件各贡献多少行 —— 这些靠读代码是看不全的。
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), 'apps', 'statusweb')
SERVER = os.path.join(APP, 'server.py')
PAGE = os.path.join(APP, 'index.html')
NODE = shutil.which('node')

PASS = FAIL = 0


def ck(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ok   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s %s' % (name, extra))


def epoch(y, mo, d, h, mi, s):
    return time.mktime(datetime.datetime(y, mo, d, h, mi, s).timetuple())


SRC = open(SERVER, encoding='utf-8').read()
PAGE_TXT = open(PAGE, encoding='utf-8').read()


def glog_py_src():
    m = "GLOG_PY = r" + "'" * 3
    i = SRC.index(m) + len(m)
    j = SRC.index("'" * 3, i)
    return SRC[i:j]


# ══════════════ 1. 车端脚本：端到端真跑 ══════════════
def build_tree(root):
    d1 = os.path.join(root, 'diagnose')
    d2 = os.path.join(root, 'tasks_manager')
    d3 = os.path.join(root, 'weird')
    for p in (d1, d2, d3):
        os.makedirs(p)
    # 用户给的真实格式：I/W/E + mmdd hh:mm:ss.uuuuuu
    with open(os.path.join(d1, 'diagnose_info_20260921-120349.4292'), 'w') as f:
        f.write('Log file created at: 2026/09/21 12:03:49\n')
        f.write('Running on machine: aoting\n')
        f.write('Log line format: [IWEF]mmdd hh:mm:ss.uuuuuu threadid file:line] msg\n')
        f.write('I0921 12:03:49.189131  4292 log.h:99] glog inited\n')
        f.write('I0921 12:03:50.919450  4292 drivermanager.cpp:42] IMU relocation\n')
        f.write('I0921 12:03:51.947355  4292 ros_bridge.cpp:182] [Diagnose] platform\n')
        f.write('W0921 12:04:01.986698  4292 diagnose.cpp:94] init SharedMemory\n')
        f.write('E0921 12:04:01.987392  4292 diagnose.cpp:96] 报错了\n')
        f.write('    at foo()\n')       # 续行：堆栈，必须跟着上一条走
        f.write('    at bar()\n')
        f.write('I0921 12:04:01.990926  4292 diagnose.cpp:1082] underlayer_monitor\n')
        f.write('I0921 12:09:01.000000  4292 x.cpp:1] 窗口外的一行\n')
    # 带时间名的目录里再放一个「认不出时间」的文件：不该被扫
    with open(os.path.join(d1, 'strange.log'), 'w') as f:
        f.write('I0921 12:03:52.000000  1 z.cpp:1] 不该出现\n')
    with open(os.path.join(d2, 'tasks_manager_info_20261020-111747.4186'), 'w') as f:
        f.write('I1020 11:17:47.959247  4186 log.h:99] glog inited\n')
    # 整个目录都没有带时间的文件名：按 mtime 兜底扫最近 N 份
    for i in range(40):
        p = os.path.join(d3, 'f%02d.log' % i)
        with open(p, 'w') as f:
            f.write('I0921 12:03:5%d.000000  1 a.cpp:1] w%d\n' % (i % 10, i))
        os.utime(p, (1000000000 + i, 1000000000 + i))
    return d1, d2, d3


def run_agent(spec, t0, t1, cap=6000, lv='', maxf=314572800, fb=30):
    tmp = tempfile.mkdtemp()
    try:
        py = os.path.join(tmp, 'a.py')
        with open(py, 'w') as f:
            f.write(glog_py_src())
        cmd = [sys.executable, '-u', py, json.dumps(spec),
               '%.3f' % t0, '%.3f' % t1, str(cap), lv, str(maxf), str(fb)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        assert r.returncode == 0, 'rc=%s %s' % (r.returncode, r.stderr[:200])
        lines = [x for x in r.stdout.strip().splitlines() if x.strip()]
        return json.loads(lines[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


print('== 车端脚本端到端 ==')
root = tempfile.mkdtemp()
try:
    d1, d2, d3 = build_tree(root)
    spec = [
        {'key': 'diagnose', 'label': '故障诊断', 'short': '诊断', 'dir': d1},
        {'key': 'tasks_manager', 'label': '任务管理', 'short': '任务', 'dir': d2},
        {'key': 'weird', 'label': '怪目录', 'short': '怪', 'dir': d3},
    ]
    t0 = epoch(2026, 9, 21, 12, 2, 51)
    t1 = epoch(2026, 9, 21, 12, 4, 51)
    j = run_agent(spec, t0, t1)

    ck('脚本跑通', j.get('ok') is True, j.get('err'))
    dg = [d for d in j['dirs'] if d['key'] == 'diagnose'][0]
    ck('命中行数正确（6 条 + 2 续行）', dg['nline'] == 8, dg['nline'])
    ck('窗口外的行被排除',
       all('窗口外' not in L['x'] for L in j['lines']))
    ck('续行被保留',
       any('at foo()' in L['x'] for L in j['lines']))
    # 文件名清单
    ck('files 记录命中文件名',
       dg['files'] and dg['files'][0]['name'] == 'diagnose_info_20260921-120349.4292',
       dg['files'][:1])
    ck('files 记录每份贡献行数',
       dg['files'][0]['n'] == 8, dg['files'][0])
    ck('带时间名的目录不扫无时间名文件',
       'strange.log' not in dg['picked'], dg['picked'])
    ck('无时间名文件的行不出现',
       all('不该出现' not in L['x'] for L in j['lines']))
    # 另一目录时间窗不匹配
    tm = [d for d in j['dirs'] if d['key'] == 'tasks_manager'][0]
    ck('时间窗不匹配的目录不取行', tm['nline'] == 0, tm['nline'])
    ck('不匹配的目录也不扫文件', tm['picked'] == [], tm['picked'])
    # 兜底扫描上限
    wd = [d for d in j['dirs'] if d['key'] == 'weird'][0]
    ck('无时间名目录按 mtime 兜底扫', len(wd['picked']) == 30, len(wd['picked']))
    ck('兜底扫描有上限并报出未扫数量', wd.get('noscan') == 10, wd.get('noscan'))
    ck('兜底取的是最近修改的那批', 'f39.log' in wd['picked'] and 'f00.log' not in wd['picked'],
       wd['picked'][:3])
    # fb 可调
    j2 = run_agent(spec, t0, t1, fb=5)
    wd2 = [d for d in j2['dirs'] if d['key'] == 'weird'][0]
    ck('fbmax 可调（fb=5 只扫 5 份）', len(wd2['picked']) == 5, len(wd2['picked']))
    ck('fb=5 时未扫数 = 35', wd2.get('noscan') == 35, wd2.get('noscan'))
    # 未命中时要有 before/after
    t2 = epoch(2026, 1, 1, 0, 0, 0)
    j3 = run_agent(spec[:1], t2 - 60, t2 + 60)
    dg3 = j3['dirs'][0]
    ck('无命中时报出最近的一份文件', bool(dg3.get('after')), dg3.get('after'))
    ck('无命中时也列出扫过的文件',
       isinstance(dg3.get('files'), list), dg3.get('files'))
finally:
    shutil.rmtree(root, ignore_errors=True)

# ══════════════ 2. 单引号禁令 ══════════════
print('== 车端脚本下发安全 ==')
ck('GLOG_PY 内无单引号', "'" not in glog_py_src())
try:
    ast.parse(glog_py_src())
    ck('GLOG_PY 语法正确', True)
except SyntaxError as e:
    ck('GLOG_PY 语法正确', False, str(e))

# ══════════════ 3. 目录清单 ══════════════
print('== 日志来源清单 ==')
# GLOG_DIRS 里用 GLOG_BASE 做拼接，literal_eval 认不了 Name 节点 ——
# 先把基准路径代进去再解析。
mbase = re.search(r'^GLOG_BASE\s*=\s*(.+)$', SRC, re.M)
ck('GLOG_BASE 已定义', bool(mbase))
base = ast.literal_eval(mbase.group(1)) if mbase else ''
mdirs = re.search(r'^GLOG_DIRS\s*=\s*(\[.*?\n\])', SRC, re.M | re.S)
ck('GLOG_DIRS 源码段定位成功', bool(mdirs))
body = re.sub(r'GLOG_BASE\s*\+\s*', '', mdirs.group(1)) if mdirs else '[]'
keys = ast.literal_eval(body)
for x in keys:
    x['dir'] = base + x['dir']
ck('GLOG_DIRS 解析成功', isinstance(keys, list))
ck('来源恰好 17 个', len(keys) == 17, len(keys))
kk = [x['key'] for x in keys]
ck('key 无重复', len(kk) == len(set(kk)),
   [x for x in set(kk) if kk.count(x) > 1])
ck('每个来源都有 key 与 dir',
   all(x.get('key') and x.get('dir') for x in keys))
# 来源名就是目录名：不再另存中文别名，否则输出前缀会和勾选框对不上
ck('不再有 label/short 中文别名',
   all(not x.get('label') and not x.get('short') for x in keys))
ck('路径都在 glogs 下',
   all(x['dir'].startswith('/home/aoting/user_config/glogs/') for x in keys))
ck('不含 zip 文件等非目录项',
   all(not x['dir'].endswith('.zip') for x in keys))
ck('诊断与任务排在最前', kk[:2] == ['diagnose', 'tasks_manager'], kk[:3])

# 前端清单必须与服务端一致，否则勾了服务端不认识
m = re.search(r'const GL_SRC = \[(.*?)\n\];', PAGE_TXT, re.S)
ck('页面有 GL_SRC', bool(m))
page_keys = re.findall(r"'([^']+)'", m.group(1)) if m else []
ck('前后端 key 完全一致', page_keys == kk,
   'srv缺:%s page缺:%s' % ([x for x in kk if x not in page_keys],
                           [x for x in page_keys if x not in kk]))
# 默认一个都不勾：十几个目录全勾上车端扫得久
ck('页面默认一个都不勾',
   (len(re.findall(r'def:true', m.group(1))) == 0) if m else False)
ck('页面有 GL_DEF 一键选诊断+任务', 'GL_DEF' in PAGE_TXT)

# ══════════════ 4. 时间六框 ══════════════
print('== 基准时间六框 ==')
for eid in ('gly', 'glmo', 'gld', 'glh', 'glmi', 'gls'):
    ck('页面有输入框 ' + eid, ('id="%s"' % eid) in PAGE_TXT)
ck('已无旧的单个长串输入框', 'id="glt"' not in PAGE_TXT)
ck('有车端时间按钮', 'id="glnow"' in PAGE_TXT)
ck('有本机时间按钮', 'id="gllocal"' in PAGE_TXT)

if NODE:
    js = PAGE_TXT[PAGE_TXT.index('function glReadTime'):]
    js = js[:js.index('\nfunction glSetTime')]
    js = js.replace('$(id).value', 'V[id]')
    harness = """
var V = ARGS;
function $(id){ return {value: V[id]}; }
%s
var r = glReadTime();
console.log(r === null ? 'null' : String(r));
""" % js
    def rt(**kw):
        v = {'gly': '', 'glmo': '', 'gld': '', 'glh': '', 'glmi': '', 'gls': ''}
        v.update({k: str(x) for k, x in kw.items()})
        p = os.path.join(tempfile.mkdtemp(), 't.js')
        with open(p, 'w') as f:
            f.write('var ARGS = %s;' % json.dumps(v))
            f.write(harness)
        r = subprocess.run([NODE, p], capture_output=True, text=True)
        return r.stdout.strip() or r.stderr.strip()[:120]
    ok = rt(gly=2026, glmo=10, gld=20, glh=11, glmi=18, gls=0)
    want = str(int(epoch(2026, 10, 20, 11, 18, 0)))
    ck('六框解析出正确时间戳', ok == want, '%s vs %s' % (ok, want))
    ck('缺一项返回 null',
       rt(gly=2026, glmo=10, gld=20, glh=11, glmi=18) == 'null')
    ck('2 月 31 日被判非法（不静默进位）',
       rt(gly=2026, glmo=2, gld=31, glh=0, glmi=0, gls=0) == 'null')
    ck('月份 13 被判非法',
       rt(gly=2026, glmo=13, gld=1, glh=0, glmi=0, gls=0) == 'null')
    ck('秒 60 被判非法',
       rt(gly=2026, glmo=1, gld=1, glh=0, glmi=0, gls=60) == 'null')

# ══════════════ 5. 命中文件名显示 ══════════════
print('== 命中文件名显示 ==')
ck('有 glFileList 函数', 'function glFileList' in PAGE_TXT)
ck('命中与未命中两处都列文件清单',
   PAGE_TXT.count('glFileList(d,') >= 2, PAGE_TXT.count('glFileList(d,'))
ck('文件清单带各自行数', "'(' + f.n + ')'" in PAGE_TXT or "f.n" in PAGE_TXT)
ck('清单过长会折叠', 'GLOG_SHOW_FILES' in PAGE_TXT)

# ══════════════ 6. 服务端参数传递 ══════════════
print('== 服务端 ==')
ck('下发命令带 fbmax 参数', "cmd = ('python3 -u -c %s %s %.3f %.3f %d %s %d %d'"
   in SRC)
ck('GLOG_FALLBACK_SCAN 已定义', 'GLOG_FALLBACK_SCAN = ' in SRC)
ck('超时放宽到 180', 'GLOG_TIMEOUT = 180' in SRC)

print('')
print('PASS=%d FAIL=%d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
