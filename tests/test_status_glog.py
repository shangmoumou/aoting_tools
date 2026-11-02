#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""glog 日志提取：车端脚本 + 服务端路由 + 前端渲染。

为什么这里要"真跑"车端脚本、而不是只查源码里有没有某个字符串：
  之前 s4 = SESSION 那种 bug 静态看代码完全正常，只有跑一遍才暴露。
  提取逻辑涉及时间窗边界、跨年、续行、软链去重，靠肉眼根本看不全。
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
m = re.search(r"GLOG_PY = r'''(.*?)'''", SRC, re.S)
ck('能取到 GLOG_PY', bool(m))
GLOG_PY = m.group(1) if m else ''


# ───────────────────────── 车端脚本：静态 ─────────────────────────
print('[车端脚本 静态]')
ck('GLOG_PY 能编译', (lambda: (compile(GLOG_PY, 'g', 'exec'), True)[1])()
   if GLOG_PY else False)
# 单引号禁令：整份会被 shell_quote 用单引号包起来再经 SSH 送远端 bash
ck('GLOG_PY 不含单引号（shell 多层引用会变形）', "'" not in GLOG_PY)
ck('GLOG_PY 不含三单引号', "'''" not in GLOG_PY)
ck('长度远小于 argv 上限 131072', len(GLOG_PY) < 100000, 'len=%d' % len(GLOG_PY))


# ───────────────────────── 车端脚本：真跑 ─────────────────────────
print('[车端脚本 实跑]')
TMP = tempfile.mkdtemp(prefix='glt_')
D_DIAG = os.path.join(TMP, 'diagnose')
D_TASK = os.path.join(TMP, 'tasks_manager')
os.makedirs(D_DIAG)
os.makedirs(D_TASK)

DIAG = (
    'Log file created at: 2026/09/21 12:03:49\n'
    'Running on machine: aoting\n'
    'Log line format: [IWEF]mmdd hh:mm:ss.uuuuuu threadid file:line] msg\n'
    'I0921 12:03:49.189131  4292 log.h:99] glog inited\n'
    'I0921 12:03:50.919450  4292 drivermanager.cpp:42] [DriverEventsManager] IMU relocation mode=auto\n'
    'I0921 12:03:51.947355  4292 ros_bridge.cpp:182] [Diagnose] platform_type: OLD_PLATFORM\n'
    'I0921 12:03:51.947618  4292 diagnose.cpp:91] DiagnoseMainThread start..... wait time\n'
    'E0921 12:04:01.000001  4292 diagnose.cpp:99] something failed\n'
    '    at foo()\n'
    '    at bar()\n'
    'I0921 12:04:30.000000  4292 diagnose.cpp:100] later line\n'
)
open(os.path.join(D_DIAG, 'diagnose_info_20260921-114427.14389'), 'w').write(
    'I0921 11:44:27.100000 14389 log.h:99] glog inited\n'
    'I0921 11:50:00.000000 14389 a.cpp:1] old line\n')
open(os.path.join(D_DIAG, 'diagnose_info_20260921-120349.4292'), 'w').write(DIAG)
open(os.path.join(D_DIAG, 'diagnose_info_20260921-130000.9999'), 'w').write(
    'I0921 13:00:00.000000 9999 x.cpp:1] next file\n')
os.symlink('diagnose_info_20260921-130000.9999',
           os.path.join(D_DIAG, 'diagnose.INFO'))
open(os.path.join(D_TASK, 'tasks_manager_info_20261020-111747.4186'), 'w').write(
    'Log file created at: 2026/10/20 11:17:47\n'
    'I1020 11:17:47.959247  4186 log.h:99] glog inited\n'
    'I1020 11:17:49.435701  4186 tasks_online_monitor.cpp:10] __supplementary_rate = 87\n'
    'I1020 11:17:50.270830  4186 log_save.cpp:49] 手自动切换, last:1, current:0\n'
    'W1020 11:18:03.455447  4186 log_save.cpp:49] 手自动切换, last:0, current:1\n')

JOB = os.path.join(TMP, 'job.py')
open(JOB, 'w', encoding='utf-8').write(GLOG_PY)
SPEC = [{"key": "diagnose", "label": "故障诊断", "short": "诊断", "dir": D_DIAG},
        {"key": "tasks_manager", "label": "任务管理", "short": "任务", "dir": D_TASK}]


def run(tc, win, lvs='', spec=None, cap=6000):
    t0, t1 = tc - win, tc + win
    cmd = [sys.executable, '-u', JOB, json.dumps(spec or SPEC),
           '%.3f' % t0, '%.3f' % t1, str(cap), lvs, '314572800']
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not r.stdout.strip():
        return {'ok': False, 'err': 'no output: ' + r.stderr[:200]}
    return json.loads(r.stdout.strip().splitlines()[-1])


j = run(epoch('2026-09-21 12:04:00'), 30)
ck('命中窗口内 8 行（6 条 + 2 续行）', j.get('nline') == 8, str(j.get('nline')))
ck('扫了 2 个候选文件', j.get('nfile') == 2, str(j.get('nfile')))
lv = [L['s'] for L in j['lines']]
ck('级别解析正确（含 E）', lv.count('E') == 1, str(lv))
ck('续行跟在报错后面', any(L['c'] == 1 for L in j['lines']))
ck('续行内容保留', 'at foo()' in ' '.join(L['x'] for L in j['lines']))
ck('按时间升序', all(j['lines'][i]['t'] <= j['lines'][i + 1]['t']
                     for i in range(len(j['lines']) - 1)))
ck('窗口外的行没进来', 'later line' not in ' '.join(L['x'] for L in j['lines'])
   or epoch('2026-09-21 12:04:30') <= epoch('2026-09-21 12:04:00') + 30)
ck('11:50 的老行没进来', 'old line' not in ' '.join(L['x'] for L in j['lines']))
ck('.INFO 软链已去重（picked 不含它）',
    all('diagnose.INFO' != p for p in j['dirs'][0]['picked']))
ck('来源标记是 diagnose', all(L['k'] == 'diagnose' for L in j['lines']))

j = run(epoch('2026-10-20 11:18:00'), 30, spec=[SPEC[1]])
ck('任务管理能取到 4 行', j.get('nline') == 4, str(j.get('nline')))
ck('中文行不乱码', '手自动切换' in ' '.join(L['x'] for L in j['lines']))

j = run(epoch('2026-09-21 12:20:00'), 60, spec=[SPEC[0]])
ck('空窗口时 nline=0', j.get('nline') == 0)
ck('空窗口给 before 提示', bool(j['dirs'][0].get('before')))
ck('空窗口给 after 提示', bool(j['dirs'][0].get('after')))
ck('before 早于起点', j['dirs'][0]['before']['t'] <= '2026-09-21 12:19:00')
ck('after 晚于终点', j['dirs'][0]['after']['t'] >= '2026-09-21 12:21:00')

j = run(epoch('2026-09-21 12:04:00'), 30, lvs='EF', spec=[SPEC[0]])
ck('仅错误：只留 E 及其续行', all(L['s'] in ('E', '') for L in j['lines']))
ck('仅错误：确实有 E', any(L['s'] == 'E' for L in j['lines']))

j = run(epoch('2026-09-21 12:04:00'), 30, spec=[SPEC[1]])
ck('任务管理在那段没数据 -> 0 行', j.get('nline') == 0)

j = run(epoch('2026-09-21 12:04:00'), 30, cap=3)
ck('超上限会截断', j.get('trunc') is True)
ck('截断后行数 = cap', len(j['lines']) == 3)

j = run(epoch('2026-09-21 12:04:00'), 30,
        spec=[{"key": "x", "label": "X", "short": "x", "dir": TMP + '/nope'}])
ck('目录不存在有 err', j['dirs'][0]['err'] == '目录不存在')

# 跨年：文件 12-31 启动，行里出现 01-01
D_Y = os.path.join(TMP, 'y')
os.makedirs(D_Y)
open(os.path.join(D_Y, 'y_info_20261231-235000.1'), 'w').write(
    'I1231 23:50:00.000000 1 a.cpp:1] end of year\n'
    'I0101 00:10:00.000000 1 a.cpp:2] new year\n')
j = run(epoch('2027-01-01 00:10:00'), 60,
        spec=[{"key": "y", "label": "Y", "short": "y", "dir": D_Y}])
ck('跨年那条能被取到', j.get('nline') == 1, str(j.get('nline')))
ck('跨年那条是 01-01 的', 'new year' in ' '.join(L['x'] for L in j.get('lines', [])))


# ───────────────────────── 服务端 ─────────────────────────
print('[服务端]')
ck('定义了 api_glog', 'def api_glog(self, b)' in SRC)
ck('do_POST 挂了 /api/glog', "/api/glog" in SRC)
# 目录现在由 GLOG_BASE 拼接而来（来源从 2 个扩到 20+ 个之后，
# 每行都写一长串根路径既啰嗦又容易拼错），所以按拼接后是否成立来断言。
_bm = re.search(r'^GLOG_BASE\s*=\s*[\'"]([^\'"]+)[\'"]', SRC, re.M)
_base = _bm.group(1) if _bm else ''
ck('GLOG_DIRS 含诊断与任务两个来源',
   _base == '/home/aoting/user_config/glogs'
   and "GLOG_BASE + '/diagnose'" in SRC
   and "GLOG_BASE + '/tasks_manager'" in SRC)
ck('未连接时有明确提示', '未连接机器，请先连接' in SRC)
ck('记录了 LAST_CONN', 'LAST_CONN' in SRC)
ck('复用/现开 SSH 的助手存在', 'def _ssh_for_job' in SRC)

# handler 分支不得引用未定义的全局名 —— s4 = SESSION 那类 bug 会让
# handler 一进来就抛异常，前端只看到 Failed to fetch，极难排查。
# 用 symtable 判作用域而不是自己收集赋值目标：手写收集会漏掉
# for k, v / 推导式 / except ... as e 这些绑定，全是误报。
import symtable

tree = ast.parse(SRC)
top = symtable.symtable(SRC, SERVER, 'exec')
mod_names = set(top.get_identifiers())
builtins_ok = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))
bad = []


def walk_st(st, prefix=''):
    for ch in st.get_children():
        nm = prefix + (ch.get_name() if ch.get_name() != 'top' else '')
        if ch.get_type() == 'class' and ch.get_name() == 'Handler':
            for mth in ch.get_children():
                for sym in mth.get_symbols():
                    if sym.is_global() and sym.is_referenced():
                        if sym.get_name() not in mod_names and sym.get_name() not in builtins_ok:
                            bad.append('Handler.%s -> %s' % (mth.get_name(), sym.get_name()))
        walk_st(ch, nm + '.')


walk_st(top)
ck('Handler 所有方法无未定义的全局名', not bad, str(sorted(set(bad))[:5]))
ck('该检查确实能抓到 SESSION 那类错（自检）',
   'SESSION' not in mod_names and ('s4 = SESSION' not in SRC))


# ───────────────────────── 前端 ─────────────────────────
print('[前端]')
P = open(PAGE, encoding='utf-8').read()
# 基准时间从「一个长串输入框」改成了年月日时分秒六个框（v1.1.3），
# 来源复选框也改成由 glBuildSrcs 动态生成，因此这里按新 UI 断言。
for i in ('gly', 'glmo', 'gld', 'glh', 'glmi', 'gls',
          'glwin', 'glunit', 'gllv', 'glgo',
          'glcopy', 'gldl', 'glst', 'glout', 'glnow', 'gllocal', 'cardgl'):
    ck('页面含 #%s' % i, ('id="%s"' % i) in P)
ck('页面含来源容器 #glsrcs', ('id="glsrcs"') in P)
ck('卡片在调试工具组内',
   P.index('id="cardgl"') > P.index('③ 调试工具')
   and P.index('id="cardgl"') < P.index('id="cardtm"'))
ck('时间不完整时给出提示', '基准时间没填完整' in P)
ck('未找到时给提示', '未找到该时间段的日志' in P)
ck('下载文件名带 glog_', "a.download = 'glog_'" in P)

if NODE:
    js = re.search(r'<script>(.*)</script>', P, re.S).group(1)
    harness = js + r'''
;(function(){
  S.lastData = {t: __T__};
  const out = {};
  // 基准时间改成六个框后不再有 glParse：这里改成填框再读。
  function setT(y,mo,d,h,mi,s){
    document.getElementById('gly').value = y;
    document.getElementById('glmo').value = mo;
    document.getElementById('gld').value = d;
    document.getElementById('glh').value = h;
    document.getElementById('glmi').value = mi;
    document.getElementById('gls').value = s;
    return glReadTime();
  }
  out.parse1 = setT('2026','10','20','11','18','0');
  out.parse2 = setT('2026','10','20','11','18','00');
  out.parse3 = setT('2026','10','20','11','18','30');
  out.parseBad = setT('2026','2','31','0','0','0');
  out.parseEmpty = setT('','','','','','');
  const t = out.parse1;
  const p = new Date(t * 1000);
  out.fmt = glFmt(t);
  // 前端解析必须与 Python 的 mktime 一致（本地时区）
  out.roundTrip = (p.getFullYear() === 2026 && p.getMonth() === 9
                   && p.getDate() === 20 && p.getHours() === 11
                   && p.getMinutes() === 18);

  const fake = {ok:true, nline:2, nfile:1, trunc:false, t0:0, t1:0,
    dirs:[{key:'diagnose', label:'故障诊断', short:'诊断', dir:'/x',
           total:9, picked:['a'], nline:2, err:'',
           before:{name:'a', t:'2026-09-21 12:03:49'}, after:null}],
    lines:[{t:1, k:'diagnose', s:'I', c:0, x:'I0921 12:03:49.1  1 a.cpp:1] hello'},
           {t:2, k:'diagnose', s:'E', c:0, x:'E0921 12:03:50.1  1 a.cpp:2] boom'}]};
  glRender(fake, t, 300);
  out.hitText = document.getElementById('glout').value;
  out.hitHasTag = out.hitText.indexOf('[diagnose] ') >= 0;
  out.hitHead = out.hitText.indexOf('# 时间窗') === 0;
  out.hitCount = (out.hitText.match(/\[diagnose\]/g) || []).length;

  const none = {ok:true, nline:0, nfile:1, trunc:false,
    dirs:[{key:'diagnose', label:'故障诊断', short:'诊断', dir:'/x',
           total:9, picked:[], nline:0, err:'',
           before:{name:'b1', t:'2026-09-21 12:03:49'},
           after:{name:'b2', t:'2026-09-21 13:00:00'}}], lines:[]};
  glRender(none, t, 300);
  out.noneText = document.getElementById('glout').value;
  out.noneHasWhy = out.noneText.indexOf('该时间段内没有任何日志行') >= 0;
  out.noneHasBefore = out.noneText.indexOf('b1') >= 0;
  out.noneHasAfter = out.noneText.indexOf('b2') >= 0;

  const errd = {ok:false, err:'未连接机器，请先连接'};
  glRender(errd, t, 300);
  out.errText = document.getElementById('glst').textContent;
  out.errCleared = document.getElementById('glout').value === '';
  require("fs").writeFileSync("/tmp/glt/out.json", JSON.stringify(out));
  process.exit(0);
})();
'''
    stub = r'''
const _els = {};
global.document = {
  getElementById: function(id){
    if (!_els[id]) _els[id] = {id:id, value:'', textContent:'', style:{},
                               checked:true, disabled:false, className:'',
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
global.setInterval = function(){ return 0; };   // 页面有轮询定时器，不桩掉 node 不会退出
global.clearInterval = function(){};
global.fetch = function(){ return new Promise(function(){}); };
'''
    # 去掉页面里那些依赖真实 DOM 的顶层绑定，只留纯函数
    lines = harness.split('\n')
    keep = []
    for ln in lines:
        if re.match(r"^\$\('gl(go|now|copy|dl)'\)\.onclick", ln):
            continue
        keep.append(ln)
    # 写进本次的临时目录，而不是硬编码 /tmp/glt ——
    # 那个路径在换一台机器跑时不一定存在，测试会直接崩而不是报失败。
    pt = os.path.join(TMP, 'page_test.js')
    oj = os.path.join(TMP, 'out.json')
    open(pt, 'w', encoding='utf-8').write(
        stub + '\n'
        + '\n'.join(keep).replace(
            '__T__', str(int(epoch('2026-10-20 11:18:00'))))
        .replace('/tmp/glt/out.json', oj))
    try:
        os.remove(oj)
    except Exception:
        pass
    r = subprocess.run([NODE, pt], capture_output=True,
                       text=True, timeout=60)
    o = {}
    if os.path.isfile(oj):
        try:
            o = json.loads(open(oj, encoding='utf-8').read())
        except Exception as e:
            ck('node 输出可解析', False, str(e)[:200])
    else:
        ck('node 跑通前端渲染', False, r.stderr[:300])
    if o:
        if o:
            ck('六框读出正确时间戳', o['parse1'] == int(epoch('2026-10-20 11:18:00')), str(o.get('parse1')))
            ck('秒可写两位', o['parse2'] == int(epoch('2026-10-20 11:18:00')))
            ck('秒为 30 时也正确', o['parse3'] == int(epoch('2026-10-20 11:18:30')))
            ck('2 月 31 日返回 null', o['parseBad'] is None)
            ck('全空返回 null', o['parseEmpty'] is None)
            ck('glFmt 与解析互为逆运算', o['fmt'] == '2026-10-20 11:18:00' and o['roundTrip'])
            ck('命中时带来源前缀', o['hitHasTag'])
            ck('命中时首行是时间窗头', o['hitHead'])
            ck('命中 2 行 -> 2 个前缀', o['hitCount'] == 2, str(o.get('hitCount')))
            ck('未找到时说明原因', o['noneHasWhy'])
            ck('未找到时给出早于起点的文件', o['noneHasBefore'])
            ck('未找到时给出晚于终点的文件', o['noneHasAfter'])
            ck('服务端错误时清空输出', o['errCleared'])
            ck('服务端错误时显示 err', '未连接机器' in (o.get('errText') or ''))
    else:
        ck('node 跑通前端渲染', False, r.stderr[:300])
else:
    print('  skip 前端实跑（无 node）')

shutil.rmtree(TMP, ignore_errors=True)
print('\n通过 %d，失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
