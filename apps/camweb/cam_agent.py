#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cam_agent.py —— 跑在【车上】的取图脚本（ROS1 / rospy）

输出协议（stdout，二进制）：
    4 字节  payload 长度（大端 uint32）
    payload = 4字节 width + 4字节 height + 8字节时间戳(double, unix秒) + 图像字节

    · width > 0  -> 后面是 RGB 原始字节 (w*h*3)，由 PC 端编 JPEG
    · width == 0 且 height == 'JPGD'/'PNGD'
                -> 后面是已编好的 jpeg/png 字节，PC 端直接透传

为什么要带时间戳：让 PC 端能算出"这一帧是几秒前拍的"，
这样延迟是可测量的，而不是靠猜。

为什么要尽量在车端编 JPEG：640x480 原始 RGB @10fps = 9.2MB/s = 73Mbps，
走 frp 中转必堵，堵了就积压成几秒延迟。编成 JPEG 后约 40KB/帧 = 3Mbps。

【v1.4 关键修复：绝不阻塞在写管道上】

    原来的 send 是 `write + flush`，这是阻塞的。一旦下游（SSH 窗口 /
    frp 链路）拥塞，写就会卡住，而 rospy 回调是串行的 —— 卡住期间
    新消息进不来，等它终于写完，管道里已经积压了一堆"过去"的帧。
    表现就是：延迟不断增大，最后像是卡死。

    现在改成：写之前先用 select 探测可写，超过 WRITE_TIMEOUT 还不可写
    就【丢弃这一帧】。宁可少几帧，也要保证画的是"现在"。
    这正是"追帧"思想在车端的那一半（另一半在 PC 端的 max_lag）。

【心跳】
    每 5 秒往 stderr 打一行 HB。PC 端据此区分：
      有心跳没帧  -> 话题本身没数据
      心跳也没了  -> 脚本/链路挂了
    这两种情况的排查方向完全不同。

用法：
    python cam_agent.py <msg_type> <topic> <max_width> <fps> <jpeg_quality>
    jpeg_quality=0  -> 车端不编码，传原始 RGB（兜底，带宽大）
"""
from __future__ import print_function
import sys
import os
import struct
import time
import threading

import rospy
import numpy as np

OUT = getattr(sys.stdout, 'buffer', sys.stdout)

# 写管道最长等待时间。超过就丢帧 —— 见上面"v1.4 关键修复"。
WRITE_TIMEOUT = 1.5
# 父进程存活检测的宽限期：刚启动时不检查，避免把正常的 ppid 当成异常
PPID_GRACE = 15.0
# 连续写失败多少次就认为 stdout 已关闭（PC 端断了），直接退出
MAX_WRITE_ERR = 3


def log(msg):
    try:
        sys.stderr.write('[agent] %s\n' % msg)
        sys.stderr.flush()
    except Exception:
        pass


def _wait_writable(timeout):
    """等 stdout 可写。返回 False = 超时，调用方应丢帧。

    为什么不能写死：管道满时 write() 会一直阻塞，而 rospy 回调是串行的，
    阻塞期间新帧进不来，等写完时管道里已堆满"过去"的帧 —— 延迟就是
    这么被拖大的。探测 + 丢帧，才能始终把"最新"的画面送出去。
    """
    try:
        import select
        fd = OUT.fileno() if hasattr(OUT, 'fileno') else sys.stdout.fileno()
        _, w, _ = select.select([], [fd], [], timeout)
        return bool(w)
    except Exception:
        # 不支持 select（少见）就退回原来的阻塞行为
        return True


def _write(payload):
    """写一帧。返回 'ok' / 'drop'(主动丢弃) / 'err'(写失败)。"""
    try:
        if not _wait_writable(WRITE_TIMEOUT):
            return 'drop'
        OUT.write(struct.pack('>I', len(payload)) + payload)
        OUT.flush()
        return 'ok'
    except Exception:
        return 'err'


# ── 车端编码能力探测：cv2 优先，其次 PIL，都没有就传原始 RGB ──
def make_encoder():
    try:
        import cv2
        def enc(rgb, q):
            bgr = np.ascontiguousarray(rgb[:, :, ::-1])
            ok, buf = cv2.imencode('.jpg', bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(q)])
            if ok:
                return bytes(bytearray(buf))
            return None
        return 'cv2', enc
    except Exception:
        pass
    try:
        from PIL import Image
        import io
        if hasattr(Image, 'frombytes'):
            def fb(data, w, h):
                return Image.frombytes('RGB', (w, h), data)
        else:
            # 老版本 PIL（车上 python2 常见）
            def fb(data, w, h):
                return Image.frombuffer('RGB', (w, h), data, 'raw', 'RGB', 0, 1)
        def enc(rgb, q):
            h, w = rgb.shape[0], rgb.shape[1]
            im = fb(bytes(bytearray(np.ascontiguousarray(rgb))), w, h)
            b = io.BytesIO()
            im.save(b, 'JPEG', quality=int(q))
            return b.getvalue()
        return 'PIL', enc
    except Exception:
        pass
    return 'raw', None


def send_raw(w, h, ts, data):
    payload = struct.pack('>II', w, h) + struct.pack('>d', ts) + data
    return _write(payload)


def send_encoded(tag, ts, blob):
    # tag: b'JPGD' / b'PNGD'，写进 height 那个 4 字节位
    payload = struct.pack('>II', 0, struct.unpack('>I', tag)[0]) + struct.pack('>d', ts) + blob
    return _write(payload)


def decode_raw(msg, max_width):
    """sensor_msgs/Image -> RGB ndarray"""
    w, h = msg.width, msg.height
    enc = (msg.encoding or '').lower()
    step = msg.step
    raw = np.frombuffer(msg.data, dtype=np.uint8)

    if enc in ('rgb8', 'bgr8', '8uc3'):
        nch = 3
    elif enc in ('rgba8', 'bgra8', '8uc4'):
        nch = 4
    elif enc in ('mono8', '8uc1', 'mono16', '16uc1'):
        nch = 1
    else:
        raise ValueError('不支持的 encoding: %s' % msg.encoding)

    if step and step >= w * nch:
        try:
            rows = raw.reshape(h, step)
            raw = rows[:, :w * nch].reshape(h, w, nch)
        except Exception:
            raw = raw[:h * w * nch].reshape(h, w, nch)
    else:
        raw = raw[:h * w * nch].reshape(h, w, nch)

    if nch == 3:
        out = raw[:, :, ::-1] if enc.startswith('bgr') else raw
    elif nch == 4:
        out = raw[:, :, 2::-1] if enc.startswith('bgra') else raw[:, :, :3]
    else:
        out = np.repeat(raw, 3, axis=2)

    out = np.ascontiguousarray(out)
    if max_width and w > max_width:
        k = int(round(float(w) / max_width))
        if k > 1:
            out = np.ascontiguousarray(out[::k, ::k, :])
    return out


def decode_compressed(msg):
    """CompressedImage -> (tag, 原始字节)。车上不解码，直接透传。"""
    fmt = (msg.format or '').lower()
    tag = b'PNGD' if 'png' in fmt else b'JPGD'
    return tag, bytes(msg.data)


def start_heartbeat(box):
    """每 5 秒打一行心跳，顺带做父进程存活检测。

    心跳的作用：让 PC 端区分"话题没数据"和"脚本挂了"。

    父进程检测的作用（v1.5 关键兜底）：
        启动方式是  bash -c 'cat > /tmp/x.py <<EOF ...; python /tmp/x.py'
        python 的父进程就是那个 bash。PC 端一断开 SSH，bash 就死了，
        python 会被 init 收养 —— 于是 os.getppid() 变了。

        这一招不依赖 PC 端的任何清理动作。实测发现 pkill 有时会发不出去
        （清理线程和读数据的线程抢同一个 SSH transport，见 server.close
        的注释），那时车端脚本就会一直跑下去，一路"已发 N 帧"，把车
        的 CPU 吃满，导致后面再也连不上。有了这个兜底，即使 PC 端清理
        完全失败，车端最多多跑几秒就自己退出。
    """
    def loop():
        while True:
            time.sleep(5)
            # 父进程存活检测：宽限期过后，ppid 变了就说明启动我的 bash
            # 已经死了（= PC 端断开），没有必要再跑下去。
            try:
                if box['ppid0'] and (time.time() - box['t0']) > PPID_GRACE:
                    if os.getppid() != box['ppid0']:
                        log('父进程已退出（PC 端断开），本脚本退出')
                        os._exit(0)
            except Exception:
                pass
            try:
                gap = -1.0
                if box['last_msg']:
                    gap = time.time() - box['last_msg']
                msg = 'HB n=%d drop=%d' % (box['n'], box['drop'])
                if box['enc_ms']:
                    msg += ' enc=%.0fms' % (sum(box['enc_ms']) / len(box['enc_ms']))
                if gap > 5:
                    msg += ' no_msg=%.0fs' % gap
                log(msg)
            except Exception:
                pass
    t = threading.Thread(target=loop)
    t.daemon = True
    t.start()


def main():
    if len(sys.argv) < 6:
        log('usage: cam_agent.py <msg_type> <topic> <max_width> <fps> <quality>')
        sys.exit(2)
    msg_type = sys.argv[1]
    topic = sys.argv[2]
    max_width = int(sys.argv[3])
    fps = float(sys.argv[4])
    quality = int(sys.argv[5])

    is_comp = 'Compressed' in msg_type
    if is_comp:
        from sensor_msgs.msg import CompressedImage
        MsgT = CompressedImage
    else:
        from sensor_msgs.msg import Image
        MsgT = Image

    enc_name, enc_fn = ('passthrough', None) if is_comp else make_encoder()
    log('MODE enc=%s quality=%d topic=%s' % (enc_name, quality, topic))
    if enc_name == 'PIL':
        log('提示: 车上没有 cv2，用 PIL 编码（较慢）。'
            '若延迟偏大，请在页面上降低「最大宽度」或「帧率」。')

    rospy.init_node('web_cam_stream', anonymous=True)
    log('订阅 %s [%s] width<=%d fps=%.1f' % (topic, msg_type, max_width, fps))

    box = {'n': 0, 'last': 0.0, 'drop': 0, 'err': 0,
           'last_msg': 0.0, 'enc_ms': [],
           'ppid0': os.getppid(), 't0': time.time()}
    min_dt = 1.0 / fps if fps > 0 else 0.0
    start_heartbeat(box)

    def cb(msg):
        box['last_msg'] = time.time()
        now = time.time()
        if min_dt and (now - box['last']) < min_dt:
            return
        box['last'] = now
        try:
            if is_comp:
                tag, blob = decode_compressed(msg)
                r = send_encoded(tag, now, blob)
            else:
                rgb = decode_raw(msg, max_width)
                if enc_fn and quality > 0:
                    t0 = time.time()
                    j = enc_fn(rgb, quality)
                    box['enc_ms'].append((time.time() - t0) * 1000.0)
                    if len(box['enc_ms']) > 50:
                        box['enc_ms'] = box['enc_ms'][-25:]
                    if j:
                        r = send_encoded(b'JPGD', now, j)
                    else:
                        r = send_raw(rgb.shape[1], rgb.shape[0], now, rgb.tobytes())
                else:
                    r = send_raw(rgb.shape[1], rgb.shape[0], now, rgb.tobytes())

            if r == 'drop':
                # 管道忙：丢掉这一帧，保证送出去的是最新画面
                box['drop'] += 1
                if box['drop'] % 20 == 1:
                    log('管道繁忙，已丢 %d 帧（延迟优先）' % box['drop'])
                return
            if r == 'err':
                box['err'] += 1
                if box['err'] >= MAX_WRITE_ERR:
                    log('stdout 写入持续失败，退出')
                    os._exit(3)
                return
            box['err'] = 0
            box['n'] += 1
            if box['n'] % 50 == 0:
                log('已发 %d 帧' % box['n'])
        except Exception as e:
            log('处理失败: %r' % e)

    rospy.Subscriber(topic, MsgT, cb, queue_size=1)
    log('已就绪，等待数据...')
    rospy.spin()


if __name__ == '__main__':
    main()
