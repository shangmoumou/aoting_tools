#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫描 apps/ 下所有工具，生成 version.json。

新增一个工具只需要：
    1. 建 apps/<id>/ 目录
    2. 放 app.json（描述 + 版本）
    3. 跑本脚本
门户会自动多出一张卡片。不需要改门户的任何代码。

为什么版本号和 sha256 都要：
    只比版本号 -> 改了文件忘了改号，用户永远收不到更新
    只比 hash   -> 用户不知道自己升到了哪一版
    两个都留，且这里会自动校验 app.json 与文件里的版本号是否一致，
    不一致直接报错，把人为失误挡在发布之前。
"""
import os
import re
import sys
import json
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APPS = os.path.join(ROOT, 'apps')

# 目录还在、但门户不再单列入口的卡片。
# camweb（远程相机实时预览）已并入 statusweb 的「彩色相机信息」：
# 一个页面里既能看状态又能看画面，来回切两个标签页没有意义。
# 这里用隐藏而不是删除目录 —— 源码留着，万一新版有问题还能回退。
HIDDEN_APPS = set(['camweb'])

# 门户卡片顺序。原来是按目录名字母序排，而字母序恰好把最常用的排到了后面
# （imuweb / slamweb / statusweb / webbag）—— 天天要看的状态监控得往下翻。
# 这里按使用频率显式指定：状态监控第一，其后 bag 分析 / IMU / SLAM。
# 没列进来的新工具排在后面，按字母序，不会漏。
APP_ORDER = ['statusweb', 'webbag', 'imuweb', 'slamweb']

PRECACHE_BASE = ['./', './index.html', './sw.js', './version.json', './brand_logo.png', './brand.ico',
                './brand.ico']


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def detect_ver(path, pat):
    try:
        txt = open(path, encoding='utf-8', errors='replace').read()
    except Exception:
        return None
    m = re.search(pat, txt)
    return m.group(1) if m else None


def main():
    out = {'generated': '', 'portal': None, 'apps': {}, 'precache': []}
    import time
    out['generated'] = time.strftime('%Y-%m-%d %H:%M:%S')

    # 门户自身
    idx = os.path.join(ROOT, 'index.html')
    pv = detect_ver(idx, r'PORTAL_VER\s+(\d+\.\d+)')
    out['portal'] = pv

    precache = list(PRECACHE_BASE)

    if not os.path.isdir(APPS):
        print('[warn] 没有 apps/ 目录')
    else:
        names = [n for n in os.listdir(APPS)
                 if os.path.isdir(os.path.join(APPS, n))]
        # 显式顺序优先，未列出的排后面按字母序。
        names.sort(key=lambda n: (0, APP_ORDER.index(n))
                   if n in APP_ORDER else (1, n))
        for name in names:
            if name in HIDDEN_APPS:
                # 功能已并入别的卡片，门户不再单列入口。
                # 目录保留（源码还在，出问题可回退），只是不出现在门户上。
                print('  [hidden] %s —— 已并入其它卡片，门户不显示' % name)
                continue
            d = os.path.join(APPS, name)
            aj = os.path.join(d, 'app.json')
            if not os.path.isfile(aj):
                print('  [skip] %s —— 缺 app.json' % name)
                continue

            cfg = json.load(open(aj, encoding='utf-8'))
            tid = cfg.get('id', name)

            # link 型：只是个外链，没有本地入口文件。
            # 不参与 sha256/size/预缓存 —— 它的内容在别人的服务器上，
            # 缓存既没意义，也会让"离线可用"的说法失真。
            if cfg.get('type') == 'link':
                url = (cfg.get('url') or '').strip()
                if not url:
                    print('  [ERROR] %s 是 link 型但没填 url' % tid)
                    sys.exit(1)
                out['apps'][tid] = {
                    'id': tid, 'dir': name,
                    'name': cfg.get('name', tid),
                    'icon': cfg.get('icon', '🧩'),
                    'desc': cfg.get('desc', ''),
                    'type': 'link',
                    'tags': cfg.get('tags', []),
                    'ver': cfg.get('ver') or '1.0',
                    'url': url,
                }
                print('  %-10s v%-5s %8s    %s' % (tid, cfg.get('ver'), 'link', url))
                continue

            entry = cfg.get('entry', 'index.html')
            ep = os.path.join(d, entry)
            if not os.path.exists(ep):
                print('  [warn] %s 入口文件不存在: %s' % (tid, entry))
                continue

            pat = cfg.get('verPat', r'v(\d+\.\d+)')
            fver = detect_ver(ep, pat)
            aver = cfg.get('ver')

            # 版本不一致会误导用户，发布前必须拦下
            if fver and aver and fver != aver:
                print('  [ERROR] %s 版本不一致：app.json=%s 文件内=%s'
                      % (tid, aver, fver))
                print('          请同步后重新发布')
                sys.exit(1)

            rel = 'apps/%s/%s' % (name, entry)
            item = {
                'id': tid,
                'dir': name,
                'name': cfg.get('name', tid),
                'icon': cfg.get('icon', '🧩'),
                'desc': cfg.get('desc', ''),
                'type': cfg.get('type', 'page'),
                'tags': cfg.get('tags', []),
                'ver': aver or fver,
                'file': rel,
                'sha256': sha256_of(ep),
                'size': os.path.getsize(ep),
                'entryUrl': './apps/%s/%s' % (name, entry),
            }
            if cfg.get('type') == 'service':
                item['service'] = cfg.get('service', {})

            # gate 必须透传。门户读的是 version.json，不是 app.json ——
            # 这里漏一个字段，等于 app.json 里写了 gate:false 也照样加锁。
            # 默认 true（锁）：新工具忘了声明时按"要锁"处理，更安全。
            item['gate'] = cfg.get('gate', True)
            if cfg.get('gate') is False:
                print('  [note] %s 不启用门禁（gate=false）' % tid)
            elif cfg.get('type') == 'service' and 'gate' not in cfg:
                # 新工具忘了声明就按"要锁"处理。显式提醒，避免默认的锁
                # 被误当成"门户又弹密码框了"。
                print('  [note] %s 未声明 gate，按默认加锁' % tid)

            out['apps'][tid] = item

            # 纯前端页面才进预缓存（service 型走本地端口，不能缓存）
            if cfg.get('type') == 'page':
                precache.append('./' + rel)

            print('  %-10s v%-5s %8d B  %s'
                  % (tid, item['ver'], item['size'], rel))

    out['precache'] = precache

    # 门户全局配置（云端链接 / 品牌名 / 总版本）从 portal.json 读。
    # 总版本 suite.ver 提到顶层：它是用户判断"要不要重新下载"的唯一依据，
    # 放在 config 子层里容易被忽略，也难在页面上突出显示。
    # 放在数据文件里而不是写死在 index.html，是为了改网址时不必碰代码。
    cfgp = os.path.join(ROOT, 'portal.json')
    if os.path.exists(cfgp):
        try:
            with open(cfgp, encoding='utf-8') as f:
                cfg = json.load(f)
            out['config'] = cfg
            sv = (cfg.get('suite') or {}).get('ver')
            if sv:
                out['suite'] = sv
        except Exception as e:
            print('[warn] portal.json 读取失败，跳过: %s' % e)

    txt = json.dumps(out, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, 'version.json'), 'w', encoding='utf-8') as f:
        f.write(txt + '\n')
    print('\n已写入 version.json（%d 个应用，precache %d 项）'
          % (len(out['apps']), len(precache)))


if __name__ == '__main__':
    main()
