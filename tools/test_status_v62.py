#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.0.62 三项改动的防复发测试。

1) TOP5 数值列不再折行导致行高跳
   原来数值后面跟一个 0~60px 的 inline-block 进度条，两者加起来宽过
   td 的 62px 时就折行 —— 同一列「有时一行、有时两行」，行高跟着跳。
   改成块级、独立一行、宽度按百分比后结构恒定。
   这里不是查源码字符串，而是把 tbl() 抠出来真跑，断言渲染结果。

2) 用户名可改 + 密码框常显
   用户名不再 disabled（默认 aoting）；密码框默认可见（原来 display:none，
   要点「点此输入」才弹）；连接时 user 取自输入框而不是写死。

3) 版本号加粗
   .vergrid .hwv 与 verCell 的内联样式都要有 font-weight:700。
"""
import os
import re
import sys
import json
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML = os.path.join(ROOT, 'apps', 'statusweb', 'index.html')

ok = 0
bad = []


def chk(name, cond, extra=''):
    global ok
    if cond:
        ok += 1
        print('  PASS ' + name)
    else:
        bad.append(name)
        print('  FAIL ' + name + (('  <- ' + str(extra)) if extra else ''))


def _fn(src, name):
    """用大括号配平从 src 里抠出顶层函数 name 的完整定义。"""
    m = re.search(r'\nfunction %s\s*\(' % re.escape(name), src)
    if not m:
        return ''
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    return ''


def _arrow(src, name):
    """抠 `const name = ... => ...;` 形式的箭头函数（esc 就是这种写法）。"""
    m = re.search(r'\nconst %s\s*=' % re.escape(name), src)
    if not m:
        return ''
    nxt = src.find('\n\n', m.start())
    return src[m.start():nxt] if nxt > 0 else src[m.start():]


html = open(HTML, encoding='utf-8').read()

# ─────────────────────────── 1. TOP5 数值列 ───────────────────────────
esc_src = _fn(html, 'esc') or _arrow(html, 'esc')
tbl_src = _fn(html, 'tbl')
chk('能抠出 esc()', bool(esc_src))
chk('能抠出 tbl()', bool(tbl_src))

if esc_src and tbl_src:
    JS = r'''
__ESC__
__TBL__
function tbody(s){ return s.split('<tbody>')[1] || ''; }
// 取出所有 td.num 的内容
function numCells(s){
  const b = tbody(s);
  return b.split('<td class="num"').slice(1).map(c => c.split('</td>')[0]);
}
const rows = [
  {pid:1, name:'python',            cpu:20,   rss:100, node:'cartographer_no'},
  {pid:2, name:'ros_app_mqtt_node', cpu:0,    rss:50,  node:''},
  {pid:3, name:'bluesea2_node',     cpu:0,    rss:25,  node:'bluesea2_1'},
  {pid:4, name:'orbbec_camera_nod', cpu:0,    rss:0,   node:''},
  {pid:5, name:'deep_perception',   cpu:0,    rss:0,   node:'deep_perception_node'}
];
const full = tbl('T', rows, 'cpu', '%');
const cells = numCells(full);
// 进度条宽度百分比
function barPct(c){
  const m = c.match(/<div class="barm"><i style="width:([\d.]+)%/);
  return m ? parseFloat(m[1]) : null;
}
console.log(JSON.stringify({
  nCells:      cells.length,
  allHaveNumv: cells.every(c => /<div class="numv">/.test(c)),
  allHaveBarm: cells.every(c => /<div class="barm">/.test(c)),
  noOldMb:     !/class="mb"/.test(full),
  pcts:        cells.map(barPct),
  noPxWidth:   !/width:\d+px/.test(full)
}));
'''.replace('__ESC__', esc_src).replace('__TBL__', tbl_src)

    open('/tmp/_t_v62.mjs', 'w', encoding='utf-8').write(JS)
    p = subprocess.run(['node', '/tmp/_t_v62.mjs'], capture_output=True, text=True)
    if p.returncode != 0:
        print('  FAIL node 执行失败\n' + p.stderr[:800])
        bad.append('node 执行')
    else:
        r = json.loads(p.stdout.strip().splitlines()[-1])
        chk('5 条数据渲染出 5 个数值格', r['nCells'] == 5, r['nCells'])
        chk('每个数值格都有 numv 块', r['allHaveNumv'])
        chk('每个数值格都有 barm 进度条块', r['allHaveBarm'])
        chk('不再出现会折行的旧 inline-block（class="mb"）', r['noOldMb'])
        chk('进度条宽度是百分比而非固定 px', r['noPxWidth'])
        pcts = r['pcts']
        chk('最大值进度条 100%', pcts and abs(pcts[0] - 100) < 0.01, pcts)
        chk('其余按相对比例（0 值即 0%）',
            pcts and all(abs(x) < 0.01 for x in pcts[1:]), pcts)

# CSS：数值列 nowrap + 进度条块级
chk('td.num 设了 white-space:nowrap',
    re.search(r'td\.num\s*\{[^}]*white-space\s*:\s*nowrap', html) is not None)
chk('.barm 是块级且限高',
    re.search(r'td\.num\s+\.barm\s*\{[^}]*height\s*:\s*4px', html) is not None)

# 车端排序带 pid 二级键（并列时不抖）
agent = os.path.join(ROOT, 'apps', 'statusweb', 'stat_agent.py')
if os.path.exists(agent):
    src = open(agent, encoding='utf-8').read()
    chk('top_cpu 排序带 pid 二级键',
        re.search(r'top_cpu.*sorted\(rows,\s*key\s*=\s*lambda[^)]*r\["pid"\]', src) is not None)

# ─────────────────────────── 2. 用户名 / 密码框 ───────────────────────────
m_user = re.search(r'<input id="user"[^>]*>', html)
chk('用户名输入框存在', m_user is not None)
if m_user:
    tag = m_user.group(0)
    chk('用户名不再是 disabled（可改）', 'disabled' not in tag, tag)
    chk('用户名默认值为 aoting', 'aoting' in tag, tag)

chk('用户名 label 不再写「固定」',
    '用户名（固定）' not in html)

m_pwd = re.search(r'<div class="f-pwd" id="pwdbox"([^>]*)>', html)
chk('密码框容器存在', m_pwd is not None)
if m_pwd:
    chk('密码框默认可见（不再 display:none）',
        'display:none' not in m_pwd.group(1), m_pwd.group(1))

chk('不再有「点此输入」链接', 'lnkpwd' not in html)
chk('connect() 里 user 取自输入框',
    re.search(r"const user\s*=.*\$\('user'\)\.value", html) is not None)
chk('connect() 请求体用变量 user（而非写死 aoting）',
    re.search(r'user\s*:\s*user\b', html) is not None)
chk('用户名会记到本机 localStorage',
    re.search(r"localStorage\.setItem\('st_user'", html) is not None)
chk('启动时会恢复上次的用户名',
    re.search(r"localStorage\.getItem\('st_user'\)", html) is not None)

# ─────────────────────────── 3. 版本号加粗 ───────────────────────────
chk('.vergrid .hwv 有 font-weight:700',
    re.search(r'\.vergrid\s+\.hwv\s*\{[^}]*font-weight\s*:\s*700', html) is not None)
ver_cell = _fn(html, 'verCell')
chk('能抠出 verCell()', bool(ver_cell))
if ver_cell:
    chk('verCell 内联样式也加粗（内联会覆盖 CSS）',
        'font-weight:700' in ver_cell, ver_cell[:200])

print('\n%d passed, %d failed' % (ok, len(bad)))
sys.exit(1 if bad else 0)
