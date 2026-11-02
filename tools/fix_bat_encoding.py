#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_bat_encoding.py —— 把 .bat / .cmd 转成 Windows cmd 能正确执行的格式

【为什么需要这个脚本】
    本项目在 Linux/macOS 上开发打包，生成的 .bat 天然是：
        · UTF-8 编码（无 BOM）
        · LF 换行
    而 Windows 的 cmd.exe 是：
        · 按系统 ANSI 代码页（简体中文 = GBK/936）解码文件字节
        · 以 CRLF 作为行分隔

    两者叠加会同时触发两个故障，而且症状极具迷惑性：
        1) UTF-8 中文字节被 GBK 解码 -> 满屏乱码
        2) LF 不被识别为行结束 -> 多行被"吞"成一行，
           rem 注释行被当成命令执行，报
           "'xxx' 不是内部或外部命令，也不是可运行的程序"

    典型报错（本项目 v1.0.11 实测）：
        '嬪悗鍙堢Щ鍔ㄨ繃鏂囦欢澶广€? 不是内部或外部命令
        '------------------------------------------' 不是内部或外部命令

【本脚本做什么】
    统一重写为 GBK 编码 + CRLF 换行。
    选 GBK 而不是「UTF-8 BOM + chcp 65001」的原因：
        · chcp 65001 在部分 Windows 版本上对管道/重定向处理不可靠
        · GBK 是简体中文 Windows 的原生代码页，Win7 ~ Win11 通吃
        · 用户的控制台代码页就是 936，无需额外 chcp

【幂等】
    已符合格式的文件会被跳过（不重复写），打包前可放心反复执行。
"""
from __future__ import print_function
import os
import sys
import glob

TARGET_EXT = ('.bat', '.cmd')


def _log(s=''):
    sys.stdout.write(s + '\n')
    sys.stdout.flush()


def needs_fix(path):
    """返回 (是否需要修复, 原因)。"""
    with open(path, 'rb') as f:
        raw = f.read()
    # 已是 GBK + 纯 CRLF？
    # 注意：剥掉所有 CRLF 后，必须同时检查残留的 LF 和 CR。
    # 曾经只查 LF，结果 "\r\r\n"（裸 CR）被判成已合规放行 —— 那正是
    # 某次替换脚本在已有 CRLF 上再 replace('\n','\r\n') 造成的。
    stripped = raw.replace(b'\r\n', b'')
    crlf_ok = (b'\n' not in stripped) and (b'\r' not in stripped)
    try:
        raw.decode('gbk')
        gbk_ok = True
    except Exception:
        gbk_ok = False
    if crlf_ok and gbk_ok:
        return False, 'already ok'
    why = []
    if not crlf_ok:
        why.append('LF->CRLF')
    if not gbk_ok:
        why.append('->GBK')
    return True, '+'.join(why)


def fix_file(path):
    """把单个文件转成 GBK + CRLF。返回 (是否改写, 说明)。"""
    with open(path, 'rb') as f:
        raw = f.read()

    # 解码：优先 UTF-8，失败退回 GBK（已是 GBK 的情况）
    try:
        txt = raw.decode('utf-8')
    except UnicodeDecodeError:
        txt = raw.decode('gbk', 'replace')

    # 规范换行：CRLF / CR / LF 统一成 LF，最后统一转 CRLF
    txt = txt.replace('\r\n', '\n').replace('\r', '\n')

    # 编码成 GBK；个别无法表示的字符替换成 ?，避免整个打包失败
    data = txt.encode('gbk', 'replace')
    data = data.replace(b'\n', b'\r\n')

    if data == raw:
        return False, 'unchanged'
    with open(path, 'wb') as f:
        f.write(data)
    return True, 'rewritten'


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)

    files = []
    for ext in TARGET_EXT:
        files += glob.glob('**/*' + ext, recursive=True)
    files = sorted(set(files))

    if not files:
        _log('[WARN] 没有找到任何 .bat / .cmd 文件')
        return 0

    changed, skipped, failed = [], [], []
    for p in files:
        try:
            need, why = needs_fix(p)
            if not need:
                skipped.append((p, why))
                continue
            did, res = fix_file(p)
            (changed if did else skipped).append((p, why if did else res))
        except Exception as e:
            failed.append((p, repr(e)))

    _log('==== bat 编码/换行规范化 ====')
    _log('  根目录: %s' % root)
    _log('  扫描到 %d 个文件' % len(files))
    for p, r in changed:
        _log('  [改写] %-40s %s' % (p, r))
    for p, r in skipped:
        _log('  [跳过] %-40s %s' % (p, r))
    for p, r in failed:
        _log('  [失败] %-40s %s' % (p, r))

    if failed:
        _log('[FAIL] %d 个文件处理失败' % len(failed))
        return 1
    _log('[OK] 改写 %d 个，跳过 %d 个，全部为 GBK + CRLF' % (len(changed), len(skipped)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
