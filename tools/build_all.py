#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键重建所有工具：跑每个 app.json 里声明的 build.py，再刷新 version.json。

改了任何 src/ 里的代码后跑这个就行，不用一个个进目录。
"""
import os
import sys
import json
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APPS = os.path.join(ROOT, 'apps')

ok = fail = 0
for d in sorted(os.listdir(APPS)):
    aj = os.path.join(APPS, d, 'app.json')
    if not os.path.isfile(aj):
        continue
    cfg = json.load(open(aj, encoding='utf-8'))
    bp = cfg.get('build')
    if not bp:
        print('  skip %-10s (无 build 脚本)' % d)
        continue
    full = os.path.join(APPS, d, bp)
    if not os.path.isfile(full):
        print('  skip %-10s (build 脚本不存在)' % d)
        continue
    print('  build %-10s ...' % d, end=' ')
    r = subprocess.run([sys.executable, os.path.basename(bp)],
                       cwd=os.path.dirname(full),
                       capture_output=True, text=True)
    if r.returncode == 0:
        print('ok')
        ok += 1
    else:
        print('FAIL')
        print(r.stdout, r.stderr)
        fail += 1

print('\n构建完成：成功 %d 失败 %d' % (ok, fail))

r = subprocess.run([sys.executable, os.path.join(HERE, 'make_version.py')],
                   capture_output=True, text=True)
sys.stdout.write(r.stdout)
sys.exit(1 if fail else 0)
