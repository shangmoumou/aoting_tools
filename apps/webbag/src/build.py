#!/usr/bin/env python3
"""
build.py —— 把 bag.js + app.js + index.html 打包成一个 单文件 HTML

为什么需要: 浏览器的 ES module（import）在 file:// 协议下会被安全策略拦掉，
            导致双击 index.html 打开时 JS 一行都跑不了。
            打包成单文件后用普通 <script> 加载，双击就能用。

用法: python3 build.py    ->  生成 bag播放器.html
"""
import re
from pathlib import Path

here = Path(__file__).parent

bag = (here / 'bag.js').read_text(encoding='utf-8')
app = (here / 'app.js').read_text(encoding='utf-8')
html = (here / 'index.html').read_text(encoding='utf-8')

# 1) 去掉 ES module 语法
bag = bag.replace('export function', 'function').replace('export class', 'class')
app = re.sub(r"^\s*import\s+.*?from\s+['\"].*?['\"];?\s*$", '', app, flags=re.M)

# 2) 防止脚本里的 </script> 提前闭合标签
bag = bag.replace('</script>', '<\\/script>')
app = app.replace('</script>', '<\\/script>')

# 3) 拼成一个 IIFE，避免污染全局
js = (
    "(function(){\n'use strict';\n\n"
    "/* ============ bag.js ============ */\n" + bag + "\n\n"
    "/* ============ app.js ============ */\n" + app + "\n\n"
    "})();\n"
)

# 4) 替换掉 index.html 里的 <script type="module" src="./app.js">
#    用 lambda 做 repl，避免 js 里的反斜杠被当成正则转义
old_tag = re.compile(r'<script\s+type="module"\s+src="\./app\.js"\s*>\s*</script>')
html, n = old_tag.subn(lambda m: '<script>\n' + js + '\n</script>', html)
if n != 1:
    raise SystemExit('没找到 script 标签，index.html 结构可能变了')

out = here.parent / 'index.html'
out.write_text(html, encoding='utf-8')
print(f'已生成: {out}  ({out.stat().st_size/1024:.0f} KB)')
print('双击它就能用，不需要开服务器。')
print('注意: 门户读的就是 apps/webbag/index.html，改完 src 必须跑一次本脚本。')
