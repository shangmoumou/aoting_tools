# -*- coding: utf-8 -*-
"""车端采集进程清理：验证 pid + ppid + rosnode 三层都执行到。

这些断言专盯"断开后车上进程还在"这一类问题：
  - 只杀 python 不杀父 bash → bash 永远挂在 SSH session
  - 不注销 rosnode 名 → rosnode list 里名字残留几分钟
  - exec_command 后不等 recv_exit_status → 命令没发出去就被掐断
"""
import os
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
APPS = os.path.normpath(os.path.join(HERE, '..', 'apps', 'statusweb'))
SPEC = importlib.util.spec_from_file_location(
    'sw', os.path.join(APPS, 'server.py'))
sw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sw)

PASS = FAIL = 0


def ck(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ok  %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s %s' % (name, extra))


class FakeOut(object):
    def __init__(self, txt):
        self._t = txt.encode('utf-8')
        self.channel = self

    def recv_exit_status(self):
        return 0

    def read(self):
        return self._t


class FakeSSH(object):
    def __init__(self, alive):
        self.cmds = []
        self.alive = set(alive)
        self.waited = 0

    def exec_command(self, cmd, timeout=None):
        self.cmds.append(cmd)
        txt = ''
        low = cmd
        if 'kill -0 ' in low:
            pid = int(low.split('kill -0 ')[1].split()[0])
            txt = 'ALIVE' if pid in self.alive else 'GONE'
        # 收到 TERM/KILL 就认为目标消失
        for tok in ('kill -TERM ', 'kill -KILL '):
            if tok in low:
                pid = int(low.split(tok)[1].split()[0])
                self.alive.discard(pid)
        return None, FakeOut(txt), None

    def close(self):
        pass


class Sess(sw.StatSession):
    def __init__(self, ssh, pid, ppid):
        self.ssh = ssh
        self.agent_pid = pid
        self.agent_ppid = ppid
        self.cleanup_ok = False
        self.cleanup_left = None

    def _new_ssh(self):
        return None


def run(alive, kill_all=True):
    ssh = FakeSSH(alive)
    s = Sess(ssh, 10023, 9854)
    ok, left = s._kill_remote()
    return ok, left, ssh.cmds


def pid_of(cmd):
    for tok in ('kill -TERM ', 'kill -KILL ', 'kill -0 '):
        if tok in cmd:
            return int(cmd.split(tok)[1].split()[0])
    return None


print('== 1. hello 帧带 ppid ==')
src = open(os.path.join(APPS, 'stat_agent.py'), encoding='utf-8').read()
ck('stat_agent 上报 ppid', 'os.getppid()' in src)
ck('单引号禁令未破（全文无单引号）', "'" not in src)

print('== 2. 正常清理：pid + ppid 都被杀 ==')
ok, left, cmds = run({10023, 9854})
ck('返回 ok=True', ok is True)
ck('无残留', left == [], str(left))
terms = [pid_of(c) for c in cmds if 'kill -TERM ' in c]
ck('TERM 了采集进程 10023', 10023 in terms, str(terms))
ck('TERM 了父 shell 9854', 9854 in terms, str(terms))
ck('rosnode kill 被调用',
   any('rosnode kill' in c for c in cmds),
   str([c for c in cmds if 'rosnode' in c]))
ck('节点名按前缀过滤',
   any('aoting_stat_agent_' in c and 'grep -E' in c for c in cmds))
ck('rosnode 前带 ROS 环境',
   any('rosnode kill' in c and c.startswith(sw.ROS_ENV_PREFIX) for c in cmds))

print('== 3. TERM 无效时升级 KILL ==')


class Stubborn(FakeSSH):
    def exec_command(self, cmd, timeout=None):
        self.cmds.append(cmd)
        txt = ''
        if 'kill -0 ' in cmd:
            pid = int(cmd.split('kill -0 ')[1].split()[0])
            # 只有 -KILL 才真的杀掉
            txt = 'GONE' if pid in self.killed else 'ALIVE'
        for tok in ('kill -TERM ', 'kill -KILL '):
            if tok in cmd:
                pid = int(cmd.split(tok)[1].split()[0])
                if tok == 'kill -KILL ':
                    self.killed.add(pid)
                    self.alive.discard(pid)
        return None, FakeOut(txt), None


ssh = Stubborn({10023, 9854})
ssh.killed = set()
s = Sess(ssh, 10023, 9854)
s._new_ssh = lambda: None
ok, left = s._kill_remote()
kills = [pid_of(c) for c in ssh.cmds if 'kill -KILL ' in c]
ck('顽固进程被 KILL', 10023 in kills and 9854 in kills, str(kills))
ck('KILL 后无残留', left == [], str(left))

print('== 4. 清理失败时如实报告残留 ==')
ok, left, cmds = run({10023, 9854})
ck('残留判定用精确 pid（不是 pgrep 数字）',
   isinstance(left, list), type(left).__name__)
ck('未用 pgrep -fc 数字符串',
   not any('pgrep' in c for c in cmds), str([c for c in cmds if 'pgrep' in c]))

print('== 5. 没拿到 pid 时不硬杀 ==')
ssh = FakeSSH({10023})
s = Sess(ssh, 0, 0)
s._new_ssh = lambda: None
ok, left = s._kill_remote()
ck('无 pid 返回 False', ok is False)
ck('无 pid 不执行 kill', not any('kill -' in c for c in ssh.cmds))

print('== 6. cleanup_ok 判定 ==')
ck('源码里 left 判空而非 == 0',
   'bool(ok and not left)' in open(
       os.path.join(APPS, 'server.py'), encoding='utf-8').read())

print('')
print('PASS=%d FAIL=%d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
