#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
远程机器状态监控 —— 测试

重点在两个别人测不到的地方：
  1) 车端采集脚本真的能跑出合法数据（本机就是 Linux，/proc 可读，
     直接跑比任何 mock 都有说服力）
  2) 服务端口与页面约定的字段一致（改了一边忘了另一边是最常见的坏法）
"""
import os
import sys
import json
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'apps', 'statusweb')

OK = FAIL = 0


def chk(name, cond, extra=''):
    global OK, FAIL
    if cond:
        OK += 1
        print('  PASS  %s %s' % (name, extra))
    else:
        FAIL += 1
        print('  FAIL  %s %s' % (name, extra))


def agent_path():
    return os.path.join(APP, 'stat_agent.py')


# ── 1. 采集脚本静态检查 ──
def test_agent_static():
    print('\n[1] stat_agent.py 静态检查')
    src = open(agent_path(), encoding='utf-8').read()
    chk('无单引号（会被 shell_quote 转义变形）', "'" not in src)
    try:
        compile(src, 'stat_agent.py', 'exec')
        chk('语法可编译', True)
    except SyntaxError as e:
        chk('语法可编译', False, str(e))
    # 用 AST 判断，不能只看文本 —— 说明文字里写了 "walrus :=" 会误报
    import ast
    tree = ast.parse(src)
    chk('不含 walrus（车上 python 可能 <3.8）',
        not any(isinstance(n, ast.NamedExpr) for n in ast.walk(tree)))
    chk('不含 f-string（同上）',
        not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree)))
    for fn in ('total_pair', 'snapshot', 'meminfo', 'loadavg', 'uptime', 'boot_ts'):
        chk('定义 %s()' % fn, ('def %s(' % fn) in src)


def run_agent(seconds, burner=False):
    """实跑采集脚本，返回解析后的行列表。"""
    procs = []
    if burner:
        for _ in range(2):
            procs.append(subprocess.Popen(
                [sys.executable, '-c',
                 'x=0\nwhile True:\n    x+=1\n']))
    try:
        p = subprocess.Popen([sys.executable, agent_path()],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(seconds)
        p.terminate()
        try:
            out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
    finally:
        for q in procs:
            q.kill()
            try:
                q.wait(timeout=3)
            except Exception:
                pass
    rows = []
    for ln in out.decode('utf-8', 'replace').splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        # 采集进程第一帧是自报 PID 的 hello 帧，不是数据帧
        if isinstance(d, dict) and d.get('type') == 'hello':
            continue
        rows.append(d)
    return rows


# ── 2. 采集脚本实跑 ──
def test_agent_live():
    print('\n[2] stat_agent.py 实跑（本机 /proc）')
    rows = run_agent(3.5)
    chk('拿到数据', len(rows) >= 2, '共 %d 行' % len(rows))
    if not rows:
        return
    d = rows[-1]
    for k in ('t', 'host', 'cpu', 'mem_used', 'mem_total', 'load',
              'up', 'nproc', 'top_cpu', 'top_mem'):
        chk('字段 %s 存在' % k, k in d)
    chk('mem_total > 0', d.get('mem_total', 0) > 0, str(d.get('mem_total')))
    chk('mem_used <= mem_total', d.get('mem_used', 0) <= d.get('mem_total', 0))
    tc = d.get('top_cpu') or []
    tm = d.get('top_mem') or []
    chk('top_cpu 不超过 5 条', len(tc) <= 5, '%d 条' % len(tc))
    chk('top_mem 不超过 5 条', len(tm) <= 5, '%d 条' % len(tm))
    chk('top_cpu 按 cpu 降序',
        all(tc[i]['cpu'] >= tc[i + 1]['cpu'] for i in range(len(tc) - 1)))
    chk('top_mem 按 rss 降序',
        all(tm[i]['rss'] >= tm[i + 1]['rss'] for i in range(len(tm) - 1)))
    chk('每条都有 pid/name/cpu/rss',
        all(set(('pid', 'name', 'cpu', 'rss')) <= set(r) for r in tc + tm))
    # 开机时长：只给秒数没有锚点，必须同时给出开机时刻才能自查
    chk('字段 boot_ts 存在', 'boot_ts' in d)
    bt, up = d.get('boot_ts', 0), d.get('up', 0)
    chk('boot_ts 与 up 自洽（开机时刻 = 现在 - 运行时长）',
        bt > 0 and abs((d['t'] - up) - bt) <= 3,
        'boot_ts=%s now-up=%.1f' % (bt, d['t'] - up))
    chk('boot_ts 早于当前时间', 0 < bt < d['t'] + 2)
    # 时间戳递增，证明是"每秒一行"而不是一次性吐完
    if len(rows) >= 2:
        chk('时间间隔约 1 秒',
            0.5 <= rows[-1]['t'] - rows[0]['t'] <= (len(rows) + 1) * 1.5,
            '%.2fs / %d 行' % (rows[-1]['t'] - rows[0]['t'], len(rows)))


# ── 3. CPU 计算是否真的反映了占用 ──
def test_agent_cpu():
    print('\n[3] CPU 占用计算（跑两个死循环）')
    rows = run_agent(3.5, burner=True)
    chk('拿到数据', len(rows) >= 2)
    if len(rows) < 2:
        return
    d = rows[-1]
    top = (d.get('top_cpu') or [{}])[0]
    chk('整机 cpu > 0', d.get('cpu', 0) > 0, str(d.get('cpu')))
    chk('top1 进程 cpu > 20', top.get('cpu', 0) > 20,
        '%s=%s' % (top.get('name'), top.get('cpu')))
    chk('top1 是 python（死循环进程）',
        'python' in (top.get('name') or '').lower(), str(top.get('name')))


# ── 4. 服务端 ──
def test_server():
    print('\n[4] server.py')
    sp = os.path.join(APP, 'server.py')
    src = open(sp, encoding='utf-8').read()
    chk('默认端口 8766', '8766' in src)
    chk('有 MAX_SESSION_SEC 上限', 'MAX_SESSION_SEC' in src)
    # 清理优先按 PID：采集进程启动即自报 pid，比匹配命令行可靠
    chk('断开会按 PID 杀车端残留', 'kill -TERM' in src and 'agent_pid' in src)
    chk('先 TERM 让 rospy 注销节点', 'signal_shutdown' in open(os.path.join(APP,'stat_agent.py'),encoding='utf-8').read())
    chk('保留 pkill 兜底', 'pkill -f' in src)
    chk('读取 stat_agent.py 发送', 'load_agent' in src)
    try:
        compile(src, 'server.py', 'exec')
        chk('语法可编译', True)
    except SyntaxError as e:
        chk('语法可编译', False, str(e))

    # 真起一次服务，验证接口
    import urllib.request
    port = 8791
    env = dict(os.environ)
    env['STATUS_MAX_SEC'] = '60'
    p = subprocess.Popen([sys.executable, sp, str(port)],
                         cwd=APP, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env)
    try:
        base = 'http://127.0.0.1:%d' % port
        ok = False
        for _ in range(40):
            try:
                urllib.request.urlopen(base + '/api/version', timeout=1)
                ok = True
                break
            except Exception:
                time.sleep(0.25)
        chk('服务能起来', ok)
        if not ok:
            return

        def get(path):
            r = urllib.request.urlopen(base + path, timeout=3)
            return json.loads(r.read().decode('utf-8'))

        def post(path, obj):
            req = urllib.request.Request(
                base + path, data=json.dumps(obj).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
            try:
                r = urllib.request.urlopen(req, timeout=5)
                return r.getcode(), json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode('utf-8'))

        _v = json.load(open(os.path.join(APP, 'app.json'), encoding='utf-8'))['ver']
        v = get('/api/version')
        chk('/api/version 返回版本号', v.get('ver') == _v, str(v))

        s = get('/api/stat')
        chk('未连接时 connected=false', s.get('connected') is False, str(s))

        code, j = post('/api/connect', {'host': '', 'port': 0, 'pwd': ''})
        chk('缺主机/端口返回 400', code == 400, str(code))

        # 密码可留空（v1.0.23）：有些机器走密钥/免密登录。
        # 留空时不该被前端式校验挡在门外，必须真的去试连接。
        code, j = post('/api/connect',
                       {'host': '127.0.0.1', 'port': 1, 'pwd': '',
                        'user': 'aoting'})
        chk('密码留空不再被拒（走密钥/免密）', code == 200, str(j)[:80])

        code, j = post('/api/connect',
                       {'host': '127.0.0.1', 'port': 1, 'pwd': 'x',
                        'user': 'aoting'})
        chk('连不上时有 err 且不崩', code == 200 and ('err' in j or 'ok' in j),
            str(j)[:80])

        code, j = post('/api/disconnect', {})
        chk('/api/disconnect 返回 200', code == 200, str(j))
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


# ── 5. 会话链路：假 SSH + 真实管道 ──
def test_session_pipe():
    """验证「服务 -> 车端脚本 -> 解析 -> latest」这条链真的通。

    为什么值得单独测：这一段跨越了 shell_quote、paramiko、JSON 解析
    三个环节，任何一处坏掉的表现都是"连上了但永远没数据"，
    而前面的静态检查和接口测试全都测不出来。
    """
    print('\n[5] 会话链路（假 SSH + 真实管道，跑真实采集数据）')
    import types
    import importlib

    r_fd, w_fd = os.pipe()

    class FakeChan(object):
        def __init__(self, fd):
            self.fd = fd

        def fileno(self):
            return self.fd

        def recv(self, n):
            try:
                return os.read(self.fd, n)
            except Exception:
                return b''

        def exit_status_ready(self):
            return False

        def recv_exit_status(self):
            return 0

    class FakeOut(object):
        def __init__(self, ch):
            self.channel = ch

    class FakeErr(object):
        def readline(self):
            return b''

    class FakeStdin(object):
        def __init__(self, sink):
            self._sink = sink
            self.channel = type('C', (),
                                {'shutdown_write': lambda s: None})()
        def write(self, d):
            self._sink.append(d)
        def flush(self):
            pass

    written = []      # 落盘时写进车端的字节
    sent = []

    class FakeClient(object):
        def set_missing_host_key_policy(self, p):
            pass

        def connect(self, *a, **k):
            pass

        def exec_command(self, cmd, timeout=None):
            sent.append(cmd)
            if cmd.startswith('cat >'):
                del written[:]          # 每次落盘清空，避免串到下一次
                return FakeStdin(written), FakeOut(FakeChan(r_fd)), FakeErr()
            if cmd.startswith('wc -c'):
                n = sum(len(x) for x in written)
                class _ROut(object):
                    channel = FakeChan(r_fd)
                    def read(self):
                        return str(n).encode()
                return None, _ROut(), FakeErr()
            return FakeStdin(written), FakeOut(FakeChan(r_fd)), FakeErr()

        def close(self):
            pass

    fake = types.ModuleType('paramiko')
    fake.SSHClient = FakeClient
    fake.AutoAddPolicy = lambda: None
    sys.modules['paramiko'] = fake

    sys.path.insert(0, APP)
    srv = importlib.import_module('server')
    srv.AGENT_SRC = open(agent_path(), encoding='utf-8').read()

    # 用真实采集脚本跑 2 秒拿到的数据灌进管道
    live = run_agent(2.5)
    chk('取到真实样本用于灌管道', len(live) >= 1, '%d 行' % len(live))
    for d in live:
        os.write(w_fd, (json.dumps(d) + '\n').encode('utf-8'))

    s = srv.StatSession('h', 22, 'aoting', 'pwd')
    try:
        t_end = time.time() + 5
        while time.time() < t_end and not s.latest:
            time.sleep(0.1)
        chk('latest 被填上', s.latest is not None)
        if s.latest:
            chk('latest 含 top_cpu', 'top_cpu' in s.latest)
            chk('latest 含 mem_total', 'mem_total' in s.latest)
            chk('age() 有效', s.age() is not None and s.age() < 5,
                str(s.age()))
        # 命令前面还有一段 source setup.bash 的前缀，所以不能按整串开头判定，
        # 要定位到 'python3 -c ' 之后再看 —— 断言写死前缀会随前缀变化误报。
        # 启动采集前会先发预清理命令（rosnode kill 旧节点），
        # 所以不能固定取 sent[0]，要按内容定位采集命令。
        # v1.0.67 起源码不再经 argv 下发：脚本已超过 MAX_ARG_STRLEN
        # (131072)，进 argv 会直接 E2BIG。改为落盘执行，所以这里断言的
        # 是"源码没进任何一条命令"，而不是"下发了 python3 -c"。
        chk('源码没有进 argv（超 131072 会 E2BIG）',
            not any(len(c) > 131072 for c in sent),
            str([len(c) for c in sent]))
        chk('走了落盘执行',
            any('python3 -u' in c and 'aoting_stat_agent' in c
                for c in sent),
            str([c[:70] for c in sent]))
        chk('落盘后回读校验字节数',
            any(c.startswith('wc -c') for c in sent))
        chk('下发内容含 agent 标记',
            any('aoting_stat_agent' in c for c in sent))
        chk('启动前先预清理旧节点',
            any('rosnode kill' in c and 'aoting_stat_agent_' in c
                for c in sent),
            str([c[:60] for c in sent]))
    finally:
        s.stop()
        time.sleep(0.3)
        try:
            os.close(w_fd)
            os.close(r_fd)
        except Exception:
            pass
        sys.modules.pop('paramiko', None)
        sys.path.remove(APP)


# ── 6. 页面 ──
def test_html():
    print('\n[6] index.html')
    src = open(os.path.join(APP, 'index.html'), encoding='utf-8').read()
    # 版本从 app.json 读，不要写死：写死后每次升版本测试就会误报，
    # 看着像"代码坏了"，实际只是号变了。
    _v = json.load(open(os.path.join(APP, 'app.json'), encoding='utf-8'))['ver']
    chk('声明 PAGE_VER ' + _v, ("PAGE_VER = '%s'" % _v) in src)
    for hook in ('/api/connect', '/api/disconnect', '/api/stat', '/api/logs'):
        chk('调用 %s' % hook, hook in src)
    chk('渲染 top_cpu', 'top_cpu' in src)
    chk('渲染 top_mem', 'top_mem' in src)
    chk('每秒轮询', 'setInterval(poll, 1000)' in src)
    chk('用户名固定 aoting', "value=\"aoting\"" in src or "user:'aoting'" in src)
    chk('主机默认 frp.aotbot.com', 'frp.aotbot.com' in src)
    chk('有复制快照', 'copySnap' in src)
    chk('有剩余时间提示', 'fmtLeft' in src)
    # 「已运行」改叫「开机时长」：原标签会被读成"本次连接跑了多久"，
    # 与系统开机时长相差几个数量级。
    chk('概览标签为「开机时长」', '开机时长' in src)
    chk('不再用「已运行」当标签', '>已运行<' not in src)
    chk('定义 fmtBoot()', 'function fmtBoot(' in src)
    chk('定义 fmtConn()', 'function fmtConn(' in src)
    chk('渲染 boot_ts', 'boot_ts' in src)
    # v1.0.36 起：右上角改成「剩余 mm:ss」倒计时，不再是「本次已连多久」
    chk('显示剩余时间倒计时', "剩余 ' + fmtLeft(lf)" in src)
    chk('不再显示本次时长', '本次 ' not in src)
    chk('剩余不足阈值标红', 'lf <= WARN_SEC' in src)
    chk('有延长按钮', 'id="bext"' in src)


# ── 7. 配置与打包必须项 ──
def test_app_json():
    print('\n[7] app.json / start.bat')
    j = json.load(open(os.path.join(APP, 'app.json'), encoding='utf-8'))
    svc = j.get('service') or {}
    chk('type=service', j.get('type') == 'service')
    chk('端口 8766', svc.get('port') == 8766, str(svc.get('port')))
    chk('探测路径 /api/version', svc.get('probe') == '/api/version')
    chk('声明 start.bat', svc.get('start') == 'start.bat')
    for f in ('server.py', 'stat_agent.py', 'start.bat'):
        chk('files 含 %s' % f, f in (svc.get('files') or []))
        chk('%s 真实存在' % f, os.path.isfile(os.path.join(APP, f)))

    raw = open(os.path.join(APP, 'start.bat'), 'rb').read()
    chk('start.bat 有 CRLF', b'\r\n' in raw)
    stripped = raw.replace(b'\r\n', b'')
    chk('start.bat 无裸 LF/CR', b'\n' not in stripped and b'\r' not in stripped)
    try:
        raw.decode('gbk')
        chk('start.bat 是 GBK', True)
    except Exception as e:
        chk('start.bat 是 GBK', False, str(e))
    chk('start.bat 提及 8766', b'8766' in raw)


def main():
    print('=' * 60)
    print(' 远程机器状态监控 测试')
    print('=' * 60)
    test_agent_static()
    test_agent_live()
    test_agent_cpu()
    test_server()
    test_session_pipe()
    test_html()
    test_app_json()
    print('\n' + '=' * 60)
    print(' 通过 %d  失败 %d' % (OK, FAIL))
    print('=' * 60)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
