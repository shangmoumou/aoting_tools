#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
远程机器状态监控 —— python 进程身份识别 + 免密优先
"""
import os
import re
import sys
import json
import time
import importlib.util

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


def load_agent():
    spec = importlib.util.spec_from_file_location(
        'ag', os.path.join(APP, 'stat_agent.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── 1. 命令行 → 节点名 ──
def test_node_parse():
    print('\n[1] 从 cmdline 认出 ROS 节点')
    ag = load_agent()
    cases = [
        # roslaunch / rosrun 一定会带 __name:=，最准
        (['/usr/bin/python3', '/opt/ros/noetic/lib/cam/cam_node.py',
          '__name:=camera_node', '__log:=/tmp/x.log'], 'camera_node'),
        # 直接 python 跑脚本，取脚本名
        (['python', '/home/aoting/ws/src/nav/move.py'], 'move'),
        (['python3', '/home/aoting/x.sh'], 'x'),
        # python -m pkg
        (['python3', '-m', 'deep_perception'], 'deep_perception'),
        # 认不出就返回空，前端退回进程名
        (['python3', '-c', 'x=1'], ''),
        ([], ''),
    ]
    for argv, want in cases:
        got = ag._node_from_argv(argv)
        chk('解析 %s' % (want or '(空)'), got == want, '-> %r' % got)

    # 非 python 进程不读 cmdline：进程名本身就是节点名
    chk('非 python 跳过读 cmdline', ag._node_of(1, 'deep_perception') == '')


# ── 2. 采集脚本真的输出 node ──
def test_agent_output():
    print('\n[2] 采集脚本输出含 node 字段')
    ag = load_agent()
    src = open(os.path.join(APP, 'stat_agent.py'), encoding='utf-8').read()
    chk('rows 带 node', '"node": node' in src)

    # 真跑一次，确认字段在 JSON 里（本机 /proc 可读）
    try:
        import subprocess
        p = subprocess.Popen([sys.executable, os.path.join(APP, 'stat_agent.py')],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        time.sleep(2.5)
        p.terminate()
        out = p.stdout.read().decode('utf-8', 'replace')
        line = []
        for l in out.splitlines():
            l = l.strip()
            if not l.startswith('{'):
                continue
            try:
                # hello 帧（自报 PID）不是数据帧
                if json.loads(l).get('type') == 'hello':
                    continue
            except Exception:
                pass
            line.append(l)
        chk('拿到输出', bool(line))
        if line:
            d = json.loads(line[0])
            top = d.get('top_cpu') or []
            chk('top_cpu 每项含 node',
                bool(top) and all('node' in r for r in top),
                'n=%d' % len(top))
            chk('cpu/内存正常', d.get('mem_total', 0) > 0,
                'mem=%s/%s' % (d.get('mem_used'), d.get('mem_total')))
    except Exception as e:
        chk('实跑采集脚本', False, repr(e))


# ── 3. 免密优先 ──
def test_auth_flow():
    print('\n[3] 免密优先，失败才要密码')
    src = open(os.path.join(APP, 'server.py'), encoding='utf-8').read()
    chk('捕获 AuthenticationException', 'AuthenticationException' in src)
    chk('免密失败置 need_pwd', 'self.need_pwd = True' in src)
    chk('接口回 needPwd', "'needPwd'" in src)
    chk('免密时放开 agent/key',
        'allow_agent=not self.pwd' in src and 'look_for_keys=not self.pwd' in src)

    # 认证失败后必须退出循环，不能卡在"连接中"
    chk('失败后 running=False', 'self.running = False' in src)


# ── 4. 预置密码的解析（短密码 / 空格）──
def test_default_pwd():
    print('\n[4] 预置密码解析')
    spec = importlib.util.spec_from_file_location(
        'srv', os.path.join(APP, 'server.py'))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)

    fp = os.path.join(ROOT, 'tools', 'status_pwd.txt')
    if not os.path.exists(fp):
        chk('随包提供 status_pwd.txt', False)
        return
    chk('随包提供 status_pwd.txt', True)
    old = open(fp, encoding='utf-8').read()
    try:
        open(fp, 'w', encoding='utf-8').write('# 备注\naoting\n')   # 6 位
        chk('6 位短密码可读', srv._default_pwd() == 'aoting',
            '-> %r' % srv._default_pwd())
        open(fp, 'w', encoding='utf-8').write('# 备注\npwd=My Secret 123\n')
        chk('带空格密码可读', srv._default_pwd() == 'My Secret 123',
            '-> %r' % srv._default_pwd())
        open(fp, 'w', encoding='utf-8').write('# 只有注释\n')
        chk('只有注释 -> 空', srv._default_pwd() == '',
            '-> %r' % srv._default_pwd())
    finally:
        open(fp, 'w', encoding='utf-8').write(old)


# ── 5. 前端 ──
def test_frontend():
    print('\n[5] 密码框常显 / 用户名可改 / 表格显示节点名')
    html = open(os.path.join(APP, 'index.html'), encoding='utf-8').read()
    # v1.0.62 起：密码框改成常显（原来要等免密失败才弹，使用者不知道该不该输），
    # "点此输入"入口随之取消。所以这两条断言的预期反过来了。
    chk('密码框默认可见（不再 display:none）',
        re.search(r'id="pwdbox"(?![\s\S]{0,40}display:none)', html) is not None)
    chk('已取消"点此输入"入口', 'id="lnkpwd"' not in html)
    chk('用户名不再是 disabled', 'id="user" value="aoting" disabled' not in html)
    chk('连接时用户名取自输入框',
        re.search(r"const user\s*=.*\$\('user'\)\.value", html) is not None)
    chk('needPwd 时弹出', 'if (j.needPwd)' in html and 'showPwd()' in html)
    chk('记住密码', "localStorage.setItem('st_pwd'" in html)
    chk('表格优先显示 node', 'const main = node || r.name' in html)
    chk('副行显示进程名', 'const sub  = node ? r.name' in html)
    chk('快照复制带 node', "r.node + ' [' + r.name + ']'" in html)
    chk('无 .hl 样式泄漏', 'td.nm .hl' in html)


def main():
    test_node_parse()
    test_agent_output()
    test_auth_flow()
    test_default_pwd()
    test_frontend()
    print('\n' + '=' * 60)
    print(' 通过 %d  失败 %d' % (OK, FAIL))
    print('=' * 60)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
