#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新增一个工具的脚手架。

用法：
    python tools/add_tool.py <id> <中文名> [--type page|service]

它会：
    1. 建 apps/<id>/ 目录
    2. 生成 app.json 模板
    3. 建 src/ 源码目录（放你拆开的 js/html）
    4. 提示你跑 build.py 生成单文件 index.html

目录约定
────────────────────────────────────────────────────────
    apps/<id>/
        app.json      工具描述 + 版本，门户靠它渲染卡片
        index.html    构建产物（单文件，可独立双击打开）
        src/          源码，平时改这里
            index.html    页面骨架
            *.js          业务逻辑
            build.py      把 src 打包成上层 index.html

为什么要有 src/ 和构建产物两份：
    src/ 分文件才好维护；但 ES module 在 file:// 下会被浏览器拦掉，
    双击打不开。所以 build.py 把它们内联成一个 index.html。
    改代码改 src/，改完跑 build.py，再跑 make_version.py。
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APPS = os.path.join(ROOT, 'apps')

APP_JSON = """{
  "id": "%(id)s",
  "name": "%(name)s",
  "ver": "1.0",
  "icon": "%(icon)s",
  "desc": "TODO: 一句话说清这个工具干什么、数据会不会外传。",
  "type": "%(type)s",
  "entry": "index.html",
  "tags": ["纯前端", "离线可用"],
  "src": "src/",
  "build": "src/build.py",
  "verPat": "v(\\\\d+\\\\.\\\\d+)"
}
"""

SRC_INDEX = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>%(name)s</title>
</head>
<body>
  <h1>%(name)s <span id="ver">v1.0</span></h1>
  <div id="app">TODO</div>

  <!-- build.py 会把 app.js 内联到这里 -->
  <script src="app.js"></script>
</body>
</html>
"""

SRC_APP = """// %(name)s —— 业务逻辑
// 改这里，然后跑 python3 build.py 重新打包。

console.log('%(id)s loaded');
"""

SRC_BUILD = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-
\"\"\"
把 src/ 下的文件打包成上层 index.html（单文件）。

为什么：ES module 在 file:// 下会被浏览器拦掉，双击打不开。
       内联后用普通 <script> 加载，双击就能用。

用法：python3 build.py
\"\"\"
import re
from pathlib import Path

here = Path(__file__).parent
out = here.parent / 'index.html'

html = (here / 'index.html').read_text(encoding='utf-8')
app = (here / 'app.js').read_text(encoding='utf-8')

# 去掉 ES module 语法。
# 用正则而不是字符串替换：export 和声明之间可能夹着 JSDoc 注释
# （例如 "export /* 注释 */ const X"），字符串替换会漏掉。
app = re.sub(r'^export\\s+', '', app, flags=re.M)

# 把 <script src="app.js"></script> 替换成内联
html = re.sub(
    r'<script\\s+src="app\\.js"\\s*></script>',
    lambda m: '<script>\\n' + app + '\\n</script>',
    html,
)

out.write_text(html, encoding='utf-8')
print('已生成', out)
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tid')
    ap.add_argument('name')
    ap.add_argument('--type', default='page',
                    choices=['page', 'service'])
    ap.add_argument('--icon', default='🧩')
    a = ap.parse_args()

    d = os.path.join(APPS, a.tid)
    if os.path.exists(d):
        print('[ERROR] 已存在: apps/%s' % a.tid)
        sys.exit(1)

    os.makedirs(os.path.join(d, 'src'), exist_ok=True)

    ctx = {'id': a.tid, 'name': a.name,
           'icon': a.icon, 'type': a.type}

    with open(os.path.join(d, 'app.json'), 'w', encoding='utf-8') as f:
        f.write(APP_JSON % ctx)
    with open(os.path.join(d, 'src', 'index.html'), 'w', encoding='utf-8') as f:
        f.write(SRC_INDEX % ctx)
    with open(os.path.join(d, 'src', 'app.js'), 'w', encoding='utf-8') as f:
        f.write(SRC_APP % ctx)
    with open(os.path.join(d, 'src', 'build.py'), 'w', encoding='utf-8') as f:
        f.write(SRC_BUILD)

    print('已创建 apps/%s/' % a.tid)
    print('  app.json        改 name / desc / ver')
    print('  src/index.html  页面骨架')
    print('  src/app.js      业务逻辑')
    print('  src/build.py    打包脚本')
    print()
    print('下一步：')
    print('  1. 写 src/ 里的代码')
    print('  2. cd apps/%s/src && python3 build.py' % a.tid)
    print('  3. cd 回根目录 && python3 tools/make_version.py')
    print('  4. 刷新门户页面，卡片会自动出现')


if __name__ == '__main__':
    main()
