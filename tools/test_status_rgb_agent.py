#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""彩色相机并入 stat_agent 的专项测试。

背景：相机画面最早是 PC 端另开一条 SSH，把 cam_agent.py heredoc 到车上
裸跑取流。那个脚本没有 source 过 ROS，import rospy 必然失败，表现就是
"登录成功、提示已启动、永远 0 帧"，而失败原因全在 stderr 里没人读。

现在取流并进 stat_agent（它跑在已 source 的 bash 里，ROS 现成可用），
帧以 {"type":"rgb"} 行回传。本文件守的就是这条新链路。
"""
import os
import sys
import re

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   '..', 'apps', 'statusweb')
APP = os.path.normpath(APP)

ok, bad = [], []


def ck(name, cond, extra=''):
    (ok if cond else bad).append(name)
    print(('  ok   ' if cond else '  FAIL ') + name
          + (('  << ' + str(extra)[:200]) if (extra and not cond) else ''))


def read(p):
    with open(p, 'r', encoding='utf-8') as f:
        return f.read()


agent = read(os.path.join(APP, 'stat_agent.py'))
srv = read(os.path.join(APP, 'server.py'))

print('─' * 60)
print('1. 车端：彩色相机模块')

for fn in ('_emit', '_rgb_flag_cams', '_rgb_want', '_rgb_pick', '_rgb_jpeg',
           '_rgb_cb', '_rgb_watch', 'rgb_snapshot'):
    ck('车端有 %s' % fn, re.search(r'^def %s\(' % fn, agent, re.M) is not None)

# 单引号禁令：整份脚本经 shell_quote 单引号包裹下发，正文里出现单引号
# 会在多层引用后变形（实测报过 unterminated quoted string）。
ck('车端脚本无单引号', "'" not in agent, agent.count("'"))

ck('主循环输出走 _emit（与相机回调共用锁）',
   re.search(r'^ {8}_emit\(out\)$', agent, re.M) is not None
   and 'print(json.dumps(out), flush=True)' not in agent)
ck('状态帧带 rgb 诊断', '"rgb": rgb_snapshot()' in agent)
ck('订阅线程已启动',
   'threading.Thread(target=_rgb_watch, args=(rospy,)' in agent)
ck('stdin 认 rgb 列表', 'if "rgb" in c:' in agent)
ck('stdin 认 rgb_cfg', 'if "rgb_cfg" in c:' in agent)
ck('标志文件路径正确', '"/tmp/aoting_rgb_cams.txt"' in agent)
ck('开关日志带 via（排查"勾了没反应"靠它）',
   'rgb -> %s (via stdin)' in agent and 'want -> %s via %s' in agent)

print('─' * 60)
print('2. 车端：话题探测不能硬编码')

ck('用 get_published_topics 探测', 'rospy.get_published_topics()' in agent)
ck('排除 depth 话题', '"depth" in low' in agent)
ck('优先压缩话题（车上已编好 JPEG，透传最省）',
   '"Compressed" in str(c[1])' in agent)
ck('匹配不到时给出明确错误', '车上没有 %s 的彩色话题' in agent)

print('─' * 60)
print('3. 车端：编码分支')

ck('压缩帧直接透传', 'passthrough' in agent)
ck('认 JPEG 魔数', r'b"\xff\xd8\xff"' in agent)
ck('cv2 首选', 'cv2.imencode' in agent)
ck('无 cv2 退 PIL', 'from PIL import Image' in agent)
ck('rgb8 转 BGR（cv2 只认 BGR）', 'a[:, :, ::-1]' in agent)
ck('按目标宽度缩放', 'cv2.resize' in agent)
ck('不支持的编码有明确提示', '不支持的编码 %s' in agent)
ck('按 fps 节流', 'RGB_LAST[cam][0]' in agent)

print('─' * 60)
print('4. 服务端：帧分流')

ck('_parse 分流 rgb 行',
   "d.get('type') == 'rgb'" in srv and 'self._on_rgb(d)' in srv)
ck('rgb 行不进 self.latest（否则会顶掉整屏数据）',
   re.search(r"if d\.get\('type'\) == 'rgb':\n\s+self\._on_rgb\(d\)\n\s+return",
             srv) is not None)
ck('状态帧里的 rgb 诊断落到 RGB_DIAG', 'RGB_DIAG = d.get' in srv)
ck('_on_rgb 存在', re.search(r'^ {4}def _on_rgb\(', srv, re.M) is not None)
ck('_on_rgb 只留最新一帧', 'RGB_FRAMES[cam] = ' in srv)

print('─' * 60)
print('5. 服务端：开关与取帧')

ck('开关发 stdin 命令', "'rgb': cams" in srv)
ck('开关备份写标志文件', '/tmp/aoting_rgb_cams.txt' in srv)
ck('关闭时删标志文件', 'rm -f /tmp/aoting_rgb_cams.txt' in srv)
ck('取帧从 RGB_FRAMES 取', 'fr = RGB_FRAMES.get(cam)' in srv)
ck('没帧时带诊断返回（只看"等待画面"查不出原因）',
   "'diag': diag" in srv)
ck('未开启与等待画面提示不同', '未开启（勾选「订阅并显示」后车端才订阅）' in srv)
ck('独立取流类已移除', 'class RgbStream' not in srv)
ck('不再引用 CAM_AGENT', 'CAM_AGENT' not in srv)

print('─' * 60)
print('6. 车端逻辑：真跑一遍')

sys.path.insert(0, APP)
try:
    import stat_agent as sa
except Exception as e:
    ck('stat_agent 可导入', False, e)
    sa = None

if sa is not None:
    # 标志文件通道：stdin 不通时的第二条路
    p = '/tmp/_t_rgb_cams.txt'
    sa.RGB_FLAG_FILE = p
    try:
        with open(p, 'w') as f:
            f.write('1, 3;5')
        got = sa._rgb_flag_cams()
        ck('标志文件解析 "1, 3;5" -> {1,3,5}', got == set([1, 3, 5]), got)
        with open(p, 'w') as f:
            f.write('')
        ck('空文件 -> 空集合', sa._rgb_flag_cams() == set())
        os.remove(p)
        ck('文件不存在 -> 空集合（不该抛异常）',
           sa._rgb_flag_cams() == set())
    except Exception as e:
        ck('标志文件解析', False, e)

    # _rgb_want：两条通道取"或"
    sa.RGB_ENABLE[2][0] = True
    sa.RGB_WANT_LAST[2][0] = None
    ck('stdin 开 -> 要订阅', sa._rgb_want(2) is True)
    sa.RGB_ENABLE[2][0] = False
    sa.RGB_WANT_LAST[2][0] = None
    ck('两条都不通 -> 不订阅', sa._rgb_want(2) is False)

    # 编码：压缩帧透传
    class _M(object):
        pass
    m = _M()
    m.data = b'\xff\xd8\xff' + b'\x00' * 20
    m.format = 'jpeg'
    d, w, h, enc, lib, err = sa._rgb_jpeg(m, 'sensor_msgs/CompressedImage', 1)
    ck('压缩 jpeg 直接透传', d == m.data and lib == 'passthrough', err)

    m2 = _M()
    m2.data = b'12345678'
    m2.format = 'png'
    d2, _, _, _, _, err2 = sa._rgb_jpeg(m2, 'sensor_msgs/CompressedImage', 1)
    ck('非 jpeg 压缩格式明确报错', d2 is None and '不是 jpeg' in err2, err2)

    # 编码：raw rgb8（无 cv2 时走 PIL）
    m3 = _M()
    m3.width, m3.height, m3.encoding = 8, 4, 'rgb8'
    m3.data = bytes(bytearray([(i * 7) % 256 for i in range(8 * 4 * 3)]))
    d3, w3, h3, enc3, lib3, err3 = sa._rgb_jpeg(m3, 'sensor_msgs/Image', 1)
    ck('raw 帧能编出 JPEG', bool(d3) and d3[:2] == b'\xff\xd8', err3)
    if d3:
        print('        编码库=%s  输出 %d 字节' % (lib3, len(d3)))

    # 不支持的编码要给出明确原因，而不是静默 0 帧
    m4 = _M()
    m4.width, m4.height, m4.encoding = 8, 4, 'bayer_grbg8'
    m4.data = bytes(bytearray(8 * 4))
    d4, _, _, _, _, err4 = sa._rgb_jpeg(m4, 'sensor_msgs/Image', 1)
    ck('bayer 编码明确报"不支持"', d4 is None and err4, err4)

    ck('RGB_IDXS 是 1~5', sa.RGB_IDXS == [1, 2, 3, 4, 5])

    snap = sa.rgb_snapshot()
    ck('rgb_snapshot 覆盖 5 路',
       sorted(snap.keys()) == ['1', '2', '3', '4', '5'], snap.keys())
    ck('每路带 topic/mtype/n/err',
       all(set(['topic', 'mtype', 'n', 'err']) <= set(v.values() if False
               else v.keys()) for v in snap.values()))

print('─' * 60)
print('通过 %d / 失败 %d' % (len(ok), len(bad)))
if bad:
    print('失败项：')
    for b in bad:
        print('  - ' + b)
sys.exit(1 if bad else 0)
