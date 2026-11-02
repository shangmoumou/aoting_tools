#!/usr/bin/env python3
"""
build.py —— 把 bag.js + chart.js + app.js + index.html 打包成一个 单文件 HTML

为什么需要: 浏览器的 ES module（import）在 file:// 协议下会被安全策略拦掉，
            导致双击 index.html 打开时 JS 一行都跑不了。
            打包成单文件后用普通 <script> 加载，双击就能用。

用法: python3 build.py    ->  生成 bag播放器.html
"""
import re
from pathlib import Path

here = Path(__file__).parent

bag = (here / 'bag.js').read_text(encoding='utf-8')
chart = (here / 'chart.js').read_text(encoding='utf-8')
app = (here / 'app.js').read_text(encoding='utf-8')
html = (here / 'index.html').read_text(encoding='utf-8')

# 1) 去掉 ES module 语法
def strip_export(src):
    # 用正则而不是字符串替换：export 与声明之间可能夹着 JSDoc 注释
    # （例如 "export /* 注释 */ const X"），字符串替换会漏掉这种。
    return re.sub(r'^export\s+', '', src, flags=re.M)


def _unused_placeholder():
    pass

bag = strip_export(bag)
chart = strip_export(chart)
app = re.sub(r"^\s*import\s+.*?from\s+['\"].*?['\"];?\s*$", '', app, flags=re.M)

# 2) 防止脚本里的 </script> 提前闭合标签
for _ in ('bag', 'chart', 'app'):
    globals()[_] = globals()[_].replace('</script>', '<\\/script>')

# 3) 拼成一个 IIFE，避免污染全局
js = (
    "(function(){\n'use strict';\n\n"
    "/* ============ bag.js ============ */\n" + bag + "\n\n"
    "/* ============ chart.js ============ */\n" + chart + "\n\n"
    "/* ============ app.js ============ */\n" + app + "\n\n"
    "})();\n"
)

# 4) 替换掉 index.html 里的 <script type="module" src="./app.js">
#    用 lambda 做 repl，避免 js 里的反斜杠被当成正则转义
old_tag = re.compile(r'<script\s+type="module"\s+src="\./app\.js"\s*>\s*</script>')
html, n = old_tag.subn(lambda m: '<script>\n' + js + '\n</script>', html)
if n != 1:
    raise SystemExit('没找到 script 标签，index.html 结构可能变了')

# 输出到 ../index.html —— 门户的 entry 就是这个，
# 写成 src/imu-analysis.html 的话门户永远读不到新代码（改了不生效）。
out = here.parent / 'index.html'
out.write_text(html, encoding='utf-8')
print(f'已生成: {out}  ({out.stat().st_size/1024:.0f} KB)')
print('门户读的就是这个文件，双击也能直接用。')
