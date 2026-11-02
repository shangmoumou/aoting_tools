# -*- coding: utf-8 -*-
"""server.py 调频接口测试（假 stdin，不起真 SSH）"""
import sys, json, os
sys.path.insert(0, '/data/workspace/aoting_tools/apps/statusweb')
PASS = FAIL = 0
def ck(c, n, e=''):
    global PASS, FAIL
    if c: PASS += 1; print('PASS  ' + n)
    else: FAIL += 1; print('FAIL  ' + n + ('  | ' + str(e) if e else ''))

import server as S

# 路由注册存在
src = open(S.__file__, encoding='utf-8').read()
ck("'/api/set_hz'" in src, '路由 /api/set_hz 已注册')
ck('def set_hz' in src, 'StatSession.set_hz 已定义')
ck('self.stdin = stdin' in src, 'stdin 通道已保存')

# 用假 stdin 测下发逻辑（跳过 __init__ 的线程）
class FakeChan(object):
    def __init__(self): self.sent = []
    def send(self, b): self.sent.append(b)

class FakeStdin(object):
    def __init__(self): self.channel = FakeChan()

def mk():
    s = object.__new__(S.StatSession)
    s.stdin = FakeStdin()
    return s

s = mk()
okd, err = s.set_hz(5)
ck(okd is True, '下发 5Hz 成功')
ck(s.stdin.channel.sent == [json.dumps({'hz': 5.0}) + '\n'],
   '下发内容 = 一行 JSON {"hz": 5.0}', s.stdin.channel.sent)

s = mk()
okd, err = s.set_hz(0.1)
ck(okd is True and '0.1' in s.stdin.channel.sent[0], '下发 0.1Hz')
s = mk()
okd, err = s.set_hz(10)
ck(okd is True and '10.0' in s.stdin.channel.sent[0], '下发 10Hz')

s = mk()
okd, err = s.set_hz(0)
ck(okd is False and '范围' in err, '0Hz 被拒并给出原因', err)
s = mk()
okd, err = s.set_hz('abc')
ck(okd is False and '数字' in err, '非数字被拒', err)
s = mk()
okd, err = s.set_hz(999)
ck(okd is False, '999Hz 被拒')

# 未连接（stdin 为 None）时不能崩
s = object.__new__(S.StatSession)
s.stdin = None
okd, err = s.set_hz(2)
ck(okd is False and '未就绪' in err, '通道未就绪时给出原因', err)

# 通道坏了也不能崩
class Bad(object):
    class channel(object):
        @staticmethod
        def send(b): raise IOError('broken pipe')
s = object.__new__(S.StatSession)
s.stdin = Bad()
okd, err = s.set_hz(2)
ck(okd is False and '下发失败' in err, '通道异常时给出原因', err)

print('\n通过 %d / 失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
