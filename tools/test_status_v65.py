#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.65 验证：登录解包 / 内存百分比 / 系统负载 / 未连接视图 / 门户顺序。

为什么不能只查字符串：
    上一版的 `ok, msg = GATE.check(...)` 静态看着完全正常 —— 语法合法、
    缩进正确、变量也都定义了。只有真正调用一次才知道 GATE.check 返回的是
    dict 而不是元组。所以本轮除了查调用形态，还真的 import gate 模块
    调一次 check，确认返回类型。
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SW = os.path.join(ROOT, 'apps', 'statusweb')
HTML = os.path.join(SW, 'index.html')
SRV = os.path.join(SW, 'server.py')
MV = os.path.join(ROOT, 'tools', 'make_version.py')

FAIL = []


def chk(name, cond, extra=''):
    if cond:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s %s' % (name, extra))
        FAIL.append(name)


def rd(p):
    return io.open(p, encoding='utf-8', errors='replace').read()


html = rd(HTML)
srv = rd(SRV)
mv = rd(MV)

print('── 1. 登录解包（本轮核心）──')

# 1) 不能再用两值解包。
# 注意：注释里为了说明这个坑，会原样写下当时的错误写法 `ok, msg = GATE.check(...)`，
# 直接全文匹配会被注释命中（误报）。所以先剔掉注释行再查。
code_lines = [l for l in srv.split('\n') if not l.strip().startswith('#')]
code = '\n'.join(code_lines)
chk('代码里不再有 ok, msg = GATE.check 的两值解包',
    not re.search(r'ok,\s*msg\s*=\s*GATE\.check', code))
chk('代码里不再有裸的 GATE.check 赋值解包',
    not re.search(r'\w+\s*,\s*\w+\s*=\s*GATE\.check\s*\(', code))
# 2) 按 dict 取值
chk('按 dict 取 ok', re.search(r"ok\s*=\s*bool\(\s*r\.get\('ok'\)\s*\)", srv) is not None)
chk('按 dict 取 err', re.search(r"msg\s*=\s*\(\s*r\.get\('err'\)", srv) is not None)

# 3) 真调一次 gate.check，确认返回的是 dict 而非元组
sys.path.insert(0, os.path.join(ROOT, 'tools'))
try:
    import gate as G
    if hasattr(G, 'Gate'):
        g = G.Gate(ROOT) if len(getattr(G.Gate, '__init__').__code__.co_varnames) > 1 else G.Gate()
        r = g.check('somebody', 'x')
        chk('gate.check 返回 dict', isinstance(r, dict), type(r).__name__)
        chk('gate.check 返回含 ok 键', isinstance(r, dict) and 'ok' in r)
    else:
        chk('gate 模块可 import（无 Gate 类则跳过实调）', True)
except Exception as e:
    chk('gate 模块可 import', False, str(e)[:60])

print('── 2. 水印提示文案已删 ──')
chk('登录框不再提水印/访问记录', '画面带账号水印' not in html)
chk('登录成功不再提水印', '画面将带此账号水印' not in html)
chk('门禁说明不再提仍带水印', '仍带水印' not in html)

print('── 3. 内存 TOP5 百分比 ──')
chk('tbl 新增 total 形参', re.search(r'function tbl\([^)]*total', html) is not None)
chk('内存表传了 mem_total',
    re.search(r"tbl\('内存占用 TOP 5'[\s\S]{0,160}?d\.mem_total", html) is not None)
# 必须是 pct 后先闭合字符串、再接 HTML 标签。写成 '%' 会被当成字面量的一部分，
# 页面上直接印出 "34.2%</span>" 这种脏东西 —— 这条断言就是为它准备的。
chk('百分比字符串正确闭合', "pct.toFixed(1) + '%' + '</span>'" in html)
chk('没有把 </span> 包进字符串', "'%</span>'" not in html)
chk('内存表头写明占整机', 'MB / 占整机 %' in html)

print('── 4. 系统负载单独一格 ──')
chk('有系统负载 kv', '<div class="k">系统负载</div>' in html)
chk('标注 1/5/15 分钟平均', '1 / 5 / 15 分钟平均' in html)
chk('CPU 格下不再重复负载', '（1/5/15 分钟负载）' not in html)

print('── 5. 未连接只显示连接配置 + 日志 ──')
chk('有 setConnView 函数', 'function setConnView(' in html)
chk('poll 里调用 setConnView', re.search(r'setConnView\(S\.connected\)', html) is not None)
chk('初始化时先隐藏', re.search(r'setConnView\(false\)', html) is not None)
for i in ('grpsys', 'grpveh', 'cardct', 'cardtm'):
    chk('存在 id=%s' % i, 'id="%s"' % i in html)
chk('四个都纳入隐藏清单',
    all(i in html.split('function setConnView(')[1].split('}')[0] for i in
        ('grpsys', 'grpveh', 'cardct', 'cardtm')))

print('── 6. 门户卡片顺序 ──')
chk('有 APP_ORDER', 'APP_ORDER' in mv)
chk('顺序：statusweb 第一', re.search(r"APP_ORDER\s*=\s*\[\s*'statusweb'", mv) is not None)
chk('顺序：webbag 第二', re.search(r"'statusweb',\s*'webbag'", mv) is not None)
chk('顺序：imuweb 第三', re.search(r"'webbag',\s*'imuweb'", mv) is not None)
chk('顺序：slamweb 第四', re.search(r"'imuweb',\s*'slamweb'", mv) is not None)
chk('不再用 sorted(os.listdir) 直接排',
    not re.search(r'for name in sorted\(os\.listdir\(APPS\)\)', mv))
chk('camweb 仍在隐藏列表', "'camweb'" in mv and 'HIDDEN_APPS' in mv)

print('── 7. 版本号一致 ──')
aj = rd(os.path.join(SW, 'app.json'))
# 版本号动态取，不要写死：写死的话每次发版这 6 条都会误报，
# 而本文件重点是布局与登录，版本误报会盖住真正的结构问题。
import json as _json
_v_app = _json.loads(aj).get('ver')
chk('app.json ver=%s' % _v_app, '"ver": "%s"' % _v_app in aj)
chk('PAGE_VER=%s' % _v_app, "const PAGE_VER = '%s';" % _v_app in html)
chk('页面标题 v%s' % _v_app, 'id="pver">v%s' % _v_app in html)
agent = rd(os.path.join(SW, 'stat_agent.py'))
chk('AGENT_VER=%s' % _v_app, 'AGENT_VER = "%s"' % _v_app in agent)
pj = rd(os.path.join(ROOT, 'portal.json'))
_v_suite = _json.loads(pj).get('suite',{}).get('ver')
chk('suite.ver=%s' % _v_suite, '"ver": "%s"' % _v_suite in pj)
cl = rd(os.path.join(ROOT, 'CHANGELOG.md'))
chk('CHANGELOG 首节为 v%s' % _v_suite, cl.lstrip().startswith('## v%s' % _v_suite))

print()
if FAIL:
    print('FAILED %d: %s' % (len(FAIL), FAIL))
    sys.exit(1)
print('ALL PASS')
