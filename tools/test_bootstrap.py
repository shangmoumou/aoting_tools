#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动引导（bat）与版本号一致性测试。

为什么要单独一份测试：
    1. 自动安装 Python 的逻辑全在 bat 里，Python 测试跑不到它，
       只能做静态检查。曾经"改了代码没重新打包/没改版本号"，
       用户拿到的包和说好的不一致，根因都是缺少这类断言。
    2. 版本号有三个地方（portal.json / version.json / 压缩包名），
       任何一处脱节，用户就分不清自己用的是哪一版。这里锁死三者相等。
"""
import os
import re
import sys
import json
import zipfile
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

OK = 0
FAIL = []


def ck(name, cond, extra=''):
    global OK
    if cond:
        OK += 1
    else:
        FAIL.append('%s %s' % (name, extra))
        print('  FAIL  %s %s' % (name, extra))


def read(p):
    with open(os.path.join(ROOT, p), encoding='utf-8', errors='replace') as f:
        return f.read()


# ---------------------------------------------------------------- 版本号
def test_version_consistency():
    pj = json.loads(read('portal.json'))
    vj = json.loads(read('version.json'))
    sv = (pj.get('suite') or {}).get('ver')

    ck('portal.json 有总版本', bool(sv), str(sv))
    ck('version.json 顶层有 suite', vj.get('suite') == sv,
       'version=%s portal=%s' % (vj.get('suite'), sv))
    cfg = vj.get('config') or {}
    ck('version.json 把 portal.json 原样带出',
       (cfg.get('suite') or {}).get('ver') == sv)

    # 页面里显示的必须是同一份数据（不能还有硬编码的旧号）
    idx = read('index.html')
    ck('index.html 没有硬编码总版本字符串',
       not re.search(r'1\.0\.[0-9]+', idx))


def test_pack_name_follows_version():
    """压缩包名必须由 portal.json 的总版本决定，杜绝文件名与内容脱节。"""
    sys.path.insert(0, HERE)
    try:
        import pack
    except Exception as e:
        ck('导入 pack', False, str(e))
        return
    sv = (json.loads(read('portal.json')).get('suite') or {}).get('ver')
    name = os.path.basename(pack.OUT)
    ck('包名含总版本', ('_v%s.zip' % sv) in name, name)
    ck('包名不带空格', ' ' not in name, name)

    # check_version_fresh 必须能拦住"改了 portal.json 却没重跑 make_version"
    tmp = tempfile.mkdtemp()
    try:
        shutil.copytree(ROOT, os.path.join(tmp, 'aoting_tools'))
        fake = os.path.join(tmp, 'aoting_tools')
        pjp = os.path.join(fake, 'portal.json')
        cfg = json.load(open(pjp, encoding='utf-8'))
        cfg['suite']['ver'] = '9.9.9'
        json.dump(cfg, open(pjp, 'w', encoding='utf-8'))

        real_root = pack.ROOT
        pack.ROOT = fake
        try:
            err = pack.check_version_fresh()
        finally:
            pack.ROOT = real_root
        ck('版本不一致会被拦下', err is not None and '9.9.9' in str(err), str(err))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ bat 静态检查
def test_bat():
    txt = read('start_aoting_tools.bat')
    lines = txt.split('\n')

    # 1) 标签与跳转：任何 goto/call 都必须有对应标签
    labels = set()
    for l in lines:
        m = re.match(r'\s*:([A-Za-z_][A-Za-z0-9_]*)', l)
        if m and not l.strip().startswith('::'):
            labels.add(m.group(1).lower())
    for i, l in enumerate(lines, 1):
        for m in re.finditer(r'\b(goto|call)\s+:([A-Za-z_][A-Za-z0-9_]*)', l, re.I):
            ck('bat 跳转目标存在 行%d' % i,
               m.group(2).lower() in labels, m.group(0))

    # 2) 自动安装 Python 的三条路都在
    ck('bat 有 winget 安装', 'winget install' in txt)
    ck('winget 指定用户级（免管理员）', '--scope user' in txt)
    ck('bat 有官方源', 'www.python.org/ftp/python/' in txt)
    ck('bat 有国内镜像兜底',
       'mirrors.huaweicloud.com' in txt or 'npmmirror' in txt)
    # 只查真实 URL：注释里为了说明"不能用这个路径"也会提到它，
    # 直接搜整段会把注释误判成代码。
    urls = re.findall(r'https?://[^\s\'")]+', txt)
    ck('bat 里有下载源', len(urls) >= 2, str(len(urls)))
    ck('镜像路径正确（不是 /-/binary/）',
       not any('/-/binary/' in u for u in urls), str(urls))

    # 3) 用户级静默安装，不强制管理员
    ck('安装包走用户级', 'InstallAllUsers=0' in txt)
    ck('安装包自动加 PATH', 'PrependPath=1' in txt)
    ck('静默安装', '/quiet' in txt)

    # 4) 关键防护：下载到的必须是完整安装包
    ck('有大小校验，防止下到 HTML 错误页',
       'check_installer' in txt and 'LSS 5000000' in txt)

    # 5) 装完必须重新找，并且等待安装真正落地
    ck('装完重新查找 python', 'find_new_python' in txt)
    ck('轮询等待安装完成', ':ip_wait' in txt and 'timeout /t 3' in txt)
    ck('等待有上限，不会死等', 'GEQ 180' in txt)

    # 6) 装完后补 PATH，否则子脚本（远程相机 start.bat）里的 python 仍找不到
    ck('把新 python 补进当前会话 PATH',
       re.search(r'if defined PY for %%D in \("%PY%"\) do set "PATH=', txt) is not None)

    # 7) 延迟展开陷阱：不能在 for 块内判断刚设置的变量
    ck('下载未使用 for 块（避开延迟展开陷阱）',
       not re.search(r'for %%U in \(', txt))

    # 8) 仍然保留原有的商店占位程序过滤
    ck('仍然过滤 WindowsApps 占位程序', 'WindowsApps' in txt)

    # 9) 提示里引用的文件必须真的存在
    for ref in re.findall(r'tools\\([A-Za-z0-9_]+\.bat)', txt):
        ck('提示里引用的 tools\\%s 存在' % ref,
           os.path.isfile(os.path.join(ROOT, 'tools', ref)))


def test_check_env():
    p = os.path.join(ROOT, 'tools', 'check_env.bat')
    ck('tools/check_env.bat 存在', os.path.isfile(p))
    if not os.path.isfile(p):
        return
    t = open(p, encoding='utf-8', errors='replace').read()
    ck('自检覆盖 python 版本', '--version' in t)
    ck('自检覆盖端口占用', '8899' in t and 'netstat' in t)
    ck('自检覆盖占位程序', 'WindowsApps' in t)
    ck('自检覆盖关键文件', 'portal_server.py' in t and 'gate_token.txt' in t)


def test_zip_versions():
    """已经打好的包，里面的版本必须和源码一致。"""
    sv = (json.loads(read('portal.json')).get('suite') or {}).get('ver')
    cand = [f for f in os.listdir(os.path.dirname(ROOT))
            if f.startswith('aoting_tools_v') and f.endswith('.zip')]
    for name in cand:
        zp = os.path.join(os.path.dirname(ROOT), name)
        try:
            with zipfile.ZipFile(zp) as z:
                names = z.namelist()
                vj = [n for n in names if n.endswith('/version.json')]
                pj = [n for n in names if n.endswith('/portal.json')]
                if not vj or not pj:
                    ck('包内清单齐全 %s' % name, False)
                    continue
                a = json.loads(z.read(vj[0]).decode('utf-8'))
                b = json.loads(z.read(pj[0]).decode('utf-8'))
                ck('包内 version.json 与 portal.json 一致 %s' % name,
                   a.get('suite') == (b.get('suite') or {}).get('ver'))
                # 真正常量：包名里的版本 == 包内 suite.ver（对任何历史包都成立）。
                # 之前拿"当前源码版本"去比历史包，于是每个旧包都被误报。
                import re as _re
                m = _re.search(r'_v([\d.]+)\.zip$', name)
                ck('包名与包内版本一致 %s' % name,
                   bool(m) and m.group(1) == str(a.get('suite')),
                   '包名=%s 包内=%s' % (m.group(1) if m else '?', a.get('suite')))
        except Exception as e:
            ck('读取压缩包 %s' % name, False, str(e))


def main():
    test_version_consistency()
    test_pack_name_follows_version()
    test_bat()
    test_check_env()
    test_zip_versions()
    print('\n通过 %d 项，失败 %d 项' % (OK, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
