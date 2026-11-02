#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.0.58 布局回归：分组内卡片的顺序 + 调试工具单列。

为什么要落盘：布局改动极易在后续版本被无意打回，而 HTML 顺序肉眼很难核对。
这里直接断言「谁在谁前面」，改错了立刻红。

注意：抓分组片段时不要用 \n    </div> 这种窄窗口 —— 组内第一个 </div>
就把片段截断了，会误报「某块不在组内」。之前踩过一次，这里改成抓到 </main>。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML = os.path.join(ROOT, 'apps', 'statusweb', 'index.html')

FAIL = []


def chk(cond, name):
    if cond:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s' % name)
        FAIL.append(name)


def main():
    s = open(HTML, encoding='utf-8').read()

    # ── ② 车辆状态信息：故障 -> 地图 -> 深度点云 ──
    # v1.0.65 起分组容器带上了 id（未连接时要整组隐藏），类名顺序也不再固定，
    # 所以只认 grphead 文案，不认外层 div 的写法。
    m = re.search(r'<div class="grp[^"]*"[^>]*>\s*<div class="grphead">② 车辆状态信息</div>(.*?)\n    </div>',
                  s, re.S)
    chk(m is not None, '找到 ② 车辆状态信息 分组')
    if not m:
        return 1
    blk = m.group(1)
    i_f = blk.find('id="faults"')
    i_m = blk.find('id="map"')
    i_p = blk.find('id="pc3dbox"')
    chk(i_f > 0, '② 内有 故障')
    chk(i_m > 0, '② 内有 地图')
    chk(i_p > 0, '② 内有 深度点云')
    chk(i_f < i_m, '故障在地图之上')
    chk(i_m < i_p, '深度点云在地图之下')

    # ── ③ 调试工具：自定义话题 -> 车机终端 -> 日志，且无 pc3dbox ──
    i = s.find('id="dbg"')
    j = s.find('</main>', i)
    chk(i > 0 and j > i, '找到 ③ 调试工具 分组')
    dbg = s[i:j] if (i > 0 and j > i) else ''
    a = dbg.find('自定义话题')
    b = dbg.find('车机终端')
    c = dbg.find('<h2>日志</h2>')
    chk(a > 0, '③ 内有 自定义话题')
    chk(b > 0, '③ 内有 车机终端')
    chk(c > 0, '③ 内有 日志')
    chk(a < b < c, '调试工具顺序：自定义话题 -> 车机终端 -> 日志')
    chk('pc3dbox' not in dbg, '深度点云已移出调试工具')

    chk(s.count('id="pc3dbox"') == 1, 'pc3dbox 容器全文只有一处')

    # ── 调试工具强制单列（否则两块输入卡并排，输入框被压窄）──
    chk('#dbg{grid-template-columns:minmax(0,1fr)}' in s, '调试工具单列竖排')
    chk('#dbg > .card{justify-self:center;width:100%}' in s, '调试工具卡片拉满')

    print('\nFAIL=%d' % len(FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
