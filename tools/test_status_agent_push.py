# -*- coding: utf-8 -*-
"""验证采集脚本不再经 argv 下发（128KB 硬限制）。"""
import sys, os
HERE = '/data/workspace/aoting_tools/apps/statusweb'
sys.path.insert(0, HERE)

class FakeStdin:
    def __init__(self): self.buf = b''; self.channel = type('C',(),{'shutdown_write':lambda s:None})()
    def write(self, d): self.buf += d
    def flush(self): pass
class FakeOut:
    def __init__(self, code=0, data=b''): self.channel=type('C',(),{'recv_exit_status':lambda s:code})(); self._d=data
    def read(self): return self._d
class FakeSSH:
    def __init__(self, can_write=True, size_ok=True):
        self.cmds=[]; self.can_write=can_write; self.size_ok=size_ok; self._sz=0
    def exec_command(self, cmd, timeout=None):
        self.cmds.append(cmd)
        if cmd.startswith('cat >'):
            self._sz = 0
            def side(): pass
            st=FakeStdin()
            # 让 _write_remote 写入后我们能拿到长度
            self.last_stdin=st
            return st, FakeOut(0 if self.can_write else 1), None
        if cmd.startswith('wc -c'):
            n = len(self.last_stdin.buf) if self.size_ok else 7
            return None, FakeOut(0, str(n).encode()), None
        return FakeStdin(), FakeOut(0), None

# 直接复用 server 里的两个方法，拼一个最小宿主
import importlib.util

# server.py 顶层会启动服务，不能直接 import；改为抽出方法测试
src = open(os.path.join(HERE,'server.py'), encoding='utf-8').read()
start = src.index('    def _write_remote')
end = src.index('    def _pump_err')
body = src[start:end]
ns = {'os': os, 'log': lambda m: print('  [log]', m),
      'ROS_ENV_PREFIX': '', 'MAX_ARG_STRLEN': 131072,
      'shell_quote': lambda s: "'"+s+"'"}
exec('class Host:\n' + body, ns)
Host = ns['Host']

SRC = 'X' * 132731   # 与真实脚本同尺寸，超过 131072
h = Host()

print("=== 场景1：/tmp 可写 ===")
ssh = FakeSSH(can_write=True, size_ok=True)
stdin, out, err = h._push_and_start(ssh, SRC)
big = [c for c in ssh.cmds if len(c) > 200000]
print("  下发的命令:", ssh.cmds)
print("  ✓ 源码进 argv 的命令数:", len(big), "(应为 0)")
print("  ✓ 走落盘执行:", any('python3 -u' in c for c in ssh.cmds))
print("  ✓ 结果可用:", out is not None)
assert len(big) == 0, "源码仍在 argv 里！"
assert any('python3 -u' in c for c in ssh.cmds)
assert out is not None

print("=== 场景2：落盘全失败 + 超长 ===")
ssh2 = FakeSSH(can_write=False)
r = h._push_and_start(ssh2, SRC)
print("  返回:", tuple(x is None for x in r))
assert r[1] is None, "超限且落盘失败时应返回 None"

print("=== 场景3：落盘失败但长度有余量 -> 回退 -c ===")
ssh3 = FakeSSH(can_write=False)
r3 = h._push_and_start(ssh3, 'Y' * 1000)
print("  走 -c 兜底:", any('python3 -c' in c for c in ssh3.cmds))
assert any('python3 -c' in c for c in ssh3.cmds)

print("=== 场景4：落盘不完整要能发现 ===")
ssh4 = FakeSSH(can_write=True, size_ok=False)
r4 = h._push_and_start(ssh4, SRC)
print("  不完整时返回 None:", r4[1] is None)
assert r4[1] is None

print("\n全部通过")
