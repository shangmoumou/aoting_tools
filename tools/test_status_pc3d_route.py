# -*- coding: utf-8 -*-
"""回归：/api/pc3d 必须能被 POST 调到（前端就是用 POST 调的）。

踩过的坑（别再犯）：
  最早这段处理整段写在 do_GET 里，而 do_GET 里根本没有变量 b，
  一进去就 NameError，handler 抛异常 → 连接被掐 → 前端 "Failed to fetch"；
  同时 do_POST 里又没有这一条 → POST 走到末尾 404。
  两个错叠在一起的表现就是「勾了开关，车端永远显示未开启」。
所以这里同时盯两头：POST 里必须有，且处理函数不能再用不存在的 b。
"""
from __future__ import print_function

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'apps', 'statusweb', 'server.py')

FAIL = []


def ok(name, cond, extra=''):
    print(('ok   ' if cond else 'FAIL ') + name + (('  ' + str(extra)) if extra else ''))
    if not cond:
        FAIL.append(name)


def _body_of(src, header):
    """取出某个 def 的函数体（按缩进切到下一个同级 def/class）。"""
    i = src.find(header)
    if i < 0:
        return None
    # 找到该行缩进
    line_end = src.find('\n', i)
    indent = len(src[i:line_end]) - len(src[i:line_end].lstrip())
    j = src.find('\n', line_end)
    out = []
    while j > 0:
        nxt = src.find('\n', j + 1)
        if nxt < 0:
            out.append(src[j:])
            break
        ln = src[j + 1:nxt]
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            break
        out.append(ln)
        j = nxt
    return '\n'.join(out)


def main():
    src = io.open(SRC, encoding='utf-8').read()

    post = _body_of(src, 'def do_POST(self):')
    get = _body_of(src, 'def do_GET(self):')
    ok('do_POST 存在', post is not None)
    ok('do_GET 存在', get is not None)
    if post is None or get is None:
        return

    # 1) POST 必须能路由到 /api/pc3d
    ok('do_POST 里有 /api/pc3d', "'/api/pc3d'" in post)

    # 2) 有独立处理函数，且它接收 b（不再依赖外层不存在的变量）
    m = re.search(r'def api_pc3d\(self,\s*b\)', src)
    ok('有 api_pc3d(self, b) 处理函数', m is not None)
    if m:
        body = _body_of(src, 'def api_pc3d(self, b):')
        ok('api_pc3d 用 b 取值（不是未定义的全局）',
           'b' in body and ".get('on')" in body)

    # 3) do_GET 里若还有 pc3d 分支，不能再出现裸的 b.get
    if "'/api/pc3d'" in get:
        seg = get[get.find("'/api/pc3d'"):]
        seg = seg[:seg.find('return') + 200]
        ok('do_GET 的 pc3d 分支不引用未定义的 b',
           'b.get' not in seg and 'b}' not in seg)

    # 4) 标志文件回读：不能只信"命令发出去了"
    m2 = re.search(r'def send_flag\(self, name, on\):', src)
    ok('send_flag 存在', m2 is not None)
    if m2:
        body = _body_of(src, 'def send_flag(self, name, on):')
        ok('send_flag 写完回读确认文件真的在',
           'test -f' in body and '回读' in body)

    print()
    print('FAIL %d' % len(FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
