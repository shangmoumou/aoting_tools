#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""访问记录落点测试（真跑，不只查字符串）

背景：
    记录原来写在 /tmp 下。/tmp 在多数系统上是 tmpfs（重启即清空），
    即便在磁盘上也会被 systemd-tmpfiles 按天清理，且权限 1777 谁都能删，
    作为"事后可追溯"的留痕靠不住。现挪到 /home/aoting/user_config。

为什么真跑而不是查字符串：
    只断言"源码里出现了这个路径"，证明不了"这句 shell 真的会把文件
    写到那里"。这里把命令抠出来，替换成临时目录，在 bash 里真执行两遍，
    看文件落在哪、是追加还是覆盖、目录不可写时是否退回。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'apps', 'statusweb')
SRV = os.path.join(APP, 'server.py')

ok = fail = 0


def ck(name, cond):
    global ok, fail
    if cond:
        ok += 1
        print('  PASS ' + name)
    else:
        fail += 1
        print('  FAIL ' + name)


def read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def main():
    srv = read(SRV)

    print('── 1. 落点常量 ──')
    m = re.search(r"^RGB_LOG_NAME = '([^']+)'", srv, re.M)
    ck('RGB_LOG_NAME 已定义', m is not None)
    m2 = re.search(r"^RGB_DIR = '([^']+)'", srv, re.M)
    ck('RGB_DIR 已定义', m2 is not None)
    ck('落点在 user_config 下',
       m2 is not None and m2.group(1) == '/home/aoting/user_config')
    ck('不再把 /tmp 作为主落点',
       "/tmp/aoting_statusweb_access.log" not in srv)
    ck('与地图库同目录（user_config）',
       '/home/aoting/user_config/map_database' in srv)

    print('── 2. 抠出命令构建，真跑 ──')
    seg = srv[srv.index('def rgb_record_access'):]
    # 注意：函数里第一个 try 是"按需 import paramiko"，必须切到第二个
    # （真正发命令那个），否则抠不到 whoami / cmd。
    seg = seg[:seg.index('    try:\n        import paramiko\n        ssh')]
    ck('命令段已抠到', 'RGB_LOG_NAME' in seg and 'mkdir -p' in seg)

    # 复制一份可执行的构建语句：把 RGB_DIR 换成临时目录
    tmp = tempfile.mkdtemp(prefix='rgb_log_')
    fake_dir = os.path.join(tmp, 'home', 'aoting', 'user_config')
    fb = os.path.join(tmp, 'fallback')
    os.makedirs(fb)

    body = seg[seg.index("    whoami = ("):]
    body = body.replace('RGB_DIR + \'; \'', 'FAKE_DIR + \'; \'')
    body = body.replace("'mkdir -p ' + RGB_DIR", "'mkdir -p ' + FAKE_DIR")
    body = body.replace('D=\' + RGB_DIR', 'D=\' + FAKE_DIR')
    body = re.sub(r'\|\| D=/tmp;', '|| D=%s;' % fb, body)
    # 不要动 \\n：源文件里就是两个字符（反斜杠+n），原样 compile 后
    # printf 才能拿到正确的换行转义；再替换一次会变成三个字符而写歪。

    ns = {
        'FAKE_DIR': fake_dir,
        'RGB_LOG_NAME': 'aoting_statusweb_access.log',
        'shell_quote': lambda s: "'" + s.replace("'", "'\\''") + "'",
    }
    code = ('def build(account, user, host, port):\n'
            '    pc, who, ips = "PC", "someone", "1.2.3.4"\n'
            + body.replace('    pc, who, ips = _local_who_ips()', '') + '\n'
            '    return cmd\n')
    try:
        exec(compile(code, '<build>', 'exec'), ns)
        cmd = ns['build']('wangqianqian', 'aoting', 'frp.aotbot.com', 21723)
    except Exception as e:
        ck('命令构建可执行', False)
        print('     %s' % e)
        shutil.rmtree(tmp, True)
        return

    ck('命令构建可执行', isinstance(cmd, str) and '>>' in cmd)
    ck('命令里是追加而非覆盖', '>>' in cmd and ' > "' not in cmd)
    ck('命令带可写性判断', '[ -w "$D" ]' in cmd)
    ck('命令带回退分支', '|| D=' in cmd)
    ck('命令里 mkdir 了目标目录', 'mkdir -p ' + fake_dir in cmd)

    # 真跑两遍，验证"追加"而不是覆盖
    for i in (1, 2):
        r = subprocess.run(['bash', '-c', cmd],
                           capture_output=True, text=True)
        ck('第%d次执行成功（rc=0）' % i, r.returncode == 0)

    logf = os.path.join(fake_dir, 'aoting_statusweb_access.log')
    ck('文件落在 user_config 目录', os.path.isfile(logf))
    if os.path.isfile(logf):
        with open(logf, encoding='utf-8') as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        ck('两遍执行留下两行（是追加不是覆盖）', len(lines) == 2)
        ck('行内含账号', all('wangqianqian' in x for x in lines))
        ck('行内含车端时间', all('car_time=' in x for x in lines))
        ck('行内含 from=statusweb', all('from=statusweb' in x for x in lines))

    print('── 3. 目录不可写时回退 ──')
    # 造一个 mkdir 必失败的路径，验证退回 fallback
    cmd2 = cmd.replace('mkdir -p ' + fake_dir, 'mkdir -p /proc/forbidden_x')
    cmd2 = cmd2.replace('D=' + fake_dir, 'D=/proc/forbidden_x')
    r2 = subprocess.run(['bash', '-c', cmd2], capture_output=True, text=True)
    ck('不可写目录时命令仍成功', r2.returncode == 0)
    ck('回退目录拿到了记录',
       os.path.isfile(os.path.join(fb, 'aoting_statusweb_access.log')))

    shutil.rmtree(tmp, True)

    print('── 4. 页面文案 ──')
    html_path = os.path.join(APP, 'index.html')
    if os.path.isfile(html_path):
        html = read(html_path)
        ck('页面未写死 /tmp 日志路径',
           '/tmp/aoting_statusweb_access.log' not in html)

    print('')
    print('通过 %d / 失败 %d' % (ok, fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
