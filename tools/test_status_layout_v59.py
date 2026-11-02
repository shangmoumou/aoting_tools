# -*- coding: utf-8 -*-
"""v1.0.59 布局改动校验（落盘持久化，防止以后被无意改回去）"""
import re, io, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(ROOT, '..', 'apps', 'statusweb', 'index.html')
AGENT = os.path.join(ROOT, '..', 'apps', 'statusweb', 'stat_agent.py')

fails = []
def ck(cond, name):
    if cond:
        print('  ok   ' + name)
    else:
        fails.append(name)
        print('  FAIL ' + name)

def rd(p):
    with io.open(p, encoding='utf-8') as f:
        return f.read()

h = rd(HTML)

print('[1] 连接配置：字段弹性填充，不再写死窄宽度')
seg = h[h.find('<div class="card connbar">'):h.find('<!-- ── 右：内容 ── -->')]
ck('f-host' in seg and 'f-port' in seg and 'f-user' in seg, '字段带弹性 class')
# 写死的 width:170px 之类必须清掉，否则永远填不满
ck(not re.search(r'style="width:1\d\dpx"', seg), '无写死像素宽度的输入框')
ck('.connbar .fields > div{flex:1 1 0' in h.replace(' ', '').replace('\n', '')
   or 'flex:1 1 0;min-width:92px' in h, 'fields 子项弹性填充')
ck('.connbar .fields input{width:100%!important' in h,
   '输入框宽度 100%')
ck('hzfill' in seg, '频率档位吃掉剩余空间')

print('[2] 地图：滚轮缩放 + 拖拽平移')
ck('mapviewport' in h, '有视口容器（overflow:hidden 才能裁切平移）')
ck("addEventListener('wheel'" in h and 'passive: false' in h,
   '滚轮监听且 passive:false（否则 preventDefault 无效，页面跟着滚）')
ck('zoomMapAt' in h and 'mapzoomin' in h and 'mapzoomout' in h,
   '缩放函数与放大/缩小按钮')
ck('mapviewreset' in h and 'resetMapView' in h, '复位视图')
ck('grabbing' in h and 'mousedown' in h and 'mousemove' in h, '拖拽平移')
ck('transform-origin:0 0' in h or 'transform-origin: 0 0' in h,
   '变换原点 0 0（否则锚点缩放算出来是错的）')
# 缩放只改 CSS transform，不碰 mapGeom —— 点云换算不能受影响
ck(h.count('function mapGeom') == 1, 'mapGeom 仍只有一份')
ck('MAPVIEW' in h, '视图状态变量存在')

print('[3] 深度点云画布铺满')
ck('max-width:900px' not in h, '不再限宽 900px（限宽就会左右留黑）')
ck('#pc3dbox > .card{width:100%}' in h, '卡片宽度 100%')
# 铺满后 CSS 宽度流动，必须同步到 attribute，否则画面横向拉伸
ck('const cssW = Math.max(320, cv.clientWidth' in h,
   '每帧同步 canvas attribute 尺寸，防止拉伸')

print('[4] 版本一致性')
import json
app = json.loads(rd(os.path.join(ROOT, '..', 'apps', 'statusweb', 'app.json')))
# 版本号从 app.json 动态取：写死的话每次发版都会误报，
# 而误报会盖住真正的布局问题（这个文件的重点是布局，不是版本）。
_v = app.get('ver') or ''
ck(bool(_v), 'app.json 有 ver（实际 %r）' % _v)
ck("const PAGE_VER = '%s';" % _v in h, 'PAGE_VER = %s' % _v)
ck('<span class="ver" id="pver">v%s</span>' % _v in h, '标题版本 v%s' % _v)
a = rd(AGENT)
ck('AGENT_VER = "%s"' % _v in a, 'AGENT_VER = %s（否则页面会误报版本不一致）' % _v)

print('[5] 单引号禁令（脚本经 shell 单引号下发）')
body = a[a.find('AGENT_VER'):]
ck("'" not in body.replace("\\'", ''), '采集脚本正文无裸单引号')

print('[6] 静态区 div 配对')
stat = h[:h.find('<script')]
ck(stat.count('<div') == stat.count('</div>'),
   'div %d vs %d' % (stat.count('<div'), stat.count('</div>')))

print('')
if fails:
    print('FAILED %d' % len(fails))
    for f in fails:
        print('  - ' + f)
    sys.exit(1)
print('ALL PASS')
