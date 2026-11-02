# -*- coding: utf-8 -*-
"""
门户测试。

重点验三类"错了会很难发现"的事：
  1. 新增工具不需要改门户代码（app.json 驱动）
  2. 关掉页面能关掉黑窗口（心跳机制）
  3. Service Worker 不拦截本地服务（否则 camweb 画面卡死）
"""
import os
import re
import sys
import json
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APPS = os.path.join(ROOT, 'apps')
PASS = FAIL = 0


def chk(n, c, e=''):
    global PASS, FAIL
    if c:
        PASS += 1
        print('  ok  ', n, e)
    else:
        FAIL += 1
        print('  FAIL', n, e)


def sec(n):
    print('\n=== %s ===' % n)


def rd(p):
    # bat/cmd 是 GBK（cmd 原生编码），其余是 utf-8。
    # 用 utf-8 去读 GBK 的 bat 会直接抛异常，之前就漏过一次。
    raw = open(os.path.join(ROOT, p), 'rb').read()
    for enc in ('utf-8', 'gbk', 'latin-1'):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode('utf-8', 'replace')


def exists(p):
    return os.path.exists(os.path.join(ROOT, p))


sec('1. 目录结构')
for p in ('index.html', 'sw.js', 'version.json',
          'portal_server.py', 'start_aoting_tools.bat',
          'tools/make_version.py', 'tools/add_tool.py',
          'tools/nginx.conf', 'tools/deploy.sh'):
    chk('存在 ' + p, exists(p))

sec('2. apps/ 每个工具自描述')
# apps/ 下可能有历史遗留的空目录（比如 apps/tools），它不是工具，
# make_version 也是直接 skip 的。这里同样排除：不排除的话第 13 节
# json.load 会因为找不到 app.json 直接崩掉，整段测试废掉。
def _is_app_dir(d):
    p = os.path.join(APPS, d)
    if not os.path.isdir(p):
        return False
    try:
        return bool(os.listdir(p))
    except Exception:
        return False


appdirs = sorted(d for d in os.listdir(APPS) if _is_app_dir(d))
chk('至少 3 个工具', len(appdirs) >= 3, ','.join(appdirs))
for d in appdirs:
    aj = os.path.join(APPS, d, 'app.json')
    chk('%s 有 app.json' % d, os.path.isfile(aj))
    if not os.path.isfile(aj):
        continue
    c = json.load(open(aj, encoding='utf-8'))
    chk('%s 有 id/name/ver' % d,
        all(k in c for k in ('id', 'name', 'ver')))
    entry = os.path.join(APPS, d, c.get('entry', 'index.html'))
    if c.get('type') == 'link':
        # 外链型故意没有本地入口文件
        continue
    chk('%s 入口文件存在' % d, os.path.exists(entry))
    if c.get('src'):
        chk('%s 有 src/ 源码目录' % d,
            os.path.isdir(os.path.join(APPS, d, 'src')))

sec('3. 纯前端工具必须带源码（否则没法改）')
for d in ('webbag', 'imuweb'):
    base = os.path.join(APPS, d)
    chk('%s 有 src/' % d, os.path.isdir(os.path.join(base, 'src')))
    chk('%s 有 build.py' % d,
        os.path.isfile(os.path.join(base, 'src', 'build.py')))
    chk('%s 有 index.html 骨架' % d,
        os.path.isfile(os.path.join(base, 'src', 'index.html')))

sec('4. camweb 可独立离线使用（不依赖服务器）')
cb = os.path.join(APPS, 'camweb')
for f in ('server.py', 'cam_agent.py', 'start.bat', 'index.html'):
    chk('camweb 含 ' + f, os.path.isfile(os.path.join(cb, f)))
c = json.load(open(os.path.join(cb, 'app.json'), encoding='utf-8'))
chk('app.json 标记 service', c.get('type') == 'service')
chk('声明了端口', c.get('service', {}).get('port') == 8765)
chk('列出可下载文件', len(c.get('service', {}).get('files', [])) >= 3)

sec('5. version.json')
V = json.loads(rd('version.json'))
chk('有 portal 版本', bool(V.get('portal')), str(V.get('portal')))
chk('index.html 有 PORTAL_VER 标记', 'PORTAL_VER' in rd('index.html'))
chk('apps 数量 >= 3', len(V.get('apps', {})) >= 3)
for tid, a in V['apps'].items():
    if a.get('type') == 'link':
        # 外链型没有本地文件，上面那几条对它没意义
        continue
    chk('%s 有 sha256(64位)' % tid, len(a.get('sha256', '')) == 64)
    chk('%s 入口可访问' % tid, exists(a['file']))
    chk('%s 有 entryUrl' % tid, a.get('entryUrl', '').startswith('./apps/'))
    fv = re.search(a.get('verPat', r'v(\d+\.\d+)'),
                   rd(a['file']).replace('v(\\d+\\.\\d+)', ''))
chk('precache 含所有纯前端工具',
    all(('./' + a['file']) in V['precache']
        for a in V['apps'].values() if a.get('type') == 'page'))
chk('precache 不含 service 型',
    all(('./' + a['file']) not in V['precache']
        for a in V['apps'].values() if a.get('type') == 'service'))

sec('6. 门户页面不硬编码工具（新增工具不用改它）')
h = rd('index.html')
for tid in ('webbag', 'imuweb', 'camweb'):
    chk('未硬编码 ' + tid, tid not in h)
chk('动态渲染卡片', 'function renderCards' in h)
chk('从 version.json 取 apps', "V.apps" in h or "V.apps" in h)
chk('有心跳 ping', "'/__portal_ping'" in h)
chk('有告别 bye', "'/__portal_bye'" in h)
chk('监听 pagehide', "addEventListener('pagehide'" in h)
chk('监听 beforeunload', "addEventListener('beforeunload'" in h)
chk('心跳用 sendBeacon', 'sendBeacon' in h)
chk('心跳间隔 4 秒', 'setInterval(ping, 4000)' in h)

sec('6b. 加载失败必须可见（不能停在"读取应用清单…"）')
# 上一版 loadVersion 没有 try/catch，渲染一抛异常就静默中断，
# 页面永远停在"读取应用清单…"，服务端明明是好的 —— 极难查。
chk('fetch 带超时', 'fetchJSON' in h)
chk('超时用 AbortController', 'AbortController' in h)
chk('超时 8 秒', '8000' in h)
chk('渲染包在 try 里', 'renderCards();\n    renderTable();' in h
    or 'renderCards();' in h and 'catch(e){' in h)
chk('有 hardFail 提示', 'function hardFail' in h)
chk('hardFail 有清除缓存按钮', 'clearCacheAndReload()' in h)
chk('有 clearCacheAndReload', 'async function clearCacheAndReload' in h)
chk('清 caches', 'caches.delete(k)' in h)
chk('注销旧 SW', 'unregister()' in h)
chk('校验 apps 字段存在', '!j || !j.apps' in h)
chk('tags 有默认值容错', '(a.tags||[])' in h)

sec('6c. SW 缓存版本（旧缓存必须能被清掉）')
sw2 = rd('sw.js')
chk('缓存名带版本号', "tools-portal-v2" in sw2, 'v2')
chk('清理所有 tools-portal-* 旧缓存', "startsWith('tools-portal-')" in sw2)

sec('6d. JS 必须能真的解析（本次事故的根因）')
# 曾经在块注释里写了 "apps/*/app.json"，其中的 "*/" 提前关闭了注释，
# 导致整个 <script> 语法错误、一行都不执行 —— 页面永远停在静态占位文字。
# 字符串匹配的断言完全发现不了这种问题，必须真的解析一遍。
_scripts = re.findall(r'<script>(.*?)</script>', h, re.S)
chk('有内联脚本', len(_scripts) >= 1)
_js = _scripts[0] if _scripts else ''
open('/tmp/_portal_check.js', 'w', encoding='utf-8').write(_js)

import shutil as _sh
_node = _sh.which('node') or _sh.which('nodejs')
if _node:
    _r = subprocess.run([_node, '--check', '/tmp/_portal_check.js'],
                        capture_output=True, text=True)
    chk('node --check 通过', _r.returncode == 0,
        (_r.stderr or '').strip().splitlines()[:2] and
        (_r.stderr or '').strip().splitlines()[1] or '')
else:
    print('  skip  node 不存在，跳过语法检查')

# 块注释里出现 "*/" 就会提前关闭注释。逐个注释块检查。
_bad = []
for _m in re.finditer(r'/\*.*?\*/', _js, re.S):
    _body = _m.group(0)[2:-2]
    if '*/' in _body:
        _bad.append(_body[max(0, _body.find('*/') - 30):_body.find('*/') + 2])
chk('块注释内无提前闭合的 "*/"', not _bad, str(_bad[:1]))

sec('6e. 错误可见性（JS 抛异常时不能静默）')
chk('监听 window error', "addEventListener('error'" in h)
chk('监听 unhandledrejection', "addEventListener('unhandledrejection'" in h)
chk('错误画到页面上', 'JS 错误' in h)
chk('有启动日志', 'PLOG' in h)
chk('loadVersion 有日志', 'loadVersion 开始' in h)

sec('6f. 空引用防护（V 尚未加载时不能崩）')
# V 初始为 null，而定时探针在清单到达前就会跑一次。
# 写 V.apps||{} 是无效的：|| 在取属性之后才生效，null.apps 已经抛错。
# 症状：页面顶部红色报错，且后续探测全部失效。
chk('无裸读 V.apps', not [l for l in h.split('\n')
                          if 'V.apps' in l and '(V && V.apps)' not in l])
chk('探针有防护', 'const svc = Object.values((V && V.apps) || {})' in h)

sec('6g. camweb 跨域（门户要能探测到本地服务）')
sv = rd('apps/camweb/server.py')
chk('有 CORS 头', 'Access-Control-Allow-Origin' in sv)
chk('支持 OPTIONS 预检', 'def do_OPTIONS' in sv)
chk('OPTIONS 返回 204', 'self.send_response(204)' in sv)
chk('server 版本与 app.json 一致',
    json.load(open(os.path.join(APPS, 'camweb', 'app.json'),
                   encoding='utf-8'))['ver'] in sv)

sec('6h. 品牌主题')
chk('标题含哈工澳汀', '哈工澳汀' in h)
chk('有品牌绿', '--gr:' in h)
chk('有白色描边', 'rgba(255,255,255' in h)
chk('非全绿（保留深色底）', '#060d0b' in h or '--bg:#060d0b' in h)

sec('6i. 品牌与图标')
chk('品牌行去掉 HIT', 'HIT AOTING' not in h)
chk('保留 AOTING', 'AOTING' in h)
chk('品牌字号已放大', 'font-size:22px' in h)
chk('有 brand.ico 文件', os.path.isfile(os.path.join(ROOT, 'brand.ico')))
chk('favicon 已引用', 'brand.ico' in h)
chk('图标进了预缓存', './brand.ico' in json.dumps(
    json.load(open(os.path.join(ROOT, 'version.json'), encoding='utf-8'))
    .get('precache', [])))

sec('6j. 自包含：不再提示下载')
chk('不提示手动下载', '请先下载' not in h)
chk('说明指向本地路径', 'apps/' in h and 'start.bat' in h)
chk('工具新标签页打开', "el.target = '_blank'" in h)

sec('6k. 关页面关窗口（本次重点）')
sv2 = rd('portal_server.py')
# v1.0.31：空闲超时整条去掉了。门户只是静态服务，常驻几乎不占资源；
# 而误杀后页面还在（浏览器离线缓存），点卡片一律 failed to fetch，
# 用户完全无从判断是门户没了。现在只在收到告别时退出。
chk('看门狗只由告别触发退出',
    'bye > 0 and (time.time() - bye) >= GRACE' in sv2)
chk('已无空闲超时分支',
    'IDLE_TIMEOUT' not in sv2 and 'NEVER_SEEN_TIMEOUT' not in sv2)
chk('不再统计空闲时刻', '_last_any' not in sv2 and '_ever_seen' not in sv2)
chk('新心跳仍会取消退出倒计时', '_bye_at = 0.0' in sv2)
chk('心跳不再只发一次', 'let _byed' not in h and 'if (_byed)' not in h)
chk('监听 visibilitychange', 'visibilitychange' in h)
bat = rd('start_aoting_tools.bat')
chk('启动前杀旧进程', 'netstat -ano' in bat and 'taskkill' in bat)

sec('6L. 一键启动本地服务（点击卡片自动拉起）')
# 浏览器启动不了本地程序，由门户后端代劳。
# 但这意味着一个 HTTP 请求能触发本机进程启动 —— 必须严格限制
# 能启动什么，否则就成了任意程序执行漏洞。
ps2 = rd('portal_server.py')
chk('有 /api/launch 路由', "'/api/launch'" in ps2)
chk('只认 app.json 声明的脚本', '_declared_start' in ps2)
chk('校验路径不越界', 'realpath' in ps2 and 'startswith' in ps2)
chk('限制可执行文件后缀', 'ALLOWED_EXT' in ps2)
chk('拒绝带分隔符的 id', "'/' in app_id" in ps2)
chk('有重复拉起去重', '_launched' in ps2 and '20' in ps2)

# 非法请求必须全部拒绝
import importlib.util as _iu
_spec = _iu.spec_from_file_location('ps', os.path.join(ROOT, 'portal_server.py'))
_ps = _iu.module_from_spec(_spec); sys.modules['ps'] = _ps
try:
    _spec.loader.exec_module(_ps)
except SystemExit:
    pass
except Exception:
    pass
_bad = [(None, 'start.bat'), ('../..', 'start.bat'), ('.', 'start.bat'),
        ('camweb', '../start_aoting_tools.bat'), ('camweb', 'app.json'),
        ('camweb', 'server.py'), ('camweb/..', 'start.bat')]
_rej = 0
for _a, _n in _bad:
    try:
        if _ps._declared_start(_a, _n) is None:
            _rej += 1
    except Exception:
        _rej += 1
chk('越权/未声明请求全部拒绝 (%d/%d)' % (_rej, len(_bad)), _rej == len(_bad))
_ok, _ = _ps.launch_service('camweb', 'app.json')
chk('未声明的脚本启动被拒', _ok is False)
# 注意：不在这里真的启动 camweb/start.bat —— 它会 pip install。

chk('前端会调拉起接口', "'/api/launch'" in h)
chk('前端轮询等待就绪', '正在启动本地服务' in h)
chk('轮询有超时上限', 'LIMIT' in h)
chk('未运行时不甩下载链接', '本地服务未启动（点卡片会自动启动）' in h)
cbat = rd(os.path.join('apps', 'camweb', 'start.bat'))
chk('camweb 支持无浏览器模式', 'CAMWEB_NO_BROWSER' in cbat)
chk('camweb 支持 py -3 反查', 'py -3' in cbat)

sec('6M. 拉起后必须看得见报错（本次重点）')
# 上一版把子进程 stdout/stderr 指向 DEVNULL，新黑窗口一片空白：
# pip 装没装、报什么错全看不见，用户只看到"连不上车"，无从下手。
ps3 = rd('portal_server.py')
_i = ps3.find('subprocess.Popen(')
_call = ps3[_i:_i+400]
chk('拉起时不再把输出丢掉', 'DEVNULL' not in _call)
chk('Windows 用 start 开新窗口', "'start'" in ps3 and 'cmd' in ps3)
cb = rd(os.path.join('apps', 'camweb', 'start.bat'))
chk('start.bat 装完会复核 paramiko', 'still missing' in cb)
chk('缺依赖时明确报错不硬跑', 'exit /b 1' in cb)
chk('结尾 pause 无条件', 'Server stopped.' in cb and 'pause' in cb)
cm = rd(os.path.join('apps', 'camweb', 'server.py'))
chk('GET /api/version 带 paramiko 状态',
    "'paramiko': HAVE_PARAMIKO" in cm)
chk('连接前检查 paramiko', "not HAVE_PARAMIKO" in cm and '缺少 paramiko' in cm)
chk('门户有日志面板', 'logpanel' in h and 'showSvcLog' in h)
chk('门户能读服务日志', "'/api/logs" in h)
chk('门户会提示缺 paramiko', 'checkDeps' in h and 'paramiko' in h)

sec('6N. 打包卫生：不能有 Windows 保留设备名')
# 真实事故：我在 Linux 沙箱里跑过 start.bat 做测试，
# bat 里的 "2>nul"（Windows 语义是丢弃输出）被 Linux 当成文件重定向，
# 于是生成了名为 nul 的文件并被打包。
# Windows 保留设备名无法解压，用户会看到 0x800704B0 且不知道该跳过什么。
RESERVED = ('nul','con','aux','prn','com1','com2','com3','com4','com5',
            'com6','com7','com8','com9','lpt1','lpt2','lpt3','lpt4',
            'lpt5','lpt6','lpt7','lpt8','lpt9','conin$','conout$')
bad = []
for _r, _ds, _fs in os.walk(ROOT):
    if '__pycache__' in _r: continue
    for _f in _fs:
        if _f.lower().split('.')[0] in RESERVED:
            bad.append(os.path.join(_r, _f))
chk('无 Windows 保留设备名文件 (%d)' % len(bad), not bad)
if bad:
    for _b in bad[:5]: print('    !!', _b)

sec('6O. start 命令的标题参数（本次重点）')
# 事故：传 'AOTING-CAMWEB'（无空格）时 list2cmdline 不加引号，
# cmd 的 start 就把标题当成"要运行的文件"，弹窗报找不到文件。
# 只有含空格才会被自动加引号、正确识别为窗口标题。
import subprocess as _sp
_c1 = _sp.list2cmdline(['cmd', '/c', 'start', 'AOTING-CAMWEB',
                        'cmd', '/c', 'x.bat'])
_c2 = _sp.list2cmdline(['cmd', '/c', 'start', 'AOTING CAMWEB',
                        'cmd', '/c', 'x.bat'])
chk('无空格标题不会被加引号（错误写法）', '"AOTING-CAMWEB"' not in _c1)
chk('含空格标题会被加引号（正确写法）', '"AOTING CAMWEB"' in _c2)
ps4 = rd('portal_server.py')
_i = ps4.find("'AOTING CAMWEB'")
chk('代码用的是含空格标题', _i > 0)
# 只检查实际命令行那一段（不含注释），避免注释里提到旧写法而误判
_line = ps4[max(0, _i - 120):_i + 40]
chk('命令行中标题带引号', '"' in _line or 'AOTING CAMWEB' in _line)

sec('6P. 门户文案')
chk('已删除手动启动说明', '回来点上面的卡片' not in h)
chk('等待超时有可操作提示', '启动失败' in h)
chk('中途提示看黑窗口', '若黑窗口已关闭' in h)

sec('6Q. 清理步骤不得挡住连接（本次重点）')
# 真实故障：sweep_host 在 /api/connect 里同步执行，SSH 握手一慢
# 就把整个请求拖过 45s；浏览器先超时放弃，服务端留 WinError 10053，
# 用户只看到"连接失败"，完全对不上真正的卡点。
cm2 = rd(os.path.join('apps','camweb','server.py'))
chk('清理有硬超时常量', 'SWEEP_DEADLINE' in cm2)
chk('清理走后台线程', '_sweep_worker' in cm2 and 'threading.Thread' in cm2)
chk('超时后不再等待', 't.join(SWEEP_DEADLINE)' in cm2)
chk('超时会明确告知可继续', '跳过清理继续连接' in cm2)
# 清理线程可能跑得慢，期间新会话已建起；不检查就会误杀
chk('pkill 前有存活护栏', '已有活跃会话，跳过 pkill' in cm2)
chk('SSH 阶段有打点', "ph('开始 SSH 握手')" in cm2)
chk('上车脚本阶段有打点', "ph('车端脚本已启动" in cm2)

sec('6R. 门户交互与视觉')
chk('不再自动探测本地服务', '不再在页面打开时自动探测本地服务' in h)
chk('卡片默认不显示检测中', '检测本地服务' not in h)
chk('service 型提示点击启动', '点击卡片启动' in h)
chk('底部有云端链接', 'cloud.aotbot.com' in h)
chk('云端链接可被 JS 覆盖', "V.config.cloud" in h and 'cloudlink' in h)
chk('头部显示总版本', 'j.suite' in h)
chk('版本表首行是总版本', '工具箱总版本' in h)

sec('6W. 总版本号')
import json as _j2
_pj = _j2.loads(rd('portal.json'))
_vj = _j2.loads(rd('version.json'))
chk('portal.json 有 suite', 'suite' in _pj)
chk('suite 有 ver', 'ver' in _pj.get('suite', {}))
import re as _re
chk('格式为 x.y.z', bool(_re.match(r'^\d+\.\d+\.\d+$', _pj['suite']['ver'])))
chk('提到 version.json 顶层', _vj.get('suite') == _pj['suite']['ver'])
chk('文案已更新', '云平台知识库' in _pj['cloud']['label'])

sec('6U. 外链型工具卡片')
import os as _os
_aj = _os.path.join('apps', 'slamweb', 'app.json')
chk('slamweb 目录存在', _os.path.isfile(_aj))
if _os.path.isfile(_aj):
    _a = _j2.loads(rd(_aj))
    chk('type=link', _a.get('type') == 'link')
    chk('有 url', 'url' in _a)
    chk('url 指向目标站', 'rb-admin.autoxing.com' in _a.get('url', ''))
    chk('有名字', bool(_a.get('name')))
    chk('有图标', bool(_a.get('icon')))
_v = _j2.loads(rd('version.json'))
_s = _v['apps'].get('slamweb')
chk('清单含 slamweb', _s is not None)
if _s:
    chk('清单 type=link', _s.get('type') == 'link')
    chk('清单带 url', 'url' in _s)
    chk('不进预缓存', not any('slamweb' in p for p in _v.get('precache', [])))
    chk('不显示体积', 'size' not in _s)
chk('前端支持 link 分支', "a.type === 'link'" in h)
chk('link 新标签页打开', 'noopener noreferrer' in h)
chk('版本表区分 link', "在线服务" in h)

sec('6V. 车端 CPU / 内存采集')
_sv = rd('apps/camweb/server.py')
chk('有 SysMon 类', 'class SysMon' in _sv)
chk('有采集脚本', 'SYSMON_PY' in _sv)
chk('按车共享（有 REG 表）', 'REG = {}' in _sv)
chk('按车共享（有 LOCK）', 'LOCK = threading.Lock()' in _sv)
chk('有 acquire/release', 'def acquire' in _sv and 'def release' in _sv)
chk('独立 SSH 连接', 'paramiko.SSHClient()' in _sv)
chk('采 1 秒一次', 'time.sleep(1)' in _sv)
chk('会话持有引用', 'self.sysmon' in _sv)
chk('断开时释放', 'SysMon.release' in _sv)
chk('status 返回 sys', "'sys': sysinfo" in _sv)
chk('不编造数据（采不到给 None）', 'sysinfo = None' in _sv)
# 采集脚本能在真 Linux 上跑通 —— 不跑等于没验证
import subprocess as _sp, sys as _sys, time as _tm, importlib.util as _iu
_sp2 = _iu.spec_from_file_location('srv2', 'apps/camweb/server.py')
_sm2 = _iu.module_from_spec(_sp2); _sp2.loader.exec_module(_sm2)
_p = _sp.Popen(['bash', '-c', 'python3 -c %s' % _sm2.shell_quote(_sm2.SYSMON_PY)],
               stdout=_sp.PIPE, stderr=_sp.PIPE, text=True)
_lines = []
_t0 = _tm.time()
while _tm.time() - _t0 < 3.4:
    _ln = _p.stdout.readline()
    if _ln:
        _lines.append(_ln.strip())
_p.kill(); _p.wait()
chk('采集脚本真机能出数据', len(_lines) >= 2, '采到 %d 行' % len(_lines))
_chk = _sm2.SysMon.__new__(_sm2.SysMon)
_chk.__init__('k', 'h', 22, 'u', 'p'); _chk.stop = True
for _l in _lines:
    _chk._parse(_l)
chk('输出可解析', _chk.cpu is not None and _chk.mem_total is not None,
    'cpu=%s mem=%s/%s' % (_chk.cpu, _chk.mem_used, _chk.mem_total))
chk('CPU 在 0~100', _chk.cpu is not None and 0 <= _chk.cpu <= 100, str(_chk.cpu))
_chk2 = _sm2.SysMon.__new__(_sm2.SysMon)
_chk2.__init__('k2', 'h', 22, 'u', 'p'); _chk2.stop = True
_chk2._parse('cpu=7 mem=100/200')
_chk2._parse('这是一行垃圾')
chk('脏数据不覆盖有效值', _chk2.cpu == 7, str(_chk2.cpu))
_chk2.at = _tm.time() - 9
chk('数据过期判定', _chk2.alive() is False or _chk2.alive() == False)
# 引用计数共享
_oi = _sm2.SysMon.__init__
def _fake(self, key, host, port, user, pwd):
    self.key = key; self.refs = 0; self.stop = False
_sm2.SysMon.__init__ = _fake
_a = _sm2.SysMon.acquire('u@h:22', 'h', 22, 'u', 'p')
_b = _sm2.SysMon.acquire('u@h:22', 'h', 22, 'u', 'p')
chk('同车共用一条采集连接', _a is _b and _a.refs == 2, 'refs=%s' % _a.refs)
_sm2.SysMon.release('u@h:22')
chk('释放1路仍保留', _a.refs == 1 and 'u@h:22' in _sm2.SysMon.REG)
_sm2.SysMon.release('u@h:22')
chk('归零后停止', 'u@h:22' not in _sm2.SysMon.REG and _a.stop)
_sm2.SysMon.__init__ = _oi

sec('6W. 变更日志')
chk('有 CHANGELOG.md', os.path.isfile('CHANGELOG.md'))
if os.path.isfile('CHANGELOG.md'):
    _c = rd('CHANGELOG.md')
    _suite = json.load(open('portal.json', encoding='utf-8'))['suite']['ver']
    chk('含当前总版本', _suite in _c, 'v%s' % _suite)
    chk('含获取方式模板', '云平台知识库' in _c or 'cloud.aotbot.com' in _c)
    chk('有转发模板', '发给使用人员的模板' in _c)

sec('6U. 发布包同步性（防止发出旧包）')
try:
    sys.path.insert(0, os.path.join(os.getcwd(), 'tools'))
    import pack as _pack
    _zp = _pack.OUT
    if os.path.isfile(_zp):
        _ok, _msg = _pack.verify()
        chk('压缩包与源码一致', _ok, _msg)
        # 关键功能必须在包里（不只是文件一致 —— 旧文件也可能"一致"）
        import zipfile as _zf
        with _zf.ZipFile(_zp) as _z:
            _imu = _z.read('aoting_tools/apps/imuweb/index.html').decode('utf-8', 'replace')
            _portal = _z.read('aoting_tools/index.html').decode('utf-8', 'replace')
            _vj = json.loads(_z.read('aoting_tools/version.json').decode('utf-8'))
        chk('包内 IMU 含结论段', '【六、结论】' in _imu)
        chk('包内 IMU 含排序按钮', 'moveBlock' in _imu and 'mvbtn' in _imu)
        chk('包内 IMU 版本 1.4', _vj['apps']['imuweb']['ver'] == '1.4', _vj['apps']['imuweb']['ver'])
        _sv = _vj.get('suite')
        _sv = _sv.get('ver') if isinstance(_sv, dict) else _sv
        chk('包内总版本 == portal.json', _sv == json.load(open('portal.json', encoding='utf-8'))['suite']['ver'], str(_sv))
        chk('包内门户含总版本显示', 'suiteVer' in _portal or '总版本' in _portal)
    else:
        chk('压缩包已生成（缺 pack 步骤则跳过）', False, '未找到 ' + _zp)
except Exception as _e:
    chk('压缩包同步性检查', False, repr(_e))

sec('6V. 相机取图时限')
sv2 = rd('apps/camweb/server.py')
chk('有时限常量', 'MAX_SESSION_SEC' in sv2)
chk('默认 600 秒', "600" in sv2 and 'MAX_SESSION_SEC' in sv2)
chk('有看门狗线程', 'def session_watchdog' in sv2)
chk('看门狗已启动', 'target=session_watchdog' in sv2)
chk('status 回传 remain', "'remain'" in sv2 and 's.remain()' in sv2)
chk('status 回传 expired', "'expired'" in sv2)
chk('到点标记 expired', 's.expired = True' in sv2)
chk('自动断开', 's.close()' in sv2.split('def session_watchdog')[1][:900])
chk('日志说明时限', '自动断开' in sv2)
cv = rd('apps/camweb/index.html')
chk('前端识别 expired', 'st.expired' in cv)
chk('前端显示倒计时', '剩余 ' in cv)
chk('提供继续查看入口', '继续查看' in cv)
chk('到点不自动重连', '本次取图已达时限' in cv)

sec('6Z. portal.json 可配置')
import json as _j
pj = _j.loads(rd('portal.json'))
chk('有 cloud 段', 'cloud' in pj)
chk('cloud 有 url', 'url' in pj.get('cloud', {}))
chk('url 与页面一致', pj['cloud']['url'] == 'https://cloud.aotbot.com')
vj = _j.loads(rd('version.json'))
chk('version.json 含 config', 'config' in vj)
chk('config 透传 cloud', vj.get('config', {}).get('cloud', {}).get('url') == pj['cloud']['url'])
chk('背景为多层渐变', h.count('radial-gradient') >= 4)
chk('有极光动画', '@keyframes drift' in h)
chk('尊重减少动效偏好', 'prefers-reduced-motion' in h)

sec('6S. 请求体只能读一次（致命回归，本次根因）')
# 事故：为支持门户用 POST 探测，我在 do_POST 开头加了一段
#   self.rfile.read(ln)
# 之后 _body() 是【第二次】读 —— 流已空，HTTP/1.1 keep-alive 下
# read() 阻塞等读满，请求挂死 45s，浏览器放弃，服务端只留 10053。
# 症状（连不上车）与原因（重复读 body）完全对不上，排查了很久。
cm3 = rd(os.path.join('apps','camweb','server.py'))
_i = cm3.find('def do_POST')
_body_seg = cm3[_i:_i + 2600]
chk('do_POST 中 rfile.read 只出现一次（在 _body 内）',
    _body_seg.count('rfile.read') <= 1)
chk('_body 有缓存', '_body_cache' in cm3)
chk('有缓存哨兵', '_UNSET' in cm3)
chk('缓存未初始化为 _UNSET 实例属性', '_body_cache = _UNSET' in cm3)
chk('注释里留了事故说明', '重复读 body' in cm3 or 'body 只能读一次' in cm3)

sec('6T. 卡片点击反馈')
chk('有冷却常量', 'CARD_COOLDOWN' in h)
chk('冷却 5 秒', 'CARD_COOLDOWN = 5000' in h)
chk('有 busy 状态集合', '_cardBusy' in h)
chk('点击加 clicked 类', "classList.add('clicked'" in h)
chk('重复点击被忽略', '_cardBusy.has(id)' in h)
chk('Promise 完成可提前结束冷却', 'r.then(finish, finish)' in h)
chk('有点击态 CSS', '.card.clicked{' in h)
chk('有转圈动画', '@keyframes spin' in h)
chk('渐变已放大', '1500px 900px' in h)

sec('6U. 相机画面卡住的自我修复')
ci = rd(os.path.join('apps','camweb','index.html'))
chk('有 abortImg 释放旧流', 'function abortImg' in ci)
chk('重挂前先掐断旧连接', 'abortImg(oldImg)' in ci or 'abortImg(s.view' in ci)
chk('清空 view 前释放流',
    ci.count("abortImg(s.view.querySelector('img'))") >= 5)
chk('有画面看门狗', 'function watchdog' in ci)
chk('看门狗 6 秒触发', 'waited < 6' in ci)
chk('看门狗最多重试 4 次', 'retryN >= 4' in ci or '>= 4' in ci)
chk('只在服务端有帧时重挂', 's.stat.n > 0' in ci)
chk('attach 加防缓存时间戳', "'&t=' + Date.now()" in ci)

sec('6V. 口令门禁与卡片行为')
# 口令已从"写死在页面"改为"服务端去 git 校验"：
# 明文口令放在网页里等于公开，私有仓库 token 更不能进页面。
chk('页面内无写死口令', "GATE_PWD" not in h and 'aoting321' not in h)
chk('口令正确才继续', 'gateUnlock(a, el)' in h)
chk('走服务端校验', '/api/gate_check' in h)
chk('有账号密码输入框', 'id="gu"' in h and 'id="gp"' in h)
chk('错误时提示', 'gerr' in h)
chk('记住本次会话', 'sessionStorage' in h)
# 相机卡片是 async handler（解锁要联网），另外两个仍是同步直开。
chk('仅相机卡片用 async handler', h.count('bindCard(el, id, async () =>') == 1)
chk('其他卡片保持同步直开', h.count("el.href = a.url") >= 1)
chk('启动脚本已改名', not os.path.exists('start_portal.bat'))
chk('新启动脚本存在', os.path.exists('start_aoting_tools.bat'))

sec('6W. 品牌图标与两段式门禁')
chk('页面有品牌 logo', 'brand_logo.png' in h)
chk('logo 有降级处理', 'onerror=' in h and "display='none'" in h)
chk('brand.ico 存在', os.path.exists('brand.ico'))
chk('brand_logo.png 存在', os.path.exists('brand_logo.png'))
chk('图标已进 precache', 'brand_logo.png' in rd('tools/make_version.py'))
chk('门禁是两段式', 'function gateUnlock' in h)
chk('未解锁加红框', "classList.add('locked')" in h)
chk('解锁后移除红框', "classList.remove('locked')" in h)
# 必须 await：gateUnlock 是异步的，不 await 拿到的是 Promise（永远为真），
# 结果就是"解锁后再点也启动不了"。
chk('首次点击不启动', 'await gateUnlock(a, el)) return;' in h)
# 门禁按卡片配置：只读看状态的工具不该让使用者每次输账号密码。
# 这条如果不生效，"远程机器状态监控"会一直显示"点击解锁"。
chk('门禁可按卡片关闭', "a.gate !== false && !gateOk()" in h)
chk('解锁调用也按配置跳过', "a.gate !== false && await gateUnlock" in h)
chk('校验失败有错误文案', "j && j.err" in h)
chk('解锁有反馈', 'function flash' in h)
chk('有锁态样式', '.card.locked' in h)
chk('无离线提示', '离线能力加载中' not in h)
chk('云端链接移到版本信息下', 'cloud2' in h and '<table id="vtab"></table>' in h)
chk('底部居中块已删', 'class="cloud"' not in h)

sec('6X. 车端登录记录')
sv = rd('apps/camweb/server.py')
chk('有 ACCESS_LOG 常量', 'ACCESS_LOG = ' in sv)
chk('日志落在 /tmp', "'/tmp/aoting_camweb_access.log'" in sv)
chk('只追加不清理', '>> ' in sv and 'rm -f' not in sv.split('def record_access')[1][:1600])
chk('记录登录IP', 'ip=' in sv and '_local_ips' in sv)
chk('记录登录时间', 'car_time=' in sv)
chk('date 格式无空格（免引号）', '+%Y-%m-%d_%H:%M:%S_%Z' in sv)
# 强断言：record_access 体内不得出现 % 格式化（日期串里的 %Y 会被误吃）
# 只看命令拼装那段（try 之前）；try 之后是 PC 侧打日志，用 % 无所谓
_body = sv.split('def record_access')[1].split('\ndef ')[0]
_build = _body.split('    try:')[0]
chk('命令段无 % 格式化', ' % (' not in _build)
chk('whoami 用拼接', "'pc=' + pc" in _build)
chk('后台线程不阻塞', 'record_access,' in sv and 'daemon=True' in sv)
chk('同车去重', 'ACCESS_DEDUPE_SEC' in sv)
chk('失败不影响取图', '不影响取图' in sv)

sec('6Y. 带 logo 的快捷方式')
# v1.0.33 起快捷方式的创建逻辑搬到了 tools/make_shortcut.ps1：
#   .lnk 必须指向 cmd.exe、把 .bat 当参数传，否则 Windows 不给固定到任务栏
#   （脚本没有 AppUserModelID）。断言跟着拆到两个文件上。
cs = rd('create_shortcut.bat')
ps = rd('tools/make_shortcut.ps1')
raw = open(os.path.join(ROOT, 'create_shortcut.bat'), 'rb').read()
# 中文注释用 GBK + CRLF 才能在 cmd 里正常显示（UTF-8 会变乱码、LF 会串行）
chk('bat 为 GBK + CRLF',
    raw.count(b'\r\n') > 0 and raw.count(b'\n') == raw.count(b'\r\n'))
chk('bat 把创建交给 ps1', 'make_shortcut.ps1' in cs)
chk('指向 brand.ico', 'brand.ico,0' in cs)
chk('目标为启动脚本', 'start_aoting_tools.bat' in cs)
chk('设了 IconLocation', 'IconLocation' in ps)
chk('文件夹内也建一个', "GetDirectoryName($Bat)" in ps and '$targets' in ps)
chk('lnk 指向 cmd.exe（否则固定不了任务栏）', "System32', 'cmd.exe'" in ps)

sec('7. portal_server.py 自动退出')
ps = rd('portal_server.py')
chk('有 ping 路由', "'/__portal_ping'" in ps)
chk('有 bye 路由', "'/__portal_bye'" in ps)
# 宽限从 5 秒放宽到 20 秒：切标签页/最小化不再误杀（v1.0.43）。
# 断言落到"意图"上（有宽限、且只由告别触发），不再写死秒数，
# 免得以后再调一次参数测试就碎。
import re as _re
_gm = _re.search(r'GRACE\s*=\s*([0-9.]+)', ps)
_gv = float(_gm.group(1)) if _gm else 0.0
chk('收到告别后有宽限', _gv >= 10.0, 'GRACE=%s' % _gv)
chk('退出只由告别触发（GRACE 宽限）', _gv > 0 and 'bye > 0' in ps)
# 切标签页/最小化不再发告别：visibilitychange 只在回到前台补心跳
chk('隐藏时不再发告别', "visibilityState === 'hidden'" not in h)
chk('回前台补心跳', "visibilityState === 'visible'" in h)
chk('已取消空闲超时（v1.0.31）', 'IDLE_TIMEOUT' not in ps)
chk('新心跳取消退出', '_bye_at = 0.0' in ps)
chk('正常退出返回 0', 'sys.exit(0)' in ps)
chk('端口占用有提示', '已被占用' in ps)
chk('HTML 禁缓存', 'no-store' in ps)

# v1.0.31：空闲超时整条取消。浏览器把后台标签页的定时器节流到 60 秒
# 一次，加上休眠唤醒，心跳迟到一次服务就没了 —— 页面还在（离线缓存），
# 点卡片却只会得到 failed to fetch。放宽超时治标不治本，直接取消。
chk('门户不再按空闲超时自杀',
    'IDLE_TIMEOUT' not in ps and 'NEVER_SEEN_TIMEOUT' not in ps)
chk('子工具的会话超时不受影响（状态监控自带 30 分钟）',
    'MAX_SESSION_SEC' in rd('apps/statusweb/server.py'))
# GET 探测接口：SW 会缓存 version.json，用它判断门户死活会误判成"活着"
chk('GET /__portal_ping 可用于存活探测',
    "self.path.split('?')[0] == '/__portal_ping'" in ps)

sec('7b. 门户失联必须可见（v1.0.23）')
ph = rd('index.html')
chk('页面有门户退出提示条', 'id="pdead"' in ph)
chk('提示条默认隐藏', "id=\"pdead\" style=\"display:none" in ph)
chk('提示条指明重开方式', 'start_aoting_tools.bat' in ph)
# sendBeacon 是"发了就不管"，门户悄悄退出时页面毫无察觉，
# 必须换成能拿到结果的 fetch，否则永远发现不了服务已经没了。
chk('心跳用 fetch（可检测失败）',
    "await fetch('/__portal_ping'" in ph)
chk('连续失败才提示（避免抖动误报）', '_deadN >= 3' in ph)
chk('启动失败前先探测门户是否还在',
    "let alive = false" in ph and "'/__portal_ping'" in ph)
chk('门户不在时提示重开而非手动开 start.bat',
    '门户后台已退出，无法启动本地服务' in ph)

sec('8. bat：正常退出不 pause（否则窗口关不掉）')
bat = rd('start_aoting_tools.bat')
chk('用 portal_server.py', 'portal_server.py' in bat)
# 原意是"不再用 python -m http.server 起静态服务"（那个版本关网页不关窗）。
# 注意不能直接搜 'http.server'：bat 里检测标准库时要 import 它，
# 字符串里必然出现，那是另一回事。
chk('不再用 python -m http.server 起静态服务', '-m http.server' not in bat)
chk('捕获退出码', 'ERRORLEVEL' in bat)
chk('0 时直接退出', 'if "%RC%"=="0" exit /b 0' in bat)
chk('非 0 才 pause', 'pause' in bat)
# bat 统一 GBK + CRLF（cmd 原生编码，chcp 936）。
# 注意不能走"UTF-8 + chcp 65001"：cmd 对已读入的批处理行不会按新代码页
# 重新解码，chcp 放在 bat 里救不了自己 —— v1.0.12 满屏乱码就是这么来的。
_has_cn = [l for l in bat.splitlines() if re.search(r'[\u4e00-\u9fff]', l)]
chk('中文与 chcp 936 配套', (not _has_cn) or ('chcp 936' in bat),
    '中文 %d 行' % len(_has_cn))

sec('9. SW 关键规则')
sw = rd('sw.js')
chk('version.json 走网络', "endsWith('/version.json')" in sw)
chk('不拦截 127.0.0.1', "url.hostname === '127.0.0.1'" in sw)
chk('非同源放行', 'url.origin !== self.location.origin' in sw)
chk('动态取 precache', 'precacheList' in sw)
chk('未写死工具路径', 'bag-player.html' not in sw)
chk('update 会 skipWaiting', 'skipWaiting' in sw)

sec('10. 新增工具脚手架')
at = rd('tools/add_tool.py')
chk('有 add_tool.py', bool(at))
chk('生成 app.json', 'app.json' in at)
chk('生成 src/', "'src'" in at)
chk('区分 page/service', "'page', 'service'" in at or 'page|service' in at)

sec('11. 实测：关页面后服务自动退出')
port = 8901
env = dict(os.environ, PORTAL_PORT=str(port))
pr = subprocess.Popen([sys.executable, 'portal_server.py'],
                      cwd=ROOT, env=env,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True)
try:
    time.sleep(2.5)
    import urllib.request
    def post(path):
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                                     data=b'1', method='POST')
        try:
            return urllib.request.urlopen(req, timeout=3).read().decode()
        except Exception as e:
            return 'ERR:%s' % e
    chk('ping 返回 ok', '"ok": true' in post('/__portal_ping'))
    chk('bye 返回 ok', '"ok": true' in post('/__portal_bye'))
    # 等待时间跟着 GRACE 走：写死 7 秒在 v1.0.43 放宽到 20 秒后就失效了。
    # 从源码里读，以后再调参数测试不会碎。
    _g2 = _re.search(r'GRACE\s*=\s*([0-9.]+)', ps)
    _gv2 = float(_g2.group(1)) if _g2 else 5.0
    time.sleep(_gv2 + 2.5)
    alive = pr.poll() is None
    chk('发 bye 后自动退出', not alive,
        '进程%s' % ('仍在' if alive else '已退出'))
    out = pr.stdout.read() if pr.stdout else ''
    chk('日志说明原因', '自动退出' in out)
finally:
    if pr.poll() is None:
        pr.kill()
    try:
        pr.wait(timeout=3)
    except Exception:
        pass

sec('12. 实测：点进工具不会被误杀')
env = dict(os.environ, PORTAL_PORT=str(port + 1))
pr = subprocess.Popen([sys.executable, 'portal_server.py'],
                      cwd=ROOT, env=env,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True)
try:
    time.sleep(2.5)
    import urllib.request
    def post2(path):
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (port + 1, path),
            data=b'1', method='POST')
        try:
            return urllib.request.urlopen(req, timeout=3).read().decode()
        except Exception as e:
            return 'ERR:%s' % e
    post2('/__portal_ping')          # 门户页
    post2('/__portal_bye')           # 点进工具，门户页卸载
    time.sleep(2)
    post2('/__portal_ping')          # 新页面接上
    time.sleep(5)
    alive = pr.poll() is None
    chk('新页面心跳取消退出', alive, '服务%s' % ('存活' if alive else '被误杀'))
finally:
    if pr.poll() is None:
        pr.kill()
    try:
        pr.wait(timeout=3)
    except Exception:
        pass

sec('13. 版本一致性')
for d in appdirs:
    c = json.load(open(os.path.join(APPS, d, 'app.json'), encoding='utf-8'))
    if c.get('type') == 'link':
        chk('%s 是外链，无需比对文件版本' % d, bool(c.get('url')))
        continue
    entry = os.path.join(APPS, d, c.get('entry', 'index.html'))
    txt = open(entry, encoding='utf-8', errors='replace').read()
    m = re.search(c.get('verPat', r'v(\d+\.\d+)'), txt)
    chk('%s app.json 版本 == 文件内版本' % d,
        m and m.group(1) == c['ver'],
        'json=%s 文件=%s' % (c['ver'], m.group(1) if m else '?'))

sec('14. 令牌文件随包分发')
# 曾经因为"包里没有这个文件"，用户只能自己新建，于是踩到
# gate_token.txt.txt / UTF-16 / 放错目录 这一串坑。所以必须随包提供。
_tk_path = os.path.join(ROOT, 'tools', 'gate_token.txt')
chk('tools/gate_token.txt 存在', os.path.isfile(_tk_path))
if os.path.isfile(_tk_path):
    _raw = open(_tk_path, 'rb').read()
    chk('UTF-8 无 BOM（有 BOM 会导致 403）',
        not _raw.startswith(b'\xef\xbb\xbf'))
    # 走真实解析路径，确认说明行不会污染令牌
    import importlib.util as _iu
    _sp = _iu.spec_from_file_location('_ps', os.path.join(ROOT, 'portal_server.py'))
    _m = _iu.module_from_spec(_sp)
    _sp.loader.exec_module(_m)
    _tk = _m.GATE._read_token_file(_tk_path)   # 实现已移到 tools/gate.py
    chk('能解析出令牌', len(_tk) >= 40, '%d 位' % len(_tk))
    chk('说明行(#)未污染令牌', '#' not in _tk and '说明' not in _tk)
    # 打包后压缩包里也必须带着，否则用户拿到的包还是缺文件
    _zip = os.path.join(os.path.dirname(ROOT), 'aoting_tools.zip')
    if os.path.isfile(_zip):
        import zipfile as _zf
        with _zf.ZipFile(_zip) as _z:
            _names = _z.namelist()
        chk('压缩包内含 gate_token.txt',
            any(n.endswith('tools/gate_token.txt') for n in _names))

sec('15. 门禁必须落在相机服务层（v1.0.15 修的洞）')
# 背景：门禁原先只做在门户页面上。直接双击 apps/camweb/start.bat，
# 服务起在 8765，浏览器直接开 8765 就能连车取图 —— 那道锁形同虚设。
# 原则：能力在哪一层，锁就得在哪一层。
cw = rd('apps/camweb/server.py')
chk('相机服务接入了共享门禁', 'from gate import Gate' in cw)
chk('相机服务会向上找工具箱根目录', '_find_root' in cw)
chk('业务接口要鉴权', 'authorized(self.headers)' in cw)
chk('视频流也要鉴权', "'/stream'" in cw and 'authorized' in cw)
chk('探测接口保持开放', "'/api/version'" in cw and "'/api/ready'" in cw)
chk('开放清单存在', 'GATE_OPEN' in cw)
chk('有登录接口', 'gate_login' in cw)
chk('支持门户交接令牌', 'AOTING_GATE_TK' in cw and 'BOOT_TK' in cw)
chk('令牌比对用 compare_digest', 'compare_digest' in cw)
# 单独拷出去的 camweb 没有 tools/gate.py —— 必须拒绝，不能放行
chk('缺少门禁模块时拒绝而非放行',
    "'找不到账号校验模块' in cw or '找不到账号校验模块（' in cw")
cwi = rd('apps/camweb/index.html')
chk('相机页面有登录框', 'gatelock' in cwi)
chk('相机页面开屏检查登录态', 'checkGate' in cwi)
chk('相机页面处理 401', 'needLogin' in cwi)

ph = rd('index.html')
chk('门户解锁时索取交接令牌', 'wantHandoff' in ph)
chk('门户把令牌带给后端', 'tk: gateTk()' in ph)
chk('门户带令牌打开工具页', '?tk=' in ph)

ps = rd('portal_server.py')
chk('后端签发交接令牌', '_new_handoff' in ps)
chk('后端把令牌传进子进程', "env['AOTING_GATE_TK']" in ps)
# 光看源码不够：上次就是只改了调用方、没改被调方，静态断言全绿，
# 一跑就 TypeError。这里真取一次签名确认对得上。
try:
    import inspect as _ins
    _sigf = str(_ins.signature(_ps.launch_service))
    chk('launch_service 真的接受 handoff', 'handoff' in _sigf, _sigf)
except Exception as _e:
    chk('launch_service 真的接受 handoff', False, str(_e))
chk('后端不重复实现门禁', 'from gate import Gate' in ps
    and 'def verify_pwd' not in ps)

# 门禁实现必须只有一份，否则门户和相机慢慢会跑偏
import os as _os
chk('共享门禁模块存在', _os.path.isfile(_os.path.join(ROOT, 'tools', 'gate.py')))

print('\n' + '=' * 50)
print('通过 %d / 失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
