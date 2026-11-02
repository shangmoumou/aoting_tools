#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TOP5 表格固定高度 —— 防复发测试。

背景：用户反馈「占用 TOP 固定一个高度，否则来回跳」。
跳动有两个来源，都要钉死：
  1) 车端最多回 5 条，进程被过滤 / 刚启动时不足 5 条 -> 行数变
  2) 认出 ROS 节点名时是「主行 + 副行」两行，认不出只有一行 -> 行高变

所以约定：槽位固定 TOPN 个，缺的补空行；副行永远占位（没内容也渲染）。
再加 CSS 把行高钉死，字体渲染的微小差异也不会让整页抖。

这里把 tbl() 从 index.html 里抽出来真跑一遍，而不是正则查源码字符串 ——
后者只验证「代码长得对」，这里验证「渲染出来的行数确实固定」。
"""
import os
import re
import sys
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
    """抠 `const name = ... => ...;` 形式的箭头函数（到此后的空行为止）。
    esc 就是这种写法，用 _fn 抠不到。"""
    m = re.search(r'\nconst %s\s*=' % re.escape(name), src)
    if not m:
        return ''
    nxt = src.find('\n\n', m.start())
    return src[m.start():nxt] if nxt > 0 else src[m.start():]


html = open(HTML, encoding='utf-8').read()

esc_src = _fn(html, 'esc') or _arrow(html, 'esc')
tbl_src = _fn(html, 'tbl')
chk('能从 index.html 抠出 esc()', bool(esc_src))
chk('能从 index.html 抠出 tbl()', bool(tbl_src))
if not (esc_src and tbl_src):
    print('\n%d passed, %d failed' % (ok, len(bad)))
    sys.exit(1)

JS = r'''
__ESC__
__TBL__
const rowsFull = [
  {pid:1,  name:'python',            cpu:12, rss:100, node:'cartographer_no'},
  {pid:2,  name:'ros_app_mqtt_node', cpu:9,  rss:90,  node:''},
  {pid:3,  name:'bluesea2_node',     cpu:7,  rss:80,  node:'bluesea2_1'},
  {pid:4,  name:'orbbec_camera_nod', cpu:5,  rss:70,  node:''},
  {pid:5,  name:'deep_perception',   cpu:3,  rss:60,  node:'deep_perception_node'}
];
const rows3 = rowsFull.slice(0, 3);
const rows0 = [];
const nTr   = s => (s.match(/<tr/g) || []).length;
const nBody = s => (s.match(/<tr(?![^>]*thead)/g) || []).length;

// 数 tbody 里的行（表头那行是 <thead><tr>，排除掉）
function bodyRows(s){
  const b = s.split('<tbody>')[1] || '';
  return (b.match(/<tr/g) || []).length;
}
// 每一行里 td.nm 下的 div 数（副行是否占位）
function nmDivs(s){
  const b = s.split('<tbody>')[1] || '';
  return (b.match(/<td class="nm"/g) || []).map(()=>1).length;
}
// 副行占位的证据：每个 td.nm 里都应有第二个 div（含 &nbsp;）
function hasPlaceholderSub(s){
  const b = s.split('<tbody>')[1] || '';
  const cells = b.split('<td class="nm"').slice(1);
  return cells.every(c => /<\/div>\s*<div class="dim"/.test(c));
}

const full = tbl('T', rowsFull, 'cpu', '%');
const three = tbl('T', rows3, 'cpu', '%');
const zero = tbl('T', rows0, 'cpu', '%');

console.log(JSON.stringify({
  fullRows:  bodyRows(full),
  threeRows: bodyRows(three),
  zeroRows:  bodyRows(zero),
  fullPh:    hasPlaceholderSub(full),
  threePh:   hasPlaceholderSub(three),
  zeroPh:    hasPlaceholderSub(zero),
  zeroHint:  /暂无数据/.test(zero),
  threeHasReal: /bluesea2_node/.test(three),
  threeNoLeak: !/deep_perception/.test(three)
}));
'''.replace('__ESC__', esc_src).replace('__TBL__', tbl_src)

open('/tmp/_t_top5.mjs', 'w', encoding='utf-8').write(JS)
p = subprocess.run(['node', '/tmp/_t_top5.mjs'], capture_output=True, text=True)
if p.returncode != 0:
    print('  FAIL node 执行失败\n' + p.stderr[:800])
    print('\n%d passed, %d failed' % (ok, len(bad) + 1))
    sys.exit(1)

import json
r = json.loads(p.stdout.strip().splitlines()[-1])

chk('5 条数据时 tbody 就是 5 行', r['fullRows'] == 5, r['fullRows'])
chk('3 条数据时补空行到 5 行', r['threeRows'] == 5, r['threeRows'])
chk('0 条数据时也补满 5 行', r['zeroRows'] == 5, r['zeroRows'])
chk('0 条时首行给「暂无数据」提示', r['zeroHint'])
chk('补的空行不会凭空造出进程名', r['threeNoLeak'])
chk('真实数据仍在', r['threeHasReal'])

# 关键：副行始终占位 —— 这才是「行高不跳」的根本
chk('满行时每行都渲染副行占位', r['fullPh'], r['fullPh'])
chk('部分行时每行都渲染副行占位', r['threePh'], r['threePh'])
chk('全空时每行都渲染副行占位', r['zeroPh'], r['zeroPh'])

# CSS：行高钉死
chk('CSS 钉死 #tables tbody tr 行高',
    re.search(r'#tables\s+tbody\s+tr\s*\{[^}]*height\s*:', html) is not None)
chk('空槽有弱化样式（不至于看着像真数据）',
    re.search(r'#tables\s+tbody\s+tr\.ph\s+td\s*\{[^}]*opacity', html) is not None)

# 单引号禁令：脚本经 shell 单引号下发，正文不能有单引号
agent = os.path.join(ROOT, 'apps', 'statusweb', 'stat_agent.py')
if os.path.exists(agent):
    src = open(agent, encoding='utf-8').read()
    chk('stat_agent.py 无单引号（shell 下发禁令）', "'" not in src)

print('\n%d passed, %d failed' % (ok, len(bad)))
sys.exit(1 if bad else 0)
