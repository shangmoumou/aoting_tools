# -*- coding: utf-8 -*-
"""v1.0.61 五项改动的落盘校验。

为什么要落盘：这五项里有四项是"看不见就不知道坏了"的 ——
表格抖不抖、画布铺没铺满、滚轮劫不劫持翻页、告警框点不点得动，
都是跑起来才暴露的问题。不写测试的话下次改布局很容易悄悄退化。

不查源码字符串了事：颜色、宽度这类改动查字符串能过，但
"progBar 定义了却没被调用"照样过。所以进度条这里改成真调一遍函数。
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'apps', 'statusweb')
HTML = os.path.join(APP, 'index.html')
AGENT = os.path.join(APP, 'stat_agent.py')

h = io.open(HTML, encoding='utf-8').read()
a = io.open(AGENT, encoding='utf-8').read()

P = 0
F = 0


def ck(ok, name):
    global P, F
    if ok:
        P += 1
        print('  ok   ' + name)
    else:
        F += 1
        print('  FAIL ' + name)


def seg(start, end=None):
    """取 HTML 片段；end 给 None 时取到文件尾，避免窗口太窄被截断。"""
    i = h.find(start)
    if i < 0:
        return ''
    j = h.find(end, i) if end else len(h)
    return h[i:j if j > 0 else len(h)]


print('[1] TOP5 继续减抖')

# 并列项顺序必须稳定，否则每秒重排 => 行上下跳
ck('(-r["cpu"], r["pid"])' in a, 'CPU 排序二级键是 pid（并列项不再每秒重排）')
ck('(-r["rss"], r["pid"])' in a, '内存排序二级键是 pid')
ck('#tables table{table-layout:fixed}' in h, '表格列宽固定（pid 位数变化不再横向挪）')
ck('<colgroup>' in h, 'tbl 输出 colgroup 固定三列宽度')

print('[2] 深度点云画布铺满')

ck('max-width:1180px' not in h, '#pc3dbox 不再限宽 1180px（就是它留的黑边）')
ck('#pc3dbox{grid-column:1 / -1;justify-self:center;width:100%}' in h,
   '#pc3dbox 铺满整行')
ck('#pc3dbox > .card{width:100%}' in h, '内部卡片也铺满')

print('[3] 地图关闭 + 滚轮不劫持翻页')

ck('id="maptoggle"' in h, '有「关闭地图」按钮')
ck('id="maphidden"' in h, '有关闭后的占位提示')
ck('S.mapHidden = !S.mapHidden;' in h, '关闭状态有状态位（重绘时不会又冒出来）')
# 关键：不判定条件就 preventDefault，页面会卡在地图上翻不下去
ck('if (!(e.ctrlKey || e.metaKey || (lock && lock.checked))) return;' in h,
   '滚轮默认不缩放（需勾选或按 Ctrl）')
ck('id="mapwheellock"' in h, '有「滚轮缩放」开关')
ck("btn.textContent = S.mapHidden ? '显示地图' : '关闭地图';" in h,
   '按钮文案随状态切换')

print('[4] 状态配色')

ck('function progBar(' in h, '有进度条配色函数 progBar')
ck('progBar(prog <= 1 ? prog * 100 : prog)' in h, '任务进度条真的调用了 progBar')
ck("yesno(f.relocating, 'bad', 'good')" in h, '重定位中：是=红 否=绿')
ck("yesno(f.wheel_slipping, 'bad', 'good')" in h, '打滑：是=红 否=绿')
ck("charging ? 'warnc' : 'good'" in h, '充电状态：充电中=橙 未充电=绿')
ck("manual ? 'cool' : 'good'" in h, '运行模式：手动=蓝 自动=绿')
ck("estop ? 'bad' : 'good'" in h, '急停：按下=红 正常=绿')

# 真跑一遍 progBar：0/30/60/90 必须落在红/橙/黄/绿四档，且互不相同。
# 只查 "function progBar(" 存在的话，"定义了却没接线"照样能过。
js = seg('function progBar(', 'function tbl(')
if js:
    runner = js + """
const out = [0, 30, 60, 90].map(v => {
  const m = progBar(v).match(/background:(#[0-9a-f]{6})/i);
  return m ? m[1] : '';
});
console.log(JSON.stringify(out));
"""
    tf = os.path.join('/tmp', '_progbar_probe.js')
    io.open(tf, 'w', encoding='utf-8').write(runner)
    try:
        r = subprocess.run(['node', tf], capture_output=True, text=True, timeout=30)
        vals = json.loads((r.stdout or '').strip().splitlines()[-1])
        ck(vals == ['#ff5a5a', '#ff9628', '#ffd93b', '#7ee787'],
           '进度条 0/30/60/90 → 红橙黄绿（实跑：%s）' % '/'.join(vals))
    except Exception as e:
        ck(False, '进度条配色实跑失败：%s' % e)
else:
    ck(False, '抽不出 progBar 源码')

print('[5] 超时告警框放大且可点击')

ck('font-size:26px;padding:18px 36px' in h, '告警框放大一倍（13px→26px，内边距翻倍）')
ck('pointer-events:auto;cursor:pointer;background:#ff3b3b' in h,
   '告警框可点击（原来 pointer-events:none 点不动）')
ck('$(\'alertb\').onclick = doExtend;' in h, '点告警框 = 点延长')
ck('async function doExtend()' in h, '延长逻辑抽成函数（两个入口共用）')
ck("$('bext').onclick = doExtend;" in h, '「延长」按钮仍走同一函数')

print('[6] 版本一致')

app = json.load(io.open(os.path.join(APP, 'app.json'), encoding='utf-8'))
v = app.get('ver') or ''
ck("const PAGE_VER = '%s';" % v in h, 'PAGE_VER = %s' % v)
ck('>v%s</span>' % v in h, '标题版本 v%s' % v)
ck('AGENT_VER = "%s"' % v in a, 'AGENT_VER = %s' % v)

print('[7] 采集脚本禁令（整份经 shell 单引号下发）')

body = a.split('AGENT_VER =', 1)[-1]
ck("'" not in body, '脚本正文无裸单引号')

print()
print('=' * 60)
print(' 通过 %d  失败 %d' % (P, F))
print('=' * 60)
sys.exit(1 if F else 0)
