# -*- coding: utf-8 -*-
"""端到端：真跑采集脚本，验证频率可调 + task/queue 出现在输出里"""
import subprocess, sys, time, json, os

AGENT = '/data/workspace/aoting_tools/apps/statusweb/stat_agent.py'
PASS = FAIL = 0
def ck(c, n, e=''):
    global PASS, FAIL
    if c: PASS += 1; print('PASS  ' + n)
    else: FAIL += 1; print('FAIL  ' + n + ('  | ' + str(e) if e else ''))

def run(hz, secs):
    """以给定频率跑 secs 秒，返回 (行数, 最后一帧 dict)。

    必须【边读边数】，不能让父进程干等再一次性收：
    管道容量有限，没人读的话采集脚本会卡在 write 上，数出来的帧数
    反映的是管道缓冲而不是真实帧率 —— 实测同样 3 秒，干等只数到
    16 帧，边读边数是 27 帧。真实的服务端（server.py）本来就是
    持续读的，所以边读才是对的做法。
    """
    p = subprocess.Popen([sys.executable, '-u', AGENT],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    lines = []
    try:
        p.stdin.write((json.dumps({'hz': hz}) + '\n').encode())
        p.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < secs:
            ln = p.stdout.readline().decode('utf-8', 'replace')
            if not ln:
                break
            if ln.startswith('{'):
                lines.append(ln)
        p.terminate()
        p.communicate(timeout=10)
    except Exception:
        p.kill(); raise
    last = None
    for l in lines:
        try: last = json.loads(l)
        except Exception: continue
        if isinstance(last, dict) and last.get('type') == 'hello':
            last = None
            continue
    return len(lines), last

# 默认 1Hz（不写 stdin）
p = subprocess.Popen([sys.executable, '-u', AGENT], stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
time.sleep(3.2)
p.terminate()
out, _ = p.communicate(timeout=10)
n = len([l for l in out.decode('utf-8','replace').split('\n') if l.startswith('{')])
ck(2 <= n <= 5, '默认频率约 3 帧/3.2 秒（1Hz）', '实际 %d 帧' % n)

# 5Hz
n, last = run(5, 3.0)
ck(10 <= n <= 20, '5Hz：3 秒约 15 帧', '实际 %d 帧' % n)

# 10Hz
n, last = run(10, 3.0)
ck(20 <= n <= 34, '10Hz：3 秒约 30 帧', '实际 %d 帧' % n)

# 0.5Hz
n, last = run(0.5, 3.0)
ck(0 <= n <= 3, '0.5Hz：3 秒约 1 帧', '实际 %d 帧' % n)

# 输出字段完整性
ck(last is not None, '拿到有效帧')
if last:
    for k in ('cpu','mem_used','top_cpu','top_mem','faults','hw','slam',
              'swver','task','queue','nav','hz'):
        ck(k in last, '输出含字段 %s' % k)
    ck('groups' in (last.get('queue') or {}), 'queue.groups 存在')
    ck((last.get('nav') or {}).get('topic') == '/cmd_vel', 'nav 带话题名')
    ck(bool((last.get('nav') or {}).get('err')), '无 ROS 时导航栏也有原因说明')
    ck('statusText' in (last.get('task') or {}), 'task.statusText 存在')
    ck(last.get('hz') == 0.5, '输出回传当前频率 0.5', last.get('hz'))
    # 沙箱无 ROS → task/queue 应给出明确原因而不是静默空白
    tq = last.get('task') or {}
    ck(bool(tq.get('err')), '无 ROS 时任务栏有原因说明', tq.get('err'))

print('\n通过 %d / 失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
