#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包成可分发 zip。

存在的意义：曾经把名为 `nul` 的文件打进了包。
那是 Windows 保留设备名，用户解压必弹 0x800704B0，
且不知道该跳过什么。所以打包前必须扫一遍。
"""
import os, sys, zipfile, hashlib, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def suite_ver():
    """从 portal.json 读总版本号。

    为什么包名必须由它决定：曾经出现过"文件名写 v1.0.10、页面里还是
    1.0.8"，用户拿到后根本分不清自己用的是哪一版。版本号只有一个来源，
    包名跟着它走，就不可能再脱节。
    """
    p = os.path.join(ROOT, 'portal.json')
    try:
        with open(p, encoding='utf-8') as f:
            return (json.load(f).get('suite') or {}).get('ver')
    except Exception:
        return None


def out_name():
    v = suite_ver()
    base = os.path.basename(ROOT)
    return '%s_v%s.zip' % (base, v) if v else base + '.zip'


OUT = os.path.join(os.path.dirname(ROOT), out_name())

RESERVED = {'nul','con','aux','prn','conin$','conout$'} | \
           {'com%d' % i for i in range(1, 10)} | \
           {'lpt%d' % i for i in range(1, 10)}

def sha(b): return hashlib.sha256(b).hexdigest()


def _bat_format_problem(data):
    """检查 bat/cmd 字节是否为「GBK 编码 + CRLF 换行」。

    只比对 sha256 抓不出格式问题 —— 源码若是 UTF-8+LF，包里忠实复制
    过去照样是错的。所以打包后必须单独断言一次。

    返回问题描述字符串；格式正确返回 None。
    """
    if b'\r\n' not in data:
        return '无 CRLF'
    stripped = data.replace(b'\r\n', b'')
    if b'\n' in stripped or b'\r' in stripped:
        return '存在裸 LF/CR'
    try:
        data.decode('gbk')
    except Exception:
        return '非 GBK 编码'
    return None

def verify(zip_path=None, root=None):
    """打包后逐文件比对：压缩包里的每个文件都必须和当前源码一致。

    为什么必须有这一步：曾经出现过"代码改了、压缩包没重打"，于是
    发出去的包里压根没有新功能，而打包命令本身还打印 [OK]，
    从外面完全看不出来。比对 sha256 能让这种情况立刻失败。
    """
    zip_path = zip_path or OUT
    root = root or ROOT
    if not os.path.isfile(zip_path):
        return False, '压缩包不存在: %s' % zip_path
    base = os.path.basename(root)
    bad, n = [], 0
    with zipfile.ZipFile(zip_path) as z:
        for it in z.infolist():
            if it.is_dir(): continue
            name = it.filename
            if not name.startswith(base + '/'): continue
            rel = name[len(base) + 1:]
            fp = os.path.join(root, rel)
            if not os.path.isfile(fp):
                bad.append('包里有、源码里没有: ' + rel); continue
            n += 1
            zb = z.read(it)
            if sha(zb) != sha(open(fp, 'rb').read()):
                bad.append('内容不一致（包是旧的）: ' + rel)
            # bat/cmd 必须是 GBK + CRLF。只比对 sha 抓不出"包里是 UTF-8+LF"，
            # 因为源码本身可能就是错的 —— 必须单独断言格式。
            if rel.lower().endswith(('.bat', '.cmd')):
                why = _bat_format_problem(zb)
                if why:
                    bad.append('bat 格式错误(%s): %s' % (why, rel))
    if bad:
        return False, '; '.join(bad[:5]) + (' …' if len(bad) > 5 else '')
    if n == 0:
        return False, '包里没有比对到任何文件'
    return True, '%d 个文件全部与源码一致' % n

def scan():
    bad = []
    for r, ds, fs in os.walk(ROOT):
        ds[:] = [d for d in ds if d != '__pycache__']
        for f in fs:
            if f.lower().split('.')[0] in RESERVED:
                bad.append(os.path.join(r, f))
    return bad

def check_version_fresh():
    """version.json 里的总版本必须和 portal.json 一致。

    典型事故：改了 portal.json 的版本号，却忘了跑 make_version.py，
    于是压缩包叫 v1.0.11、页面里仍显示 1.0.8 —— 用户完全分不清
    自己拿到的是哪一版。这里在打包前直接拦下。
    """
    vj = os.path.join(ROOT, 'version.json')
    if not os.path.isfile(vj):
        return 'version.json 不存在，请先跑 tools/make_version.py'
    try:
        with open(vj, encoding='utf-8') as f:
            got = json.load(f).get('suite')
    except Exception as e:
        return 'version.json 读取失败: %s' % e
    want = suite_ver()
    if want and got != want:
        return ('version.json 的总版本 %s 与 portal.json 的 %s 不一致 —— '
                '请先跑 python3 tools/make_version.py' % (got, want))
    return None


def main():
    verr = check_version_fresh()
    if verr:
        print('[FAIL] ' + verr)
        return 1

    bad = scan()
    if bad:
        print('[FAIL] 发现 Windows 保留设备名文件，拒绝打包：')
        for b in bad:
            print('   ', b)
            try:
                os.remove(b)
                print('     -> 已自动删除')
            except Exception as e:
                print('     -> 删除失败:', e)
        return 1

    # .bat / .cmd 必须转成 GBK + CRLF 再打包。
    # 本项目在 Linux 上生成文件，天然是 UTF-8 + LF，而 Windows cmd 按
    # GBK 解码、以 CRLF 分行 —— 两者叠加会同时触发：
    #   1) 中文全部乱码
    #   2) rem 注释行被当成命令执行，满屏 "'xxx' 不是内部或外部命令"
    # v1.0.11 正是漏了这一步，对方双击后脚本完全不执行。
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import fix_bat_encoding
        if fix_bat_encoding.main() != 0:
            print('[FAIL] bat 编码/换行规范化失败，拒绝打包')
            return 1
    except Exception as e:
        print('[FAIL] 无法执行 bat 编码规范化: %r' % (e,))
        return 1

    # 打包时若含真实 token，必须提醒：这个包发出去就等于把仓库只读权
    # 交出去了。不阻止（有时就是要内置后分发），但不能悄悄过去。
    tk = os.path.join(ROOT, 'tools', 'gate_token.txt')
    if os.path.isfile(tk):
        print('=' * 58)
        print('[警告] 本包包含 tools/gate_token.txt（git 只读令牌）')
        print('       拿到这个包的人都能读取账号库。')
        print('       只发给可信同事；若只是自己用，可忽略。')
        print('=' * 58)

    n = 0
    with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as z:
        for r, ds, fs in os.walk(ROOT):
            ds[:] = [d for d in ds if d != '__pycache__']
            for f in fs:
                fp = os.path.join(r, f)
                z.write(fp, os.path.relpath(fp, os.path.dirname(ROOT)))
                n += 1
    print('[OK] %s  (%d 个文件)' % (OUT, n))
    ok, msg = verify()
    if not ok:
        print('[FAIL] 打包后校验未通过：' + msg)
        return 1
    print('[OK] 校验通过：' + msg)
    return 0

if __name__ == '__main__':
    sys.exit(main())
