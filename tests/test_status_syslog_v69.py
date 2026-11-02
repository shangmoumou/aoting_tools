#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.1.4：glog 来源名可见性 + 系统日志（dmesg/串口/system）提取。

这一版修的那个「来源名不显示」的 bug，静态看 HTML 完全正常 ——
复选框、文字都在，只有知道 input{width:100%} 会把复选框撑成整行宽才
说得通。所以这里除了查结构，还把车端脚本真跑一遍、把前端渲染真跑一遍。
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


def epoch(s):
    return time.mktime(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timetuple())


SRC = open(SERVER, encoding='utf-8').read()
P = open(PAGE, encoding='utf-8').read()

# ───────────────── 1. 来源名不显示：根因是 CSS ─────────────────
print('[复选框宽度：根因]')
# 全局 input{width:100%} 会把复选框撑成整行宽，文字被挤走。
# 必须有单独一条 input[type=checkbox] 把它拉回来。
ck('有 input[type=checkbox] 单独规则',
   re.search(r'input\[type=checkbox\]\s*\{[^}]*width\s*:\s*auto', SRC and P, re.S)
   is not None)
m = re.search(r'input\[type=checkbox\]\s*\{(.*?)\}', P, re.S)
ck('复选框宽度钉为 auto', bool(m) and 'width:auto' in m.group(1))
ck('复选框不再被 flex 拉伸', bool(m) and 'flex:0 0 auto' in m.group(1))
# 旧的写法是每个地方内联 style="width:auto" 绕过，现在从根上修掉
ck('全局 input 仍是 width:100%（文本框不受影响）',
   re.search(r'\n\s*input\{width:100%', P) is not None)

# ───────────────── 2. 来源清单：用目录名，默认不勾 ─────────────────
print('[来源清单]')
m = re.search(r'const GL_SRC = \[(.*?)\n\];', P, re.S)
ck('页面有 GL_SRC', bool(m))
page_keys = re.findall(r"'([^']+)'", m.group(1)) if m else []
ck('恰好 17 个来源', len(page_keys) == 17, len(page_keys))
ck('默认一个都不勾', 'def:true' not in (m.group(1) if m else ''))
ck('有一键选诊断+任务', 'GL_DEF' in P)

mdirs = re.search(r'^GLOG_DIRS\s*=\s*(\[.*?\n\])', SRC, re.M | re.S)
mbase = re.search(r"^GLOG_BASE = '([^']+)'", SRC, re.M)
base = mbase.group(1) if mbase else ''
body = re.sub(r'GLOG_BASE\s*\+\s*', '', mdirs.group(1)) if mdirs else '[]'
keys = ast.literal_eval(body)
ck('GLOG_DIRS 解析成功', isinstance(keys, list))
ck('服务端也是 17 个', len(keys) == 17, len(keys))
ck('前后端 key 完全一致', [x['key'] for x in keys] == page_keys,
   'srv=%s page=%s' % ([x['key'] for x in keys], page_keys))
# 来源名就是目录名：输出前缀要能对着终端 ls 找到文件
ck('来源不再带 label/short 别名',
   all(not x.get('label') and not x.get('short') for x in keys))
# dir 已被剥掉 GLOG_BASE 前缀，剩下的应该正好等于 key
ck('路径就是目录名', all(x['dir'].lstrip('/') == x['key'] for x in keys),
   [x for x in keys if x['dir'].lstrip('/') != x['key']][:3])

# 渲染出的标签：文字必须可见（不能被 overflow:hidden 裁掉）
mb = re.search(r'function glBuildSrcs\(\)\{(.*?)\n\}', P, re.S)
ck('能取到 glBuildSrcs', bool(mb))
if mb:
    b = mb.group(1)
    ck('标签用 flex 横排（复选框与文字同一行）', 'display:flex' in b)
    ck('标签不再 overflow:hidden', 'overflow:hidden' not in b)
    ck('文字包在 span 里且写入目录名', '<span>' in b and 'GL_SRC[i]' in b)
    ck('文字用正文色而非 dim', 'color:var(--text)' in b)

# ───────────────── 3. 按钮压缩到一行 ─────────────────
print('[按钮布局]')
# glwin / glunit / gllv 与 六个按钮必须在同一个 flex 容器里
mrow = re.search(r'<div style="display:flex;[^"]*">\s*<span[^>]*>前后 ±</span>(.*?)</div>',
                 P, re.S)
ck('前后± 与操作按钮同一行', bool(mrow))
if mrow:
    seg = mrow.group(1)
    for i in ('glunit', 'gllv', 'glall', 'glnone', 'gldef',
              'glgo', 'glcopy', 'gldl', 'glcnt'):
        ck('同一行含 #%s' % i, ('id="%s"' % i) in seg)
    ck('不再有「只留默认两个」旧文案', '只留默认两个' not in P)

# ───────────────── 4. 系统日志输出框 ─────────────────
print('[系统日志 UI]')
for i in ('sygo', 'sycpy', 'sydl', 'syst', 'syout'):
    ck('页面含 #%s' % i, ('id="%s"' % i) in P)
ck('系统日志框在 glog 输出框之后', P.index('id="syout"') > P.index('id="glout"'))
ck('复用同一个时间窗（提示写明）', '用上面同一个时间窗' in P)
ck('系统日志函数已接线',
   all(("$('%s').onclick" % x) in P for x in ('sygo', 'sycpy', 'sydl')))

# ───────────────── 5. 服务端 ─────────────────
print('[服务端]')
ck('有 SYS_DIR', 'SYS_DIR = ' in SRC)
ck('SYS_DIR 指向 logs/system',
   re.search(r"SYS_DIR = '/home/aoting/user_config/logs/system'", SRC) is not None)
ck('有 api_syslog 方法', 'def api_syslog(self, b)' in SRC)
ck('路由已注册 /api/syslog', "'/api/syslog'" in SRC)

m = re.search(r"SYSLOG_PY = r'''(.*?)'''", SRC, re.S)
ck('能取到 SYSLOG_PY', bool(m))
SY = m.group(1) if m else ''
if SY:
    try:
        ast.parse(SY)
        ok = True
    except SyntaxError as e:
        ok = False
        print('     SyntaxError: %s' % e)
    ck('SYSLOG_PY 能编译', ok)
    # 单引号禁令：整份会被 shell_quote 包起来经 SSH 送远端 bash
    ck('SYSLOG_PY 不含单引号', "'" not in SY)
    ck('SYSLOG_PY 不含三单引号', "'''" not in SY)
    ck('长度小于 argv 上限 131072', len(SY) < 100000, 'len=%d' % len(SY))
    ck('认 system_20261019_191533.log 这种命名',
       r'(\d{8})[-_](\d{6})' in SY)
    ck('认 dmesg -T 的时间格式', 'DMESG_RE' in SY and 'dmesg' in SY)
    ck('串口关键词含中文（车上日志有中文）',
       '串口' in SY and 'disconnect' in SY)

# Handler 里不能出现引用了不存在名字的分支：
# 之前 s4 = SESSION 就是这么静默崩掉的，前端只看到 Failed to fetch
import symtable
top = symtable.symtable(SRC, 'server.py', 'exec')
globs = set(top.get_identifiers())


def _walk(tbl, out):
    for ch in tbl.get_children():
        out.add(ch.get_name())
        _walk(ch, out)


names = set(dir(__builtins__)) if isinstance(__builtins__, dict) \
    else set(dir(__builtins__))
_walk(top, names)
# bool/int/getattr 这些是内建，不是「引用了不存在的全局名字」
for b in ('bool', 'int', 'str', 'float', 'getattr', 'setattr', 'len', 'list',
          'dict', 'tuple', 'set', 'isinstance', 'repr', 'round', 'min', 'max',
          'sum', 'sorted', 'enumerate', 'zip', 'range', 'hasattr', 'type',
          'open', 'super', 'Exception', 'True', 'False', 'None'):
    names.add(b)
bad = []
mcls = re.search(r'\nclass Handler\(.*?\n(?=\n(?:class|def)\s|\Z)', SRC, re.S)
if mcls:
    for mm in re.finditer(r'def (do_\w+|api_\w+)\(self(?:, (\w+))?\)\s*:(.*?)(?=\n    def |\Z)',
                          mcls.group(0), re.S):
        fn, arg, blk = mm.group(1), mm.group(2), mm.group(3)
        try:
            st = symtable.symtable('def f(self%s):\n%s' % (
                (', ' + arg) if arg else '', blk), 'x', 'exec')
        except SyntaxError:
            continue
        inner = st.get_children()[0]
        for s in inner.get_symbols():
            if s.is_global() or (s.is_free() and not s.is_assigned()):
                if s.get_name() not in globs and s.get_name() not in names:
                    bad.append('%s:%s' % (fn, s.get_name()))
ck('Handler 各分支无未定义名字引用', not bad, bad[:6])

# ───────────────── 6. 车端脚本真跑 ─────────────────
print('[SYSLOG_PY 实跑]')
TMP = tempfile.mkdtemp(prefix='sytest')
dp = os.path.join(TMP, 'system')
os.makedirs(dp, exist_ok=True)
# 名字照抄车上真实命名：system_20261019_191533.log
open(os.path.join(dp, 'system_20261019_191533.log'), 'w', encoding='utf-8').write(
    '2026-10-20 11:17:40.100 [INFO] boot ok\n'
    '2026-10-20 11:17:48.500 [WARN] usb 1-1: USB disconnect, device 3\n'
    '2026-10-20 11:17:49.100 [INFO] 串口 /dev/ttyUSB0 断连\n'
    '2026-10-20 11:18:00.000 [INFO] task start\n'
    '    at some_stack_frame()\n'          # 续行，必须跟着上一条一起留下
    '2026-10-20 11:25:00.000 [INFO] out of window\n')
open(os.path.join(dp, 'system_20261020_111722.log'), 'w', encoding='utf-8').write(
    '2026-10-20 11:19:00.000 [INFO] second file in window\n')
open(os.path.join(dp, 'system_20261021_115703.log'), 'w', encoding='utf-8').write(
    '2026-10-21 11:30:00.000 [INFO] much later\n')

t = epoch('2026-10-20 11:18:00')
r = subprocess.run([sys.executable, '-c', SY, dp,
                    str(t - 300), str(t + 300), '4000', '83886080', '30'],
                   capture_output=True, text=True, timeout=120)
ck('车端脚本退出码 0', r.returncode == 0, r.stderr[-200:])
j = None
for ln in reversed([x.strip() for x in r.stdout.splitlines()]):
    if not ln:
        continue
    try:
        j = json.loads(ln)
        break
    except Exception:
        continue
ck('返回可解析 JSON', j is not None, r.stdout[:200])
if j:
    ck('ok=True', j.get('ok') is True, j.get('err'))
    ck('按文件名圈中 2 个候选', len(j.get('picked') or []) == 2,
       j.get('picked'))
    ck('认出 system_20261019_191533.log',
       'system_20261019_191533.log' in (j.get('picked') or []))
    xs = [L['x'] for L in (j.get('system') or [])]
    ck('命中窗内的行', any('task start' in x for x in xs), xs[:3])
    ck('窗外 11:25 的行不进', not any('out of window' in x for x in xs))
    ck('续行跟着上一条保留', any('some_stack_frame' in x for x in xs))
    ck('ISO 时间戳解析出非零时间',
       all(L.get('t', 0) > 0 for L in (j.get('system') or []) if 'task' in L['x']))
    sr = [L['x'] for L in (j.get('serial') or [])]
    ck('串口/USB 断连单独拎出来', len(sr) >= 2, sr[:3])
    ck('英文关键词命中', any('USB disconnect' in x for x in sr))
    ck('中文关键词命中（只按英文 grep 会漏）', any('串口' in x for x in sr))
    ck('文件清单带各自贡献行数',
       all('n' in f for f in (j.get('files') or [])), j.get('files'))
    ck('每个文件报出可解析时间戳条数',
       all('nts' in f for f in (j.get('files') or [])))

# ───────────────── 7. 前端渲染真跑 ─────────────────
print('[前端渲染 实跑]')
if NODE:
    js = re.search(r'<script>(.*)</script>', P, re.S).group(1)
    stub = r'''
const _els = {};
global.document = {
  getElementById: function(id){
    if (!_els[id]) _els[id] = {id:id, value:'', textContent:'', style:{},
                               checked:false, disabled:false, className:'',
                               innerHTML:'', onclick:null,
                               addEventListener:function(){},
                               appendChild:function(){},
                               classList:{add:function(){}, remove:function(){}},
                               dataset:{},
                               querySelectorAll:function(){ return []; },
                               querySelector:function(){ return null; },
                               focus:function(){}, click:function(){}};
    return _els[id];
  },
  createElement: function(){ return {style:{}, click:function(){},
                                     appendChild:function(){}}; },
  querySelectorAll: function(){ return []; },
  querySelector: function(){ return null; },
  body: {appendChild:function(){}, removeChild:function(){}}
};
global.window = {};
global.navigator = {clipboard:{writeText:function(){}}};
global.URL = {createObjectURL:function(){return 'blob:x';},
              revokeObjectURL:function(){}};
global.Blob = function(){};
global.setTimeout = function(){};
global.setInterval = function(){ return 0; };
global.clearInterval = function(){};
global.fetch = function(){ return new Promise(function(){}); };
'''
    tail = r'''
;(function(){
  const out = {};
  // 来源清单：文字必须真的写进去了
  glBuildSrcs();
  const h = document.getElementById('glsrcs').innerHTML;
  out.srcHtml = h;
  out.srcCount = (h.match(/<input type="checkbox"/g) || []).length;
  out.hasName = h.indexOf('<span>diagnose</span>') >= 0;
  out.hasLongName = h.indexOf('<span>BlueSea2LaserUDPLogPath</span>') >= 0;
  out.noOverflow = h.indexOf('overflow:hidden') < 0;
  out.defaultNone = (h.match(/checked/g) || []).length === 0;

  // 一键选诊断+任务
  glSetSrcs(function(x){ return GL_DEF.indexOf(x) >= 0; });
  out.picked2 = glSrcKeys().length;

  // 系统日志渲染
  const t = new Date(2026, 9, 20, 11, 18, 0).getTime() / 1000;
  const fake = {ok:true, t0:t-300, t1:t+300, nline:3,
    dm_note:'测试提示', dm_all:353, dm_ts:353,
    total:3, picked:['system_20261019_191533.log'], noscan:0, sys_err:'',
    files:[{name:'system_20261019_191533.log', n:2, nts:5}],
    dmesg:[{t:t, k:'dmesg', x:'[Mon Oct 20 11:17:47 2026] usb 1-1: disconnect'}],
    serial:[{t:t, k:'serial', x:'usb 1-1: USB disconnect'}],
    system:[{t:t, k:'system', x:'2026-10-20 11:18:00.000 [INFO] task start'}],
    trunc:false};
  syRender(fake, t, 300);
  out.sy = document.getElementById('syout').value;
  out.sySt = document.getElementById('syst').textContent;
  out.syHead = out.sy.indexOf('# 时间窗') === 0;
  out.syHasDm = out.sy.indexOf('── dmesg（1 行）──') >= 0;
  out.syHasSerial = out.sy.indexOf('── 串口 / USB 断连（1 行）──') >= 0;
  out.syHasSys = out.sy.indexOf('── system（1 行）──') >= 0;
  out.syHasCnt = (out.sySt || '').indexOf('dmesg 1 / 串口 1 / system 1 行') >= 0;
  out.syHasFile = out.sy.indexOf('system_20261019_191533.log(2)') >= 0;
  out.syHasNote = out.sy.indexOf('测试提示') >= 0;

  // 空结果要说清楚，而不是给个空框
  const none = {ok:true, t0:t, t1:t, nline:0, dm_note:'', dm_all:0, dm_ts:0,
    total:0, picked:[], noscan:0, sys_err:'目录不存在',
    files:[], dmesg:[], serial:[], system:[], trunc:false};
  syRender(none, t, 300);
  out.noneHasErr = document.getElementById('syout').value.indexOf('目录不存在') >= 0;
  out.noneSt = document.getElementById('syst').textContent;

  require('fs').writeFileSync(process.argv[2], JSON.stringify(out));
})();
'''
    pj = os.path.join(TMP, 'page.js')
    oj = os.path.join(TMP, 'out.json')
    open(pj, 'w', encoding='utf-8').write(stub + '\n' + js + '\n' + tail)
    rr = subprocess.run([NODE, pj, oj], capture_output=True, text=True, timeout=60)
    o = {}
    if os.path.isfile(oj):
        o = json.loads(open(oj, encoding='utf-8').read())
    else:
        ck('node 跑通前端渲染', False, rr.stderr[:300])
    if o:
        ck('渲染出 17 个复选框', o['srcCount'] == 17, o['srcCount'])
        ck('目录名写进标签', o['hasName'])
        ck('最长的目录名也完整写入', o['hasLongName'])
        ck('标签不再裁切文字', o['noOverflow'])
        ck('默认一个都不勾', o['defaultNone'])
        ck('一键选诊断+任务 -> 2 个', o['picked2'] == 2, o['picked2'])
        ck('系统日志首行是时间窗头', o['syHead'])
        ck('分三段输出', o['syHasDm'] and o['syHasSerial'] and o['syHasSys'])
        ck('状态行给出各组条数', o['syHasCnt'])
        ck('列出命中文件及行数', o['syHasFile'])
        ck('dmesg 提示会带出来', o['syHasNote'])
        ck('目录不存在时说明原因', o['noneHasErr'])
        ck('无命中时状态行明确', '没有系统日志' in (o['noneSt'] or ''), o['noneSt'])
else:
    print('  skip 前端实跑（无 node）')

shutil.rmtree(TMP, ignore_errors=True)
print('\nPASS=%d FAIL=%d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
