# -*- coding: utf-8 -*-
"""彩色相机「0 帧」诊断能力测试（v1.0.64）

背景：用户反馈"登录后没有图像，只提示等待车端画面"，服务端日志只有
「相机01 取流结束（共 0 帧）」—— 看得见现象，看不见原因。

根因是 cam_agent 的所有日志都走 stderr，而 PC 端从来没读过它。
所以这份测试盯的不是"能不能出图"（那依赖真车），而是三件事：

  1. 车端 stderr 必须被读出来（_pump_err）
  2. 启动前必须探测话题在不在、什么类型（_probe_topic）
  3. 无帧时必须把诊断回给页面（diag / api_rgb_frame）

第 3 条用真实调用验证，不只查字符串——查字符串能过但接口没接线的情况
历史上出现过（s4 = SESSION 那次）。
"""
import os
import sys
import io
import types

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), 'apps', 'statusweb')
sys.path.insert(0, APP)

ok = fail = 0


def chk(name, cond, extra=''):
    global ok, fail
    if cond:
        ok += 1
        print('  ok   %s' % name)
    else:
        fail += 1
        print('  FAIL %s %s' % (name, extra))


class FakeOut(object):
    def __init__(self, text):
        self._b = text.encode('utf-8')

    def read(self):
        return self._b


class FakeChannel(object):
    def __init__(self, exited=False, status=4):
        self._exited = exited
        self._status = status

    def exit_status_ready(self):
        return self._exited

    def recv_exit_status(self):
        return self._status


class FakeSSH(object):
    """按命令关键字返回预设输出。"""

    def __init__(self, table):
        self.table = table      # 关键字 -> 输出文本
        self.cmds = []

    def exec_command(self, cmd, timeout=None):
        self.cmds.append(cmd)
        out = ''
        for k, v in self.table.items():
            if k in cmd:
                out = v
                break
        return (None, FakeOut(out), FakeOut(''))


def new_stream():
    import server
    s = server.RgbStream(1, 'h', 22, 'u', '')
    return s


def main():
    import server

    print('== _probe_topic ==')
    st = new_stream()
    ssh = FakeSSH({'rostopic info': 'Type: sensor_msgs/CompressedImage\n'
                                    'Publishers: None\n'})
    st._probe_topic(ssh)
    chk('探测到 CompressedImage 就切换类型',
        st.mtype == 'sensor_msgs/CompressedImage', st.mtype)
    chk('探测做了标记', st.probed)

    st2 = new_stream()
    ssh2 = FakeSSH({'rostopic info /camera_01/color/image_raw':
                    'ERROR: Unknown topic /camera_01/color/image_raw\n',
                    'rostopic list': '/camera_01/rgb/image_raw\n'
                                     '/camera_01/depth/image_raw\n'})
    st2._probe_topic(ssh2)
    chk('默认话题不存在时改用车上实际话题',
        st2.topic == '/camera_01/rgb/image_raw', st2.topic)
    chk('探测失败会在 err 里说清楚',
        'camera_01' in (st2.err or '') or st2.topic !=
        '/camera_01/color/image_raw', st2.err)

    st3 = new_stream()
    ssh3 = FakeSSH({'rostopic info': ''})
    st3._probe_topic(ssh3)
    chk('车上完全没有该相机话题时给出明确 err',
        '没有' in (st3.err or ''), st3.err)

    print('== _agent_cmd 携带类型 ==')
    st4 = new_stream()
    st4.mtype = 'sensor_msgs/CompressedImage'
    cmd = st4._agent_cmd()
    chk('下发命令带探测到的类型', 'sensor_msgs/CompressedImage' in cmd)
    chk('下发命令带话题', '/camera_01/color/image_raw' in cmd)
    chk('用 heredoc 下发（源码含大量单引号）', 'AOEOF' in cmd)

    print('== _pump_err ==')
    st5 = new_stream()
    lines = [b'[agent] MODE enc=cv2 quality=70\n',
             u'[agent] 已就绪，等待数据...\n'.encode('utf-8'),
             b'\n',
             u'[agent] 警告: 话题当前没有发布者\n'.encode('utf-8')]
    st5._pump_err(lines)
    chk('车端 stderr 被收进 err_tail', len(st5.err_tail) == 3,
        len(st5.err_tail))
    chk('空行被跳过', all(x.strip() for x in st5.err_tail))
    chk('中文行正常解码',
        any(u'没有发布者' in x for x in st5.err_tail), list(st5.err_tail))

    print('== diag ==')
    st6 = new_stream()
    st6.topic = '/camera_01/rgb/image_raw'
    st6.exit_status = 4
    st6.err = 'xx'
    st6.err_tail.append('致命: 缺少 numpy')
    d = st6.diag()
    for k in ('topic', 'mtype', 'n', 'exit', 'err', 'tail'):
        chk('diag 含 %s' % k, k in d)
    chk('diag 带上车端日志尾', d['tail'] == ['致命: 缺少 numpy'])

    print('== _watch：脚本已退出要报 exit code ==')
    st7 = new_stream()
    st7.err_tail.append('[agent] 致命: import rospy 失败')
    ch = FakeChannel(True, 4)
    out = types.SimpleNamespace(channel=ch)
    st7._watch(out, wait=0)       # wait=0：不等 20 秒，直接走判定
    chk('记录退出码', st7.exit_status == 4, st7.exit_status)

    print('== api_rgb_frame 无帧时回传诊断 ==')
    src = io.open(os.path.join(APP, 'server.py'), encoding='utf-8').read()
    chk('无帧分支带 diag', "err': st.err or '等待车端画面…',\n"
        "                             'diag': st.diag()" in src)
    chk('帧头带帧序号', 'X-Frame-N' in src)

    print('== 前端显示诊断 ==')
    html = io.open(os.path.join(APP, 'index.html'), encoding='utf-8').read()
    chk('前端读 diag', 'res.j.diag' in html)
    chk('前端显示车端日志尾', "车端: ' + tl[i]" in html)
    chk('cell 支持多行', 'Array.isArray(t)' in html)

    print('== cam_agent 依赖缺失要说人话 ==')
    ca = io.open(os.path.join(APP, 'cam_agent.py'), encoding='utf-8').read()
    chk('numpy 缺失有明确提示', '缺少 numpy' in ca and 'pip install' in ca)
    chk('rospy 缺失有明确提示', 'import rospy 失败' in ca)
    chk('订阅前检查话题有没有发布者', 'get_published_topics' in ca)
    chk('类型不匹配会警告', '类型不匹配' in ca)

    print('== 令牌已就位 ==')
    tk = os.path.join(os.path.dirname(HERE), 'tools', 'gate_token.txt')
    chk('tools/gate_token.txt 存在', os.path.isfile(tk))
    if os.path.isfile(tk):
        txt = io.open(tk, encoding='utf-8').read()
        chk('令牌非注释行存在',
            any(l.strip() and not l.strip().startswith('#')
                for l in txt.splitlines()))

    print('\n通过 %d，失败 %d' % (ok, fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
