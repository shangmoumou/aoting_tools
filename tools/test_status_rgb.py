#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.63 彩色相机集成 + 本轮几处改动。

原则：能跑起来的就真跑，不查源码字符串。
查字符串只能证明"代码长得对"，跑一遍才能证明"行为是对的"。
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'apps', 'statusweb')

fails = []


def ck(name, cond, extra=''):
    if cond:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s %s' % (name, extra))
        fails.append(name)


def read(p):
    with io.open(p, encoding='utf-8') as f:
        return f.read()


# ── 1. 服务端：路由都已接线 ────────────────────────────────
srv = read(os.path.join(APP, 'server.py'))
html = read(os.path.join(APP, 'index.html'))

# ── 1. 路由接线：GET/POST 两侧都要有，缺一侧就会出现
#    "前端调用了但服务端没这条路"（v1.0.50 栽过：只有 do_GET 有，
#    而前端走 POST，结果开关命令一次都没发出去）
for p in ['/api/rgb', '/api/rgb_frame', '/api/rgb_state', '/api/rgb_login']:
    ck('路由存在 %s' % p, p in srv)

# 路由必须真的落在 do_GET / do_POST 里，不能只出现在注释里
def routes_in(method):
    m = re.search(r'def %s\(self\):(.*?)(?=\n    def |\Z)' % method,
                  srv, re.S)
    return m.group(1) if m else ''

g, po = routes_in('do_GET'), routes_in('do_POST')
for p in ['/api/rgb_state', '/api/rgb_frame']:
    ck('%s 在 do_GET 内' % p, ("p == '%s'" % p) in g)
for p in ['/api/rgb', '/api/rgb_login']:
    ck('%s 在 do_POST 内' % p, ("p == '%s'" % p) in po)

# ── 2. Handler 内不得引用未定义的名字 ──────────────────────
#    这是真踩过的坑：s4 = SESSION（该名字从未定义）导致 handler
#    一进来就 NameError，连接被掐断，前端只看到 Failed to fetch。
#    现在打包前自动扫一遍，防止再犯。
import ast
import builtins
tree = ast.parse(srv)
top = set(dir(builtins))
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        top.add(n.name)
    elif isinstance(n, ast.Assign):
        for tg in n.targets:
            for x in ast.walk(tg):
                if isinstance(x, ast.Name):
                    top.add(x.id)
    elif isinstance(n, (ast.Import, ast.ImportFrom)):
        for a in n.names:
            top.add((a.asname or a.name).split('.')[0])
    elif isinstance(n, ast.For):
        for x in ast.walk(n.target):
            if isinstance(x, ast.Name):
                top.add(x.id)
    elif isinstance(n, ast.With):
        for it in n.items:
            if it.optional_vars is not None:
                for x in ast.walk(it.optional_vars):
                    if isinstance(x, ast.Name):
                        top.add(x.id)

cls_m = {}
for n in tree.body:
    if isinstance(n, ast.ClassDef):
        cls_m[n.name] = {m.name for m in n.body
                         if isinstance(m, ast.FunctionDef)}

bad = []
for n in tree.body:
    if not isinstance(n, ast.ClassDef) or n.name != 'Handler':
        continue
    for m in n.body:
        if not isinstance(m, ast.FunctionDef):
            continue
        loc = set(a.arg for a in m.args.args)
        for sub in ast.walk(m):
            if isinstance(sub, ast.Assign):
                for tg in sub.targets:
                    for x in ast.walk(tg):
                        if isinstance(x, ast.Name):
                            loc.add(x.id)
            elif isinstance(sub, ast.AugAssign):
                for x in ast.walk(sub.target):
                    if isinstance(x, ast.Name):
                        loc.add(x.id)
            elif isinstance(sub, ast.For):
                for x in ast.walk(sub.target):
                    if isinstance(x, ast.Name):
                        loc.add(x.id)
            elif isinstance(sub, ast.With):
                for it in sub.items:
                    if it.optional_vars is not None:
                        for x in ast.walk(it.optional_vars):
                            if isinstance(x, ast.Name):
                                loc.add(x.id)
            elif isinstance(sub, (ast.FunctionDef, ast.Lambda)):
                loc.update(a.arg for a in sub.args.args)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                loc.add(sub.name)
            elif isinstance(sub, ast.comprehension):
                for x in ast.walk(sub.target):
                    if isinstance(x, ast.Name):
                        loc.add(x.id)
            elif isinstance(sub, ast.Global):
                loc.update(sub.names)
        for sub in ast.walk(m):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id in loc or sub.id in top:
                    continue
                if sub.id in cls_m.get('Handler', set()):
                    continue
                bad.append((m.name, sub.id))
# urllib 是 try 内 import 的局部名，属于误报，单独排除
bad = [b for b in bad if b[1] != 'urllib']
ck('Handler 无未定义名字引用', not bad, str(bad[:5]))

# ── 3. 车端脚本下发方式：必须 heredoc ──────────────────────
#    cam_agent.py 含 73 个单引号，用 python3 -c '<源码>' 经 shell
#    单引号包裹后多层转义会变形，远端直接 unterminated quoted string。
agent = read(os.path.join(APP, 'cam_agent.py'))
# 取流已并入 stat_agent：不再单独下发 cam_agent.py。
# 独立脚本是裸跑的，没有 ROS 环境，import rospy 必失败 —— 这正是
# "登录成功、已启动、永远 0 帧"的根因。现在只下发开关命令。
ck('不再下发独立取流脚本（CAM_AGENT 已移除）', 'CAM_AGENT' not in srv)
ck('开关走采集通道 stdin', "'rgb': cams" in srv)
ck('开关备份走车上标志文件', '/tmp/aoting_rgb_cams.txt' in srv)
ck('没有 RgbStream 独立取流类', 'class RgbStream' not in srv)

# ── 4. 门禁：未登录不给画面 ────────────────────────────────
ck('门禁启用判断 rgb_gate_enabled', 'def rgb_gate_enabled' in srv)
ck('未登录返回 needLogin', 'needLogin' in srv)
ck('取帧接口校验门禁',
   'def api_rgb_frame' in srv and 'rgb_user(self.headers)' in srv)
ck('cookie 用 HttpOnly', 'HttpOnly' in srv)

# ── 5. 车端访问记录 ────────────────────────────────────────
ck('有车端访问记录函数', 'def rgb_record_access' in srv)
ck('访问记录只追加不覆盖', '>>' in srv and 'aoting_statusweb_access.log' in srv)

# ── 6. 断连时清理相机流 ────────────────────────────────────
ck('断开时停止相机流', 'rgb_stop_all()' in srv)
ck('有 rgb_stop_all', 'def rgb_stop_all' in srv)

# ── 7. app.json 带上了 cam_agent.py ────────────────────────
aj = json.loads(read(os.path.join(APP, 'app.json')))
ck('app.json files 含 cam_agent.py',
   'cam_agent.py' in (aj.get('service', {}).get('files') or []))
ck('cam_agent.py 文件存在', os.path.isfile(os.path.join(APP, 'cam_agent.py')))

# ── 8. 前端：卡片与位置 ────────────────────────────────────
ck('有 rgbbox 容器', 'id="rgbbox"' in html)
# 彩色相机必须在深度点云之上
ck('彩色相机在深度点云之上',
   html.index('id="rgbbox"') < html.index('id="pc3dbox"'))
ck('卡片名「彩色相机信息」', '彩色相机信息' in html)
# 五路勾选：复选框是 [1,2,3,4,5].map 动态生成的，源码里字面量只出现一次，
# 数字符串必然失败。把那段表达式抽出来用 node 真跑，数产出几个 checkbox。
mm = re.search(r"(\[1,2,3,4,5\]\.map\(function\(i\)\{.*?\}\)\.join\(''\))",
               html, re.S)
ck('五路勾选表达式可提取', bool(mm))
if mm:
    import subprocess
    js = 'var s = ' + mm.group(1) + ';' \
         + 'console.log(JSON.stringify({' \
         + 'n:(s.match(/class="rgbcam"/g)||[]).length,' \
         + 'vals:(s.match(/value="(\\d)"/g)||[])}));'
    open('/tmp/_cams.js', 'w', encoding='utf-8').write(js)
    r = subprocess.run(['node', '/tmp/_cams.js'], capture_output=True,
                       text=True)
    if r.returncode == 0 and r.stdout.strip():
        try:
            res = json.loads(r.stdout.strip())
            ck('生成 5 个相机勾选框', res['n'] == 5, str(res))
            ck('勾选值为 01~05', res['vals'] == ['value="1"', 'value="2"',
                                                 'value="3"', 'value="4"',
                                                 'value="5"'], str(res))
        except Exception as e:
            ck('五路勾选可解析', False, str(e) + r.stdout[:200])
    else:
        ck('五路勾选可运行', False, (r.stderr or '')[:200])

# ── 9. 水印：真画一遍，不查字符串 ──────────────────────────
m = re.search(r'function drawWatermark\(ctx, w, h, opt\)\{(.*?)\n\}',
              html, re.S)
ck('drawWatermark 可提取', bool(m))
if m:
    js = ('function measureText(){return function(t){'
          'return {width: t.length * 7};}}\n'
          'var ctx = {save:function(){},restore:function(){},'
          'measureText:function(t){return {width:t.length*7};},'
          'setTransform:function(){},translate:function(){},'
          'rotate:function(){},fillRect:function(){},'
          'fillText:function(){},globalAlpha:1,fillStyle:"",font:"",'
          'textAlign:"",textBaseline:""};\n'
          'var out=[];\n'
          'var _ft = ctx.fillText;\n'
          'ctx.fillText = function(t,x,y){ out.push(String(t)); };\n'
          + m.group(0) + '\n'
          'drawWatermark(ctx, 640, 480, {user:"zhangsan",'
          'target:"car.example.com:21823",time:"2026-10-13 15:00:00"});\n'
          'console.log(JSON.stringify({n: out.length,'
          'hasUser: out.some(function(s){return s.indexOf("zhangsan")>=0}),'
          'first: out[0]||""}));')
    open('/tmp/_wm.js', 'w', encoding='utf-8').write(js)
    import subprocess
    r = subprocess.run(['node', '/tmp/_wm.js'], capture_output=True,
                       text=True)
    if r.returncode == 0 and r.stdout.strip():
        try:
            res = json.loads(r.stdout.strip())
            ck('水印画了斜纹（多条）', res['n'] > 3, str(res))
            ck('水印含账号', res['hasUser'], str(res))
        except Exception as e:
            ck('水印测试可解析', False, str(e) + r.stdout[:200])
    else:
        ck('水印测试可运行', False, (r.stderr or '')[:200])

# ── 10. 深度点云：滚轮默认不接管 + 过滤默认 2 ───────────────
ck('点云有滚轮锁复选框', 'id="pc3dwheellock"' in html)
# wheel 里必须先判断再 preventDefault
wm = re.search(r"cv\.addEventListener\('wheel'(.*?)\{passive: false\}\);",
               html, re.S)
ck('点云 wheel 有条件判断', bool(wm) and 'preventDefault' in (wm.group(1) if wm else '')
   and ('ctrlKey' in (wm.group(1) if wm else '')),
   '' if wm else '找不到 wheel 监听')
if wm:
    body = wm.group(1)
    # preventDefault 必须在 if 内：无条件的话页面会卡住翻不动
    ck('preventDefault 在条件内（不无条件拦截翻页）',
       body.index('return') < body.index('preventDefault'))
ck('过滤默认 2 米', re.search(r'id="pc3dmaxd"[^>]*value="2"', html) is not None)

# ── 11. 地图按钮改名 ───────────────────────────────────────
ck('「加载所选地图」', '加载所选地图' in html)
ck('「重新加载当前地图」', '重新加载当前地图' in html)
ck('旧名「重新解析话题」已移除', '重新解析话题' not in html)
ck('旧名「>加载<」已移除', re.search(r'>加载<', html) is None)

# ── 12. 门户：camweb 卡片已隐藏 ────────────────────────────
mv = read(os.path.join(HERE, 'make_version.py'))
ck('make_version 有 HIDDEN_APPS', 'HIDDEN_APPS' in mv)
ck('camweb 在隐藏列表', "camweb" in mv and 'HIDDEN_APPS = set' in mv)

# ── 13. 版本一致性 ─────────────────────────────────────────
# 版本号【不写死】：写死的话每次发版都会误报，而这份测试的重点是
# 彩色相机的结构与行为 —— 版本误报会盖住真正的结构问题。
V = aj.get('ver')
pj = json.loads(read(os.path.join(ROOT, 'portal.json')))
SV = (pj.get('suite') or {}).get('ver')
ck('app.json 有版本号', bool(V), str(V))
ck('PAGE_VER 与 app.json 一致', ("const PAGE_VER = '%s';" % V) in html, V)
ck('页面标题与 app.json 一致', ('id="pver">v%s<' % V) in html, V)
ag = read(os.path.join(APP, 'stat_agent.py'))
ck('AGENT_VER 与 app.json 一致', ('AGENT_VER = "%s"' % V) in ag, V)
cl = read(os.path.join(ROOT, 'CHANGELOG.md'))
ck('CHANGELOG 首节 = suite.ver',
   cl.split('\n', 1)[0].strip() == ('## v%s' % SV),
   cl.split('\n', 1)[0].strip())

print('\n失败 %d 项' % len(fails))
if fails:
    for f in fails:
        print('  - ' + f)
    sys.exit(1)
print('全部通过')
