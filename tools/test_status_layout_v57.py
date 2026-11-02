#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.0.57 布局改动的静态校验。

为什么是静态校验：这些是纯 CSS / DOM 结构改动，没有可执行的运行时行为，
但一旦被后续改动覆盖（比如又有人加回 max-width），页面会立刻回到
"地图两侧留黑边 / 磁盘内部滚动" 的状态，而没有任何报错。
所以把结构钉死在这里。

覆盖 5 项：
  1. 磁盘卡片全展开（不再限高内部滚动）
  2. 车辆状态卡内顺序：导航 → 定位 → 硬件 → 版本
  3. 任务队列固定高度 + 内部滚动
  4. 地图居中拉满（不再限宽 1180px）
  5. 调试工具卡片（自定义话题 / 车机终端）居中拉满
"""
from __future__ import print_function

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML = os.path.join(ROOT, "apps", "statusweb", "index.html")

FAIL = []
OK = []


def ck(name, cond, extra=""):
    if cond:
        OK.append(name)
        print("ok   " + name)
    else:
        FAIL.append(name)
        print("FAIL " + name + ("  | " + str(extra) if extra else ""))


def main():
    with io.open(HTML, encoding="utf-8") as f:
        h = f.read()

    # ── 1. 磁盘：不再限高内部滚动 ──
    ck("磁盘不再限高内部滚动",
       "#disk > .card{max-height" not in h and
       "max-height:152px;overflow-y:auto" not in h)
    ck("磁盘仍独占整行", "#disk{grid-column:1 / -1}" in h)

    # ── 2. 车辆状态卡内顺序 ──
    m = re.search(r'<div class="vehgrid">(.*?)\n\s*</div>', h, re.S)
    ck("找到 vehgrid 结构", m is not None)
    if m:
        blk = m.group(1)
        ids = re.findall(r'id="(tasks|nav|axslam|hw|vers)"', blk)
        ck("vehgrid 内五个块齐全", len(ids) == 5, ids)
        # 定位必须在导航之后，硬件在定位之后，版本在硬件之后
        ck("导航 → 定位 → 硬件 → 版本 顺序正确",
           ids == ["tasks", "nav", "axslam", "hw", "vers"], ids)
        ck("nav/axslam/hw/vers 在 vehcol 内（上下排列）",
           re.search(r'<div class="vehcol">\s*<div id="nav">.*?'
                     r'<div id="axslam">.*?<div id="hw">.*?'
                     r'<div id="vers">', blk, re.S) is not None)
        ck("vehcol 有 flex 纵向布局",
           ".vehcol{display:flex;flex-direction:column" in h)
        ck("vehcol 内的块也去掉了独立卡片边框",
           "#veh > .vehgrid > .vehcol > div > .card{background:transparent" in h)

    # ── 3. 任务队列固定高度 + 滚动 ──
    ck("队列限高 190px 且内部滚动",
       re.search(r'\.qlist\{[^}]*max-height:190px', h) is not None and
       re.search(r'\.qlist\{[^}]*overflow-y:auto', h) is not None)

    # ── 4. 地图拉满 ──
    m4 = re.search(r'#map\{([^}]*)\}', h)
    ck("找到 #map 样式", m4 is not None)
    if m4:
        css = m4.group(1)
        ck("地图宽度 100%", "width:100%" in css, css)
        ck("地图不再限宽 1180px", "max-width:1180px" not in css, css)

    # ── 5. 调试工具卡片拉满 ──
    m5 = re.search(r'#dbg > \.card\{([^}]*)\}', h)
    ck("找到 #dbg > .card 样式", m5 is not None)
    if m5:
        css = m5.group(1)
        ck("调试工具卡片宽度 100%", "width:100%" in css, css)
        ck("调试工具卡片不再限宽", "max-width:1180px" not in css, css)
    # 自定义话题 / 车机终端 必须是 #dbg 的直接 .card 子元素
    # 窗口取到 </main> 为止：dbg 里有多个卡片，取到第一个 </div> 会截断
    m5b = re.search(r'<div class="grp g2" id="dbg">(.*?)</main>', h, re.S)
    ck("找到 #dbg 结构", m5b is not None)
    if m5b:
        ck("自定义话题卡在 dbg 内", "自定义话题" in m5b.group(1))
        ck("车机终端卡在 dbg 内", "车机终端" in m5b.group(1))

    # ── 结构完整性（布局改动最容易碎的地方）──
    body = h.split("<script>", 1)[0]
    ck("静态区 div 配对",
       body.count("<div") == body.count("</div>"),
       (body.count("<div"), body.count("</div>")))

    # ── 版本一致 ──
    pv = re.search(r"const PAGE_VER = '([^']+)'", h)
    aj = re.search(r'"ver":\s*"([\d.]+)"',
                   io.open(os.path.join(ROOT, "apps", "statusweb", "app.json"),
                           encoding="utf-8").read())
    av = re.search(r'AGENT_VER = "([\d.]+)"',
                   io.open(os.path.join(ROOT, "apps", "statusweb", "stat_agent.py"),
                           encoding="utf-8").read())
    ck("PAGE_VER / app.json / AGENT_VER 三处一致",
       pv and aj and av and pv.group(1) == aj.group(1) == av.group(1),
       (pv.group(1) if pv else None, aj.group(1) if aj else None,
        av.group(1) if av else None))

    print("\n通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项：")
        for x in FAIL:
            print("  - " + x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
