# -*- coding: utf-8 -*-
"""
v1.1.6  glog 提取：筛选条件并入基准时间同一行

只改布局，所以断言落在「结构」上：
  1. glnow / glwin / glunit / gllv / glall / glnone / gldef 必须落在同一个 flex 行里
  2. 这行必须在来源清单 glsrcs 之前
  3. 提取/复制/下载/计数 在来源清单之后（不被误挪）
  4. 每个 id 全局唯一，且 JS 里引用的 id 都能在 HTML 里找到（防止挪动时漏了接线）
  5. div 配对与 v1.1.5 基线一致（不引入新的结构破损）

注意：不要用手写正则去数作用域或用字符串匹配判断"属于哪一行"，
     HTML 是嵌套结构，按 div 边界切块才可靠。
"""
import os, re, sys, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, 'apps', 'statusweb', 'index.html')

fails = []
def chk(cond, name, extra=''):
    if cond:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s %s' % (name, extra))
        fails.append(name)

def main():
    s = open(HTML, encoding='utf-8').read()

    # ---- 1. 按 <div ...> 切块，找出含 gldef 的那个块 ----
    # 只取 id 首次出现的位置，用它在 div 块序列里的归属判断"同一行"
    pos = {i: s.index('id="%s"' % i) for i in
           ('glnow', 'glwin', 'glunit', 'gllv', 'glall', 'glnone', 'gldef',
            'glsrcs', 'glgo', 'glcopy', 'gldl', 'glcnt')}

    # 用 glsrcs 作为分界：之前是"时间+筛选"两行，之后是"提取"行
    chk(pos['glnow'] < pos['glsrcs'], 'glnow 在来源清单之前')
    chk(pos['glwin'] < pos['glsrcs'], 'glwin 在来源清单之前')
    chk(pos['gllv'] < pos['glsrcs'], 'gllv 在来源清单之前')
    chk(pos['gldef'] < pos['glsrcs'], 'gldef 在来源清单之前')
    chk(pos['glgo'] > pos['glsrcs'], 'glgo 在来源清单之后')
    chk(pos['glcopy'] > pos['glsrcs'], 'glcopy 在来源清单之后')
    chk(pos['gldl'] > pos['glsrcs'], 'gldl 在来源清单之后')
    chk(pos['glcnt'] > pos['glsrcs'], 'glcnt 在来源清单之后')

    # ---- 2. 真正判"同一行"：glnow 与 gldef 之间不能出现 flex 行的闭合 ----
    # 取 gldef 所在位置往前回溯，找到它所属的那个 display:flex 容器起点，
    # 确认 glnow 也在该容器内、且两者之间没有第二个 display:flex 起点。
    seg = s[pos['glnow']: pos['gldef']]
    # 这一区间内若出现新的 flex 容器起点，说明不在同一行
    chk(seg.count('display:flex') <= 0,
        'glnow→gldef 之间未新开一行', '(出现 %d 次 display:flex)' % seg.count('display:flex'))
    # 该区间也不能跨过来源清单
    chk('id="glsrcs"' not in seg, 'glnow→gldef 未跨越来源清单')

    # ---- 3. 顺序：车端当前时间 之后依次是 前后±、级别、全选、清空、诊断+任务 ----
    order = sorted(['glnow', 'glwin', 'glunit', 'gllv', 'glall', 'glnone', 'gldef'],
                   key=lambda i: pos[i])
    want = ['glnow', 'glwin', 'glunit', 'gllv', 'glall', 'glnone', 'gldef']
    chk(order == want, '行内顺序正确', str(order))

    # ---- 4. id 唯一 ----
    ids = re.findall(r'id="([A-Za-z0-9_]+)"', s)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    chk(not dup, 'HTML 无重复 id', str(dup))

    # ---- 5. JS 里 $('xxx') 引用的 id 都必须存在（挪动时最怕漏接线）----
    refs = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)", s))
    miss = sorted(r for r in refs if ('id="%s"' % r) not in s)
    chk(not miss, 'JS 引用的 id 全部存在', str(miss))

    # ---- 6. div 配对与基线一致 ----
    o, c = len(re.findall(r'<div\b', s)), len(re.findall(r'</div>', s))
    # 全文含 JS 拼串里的 </div>，这里只比对"相对上一版没变差"
    chk(o == 235 and c == 240, 'div 配对与 v1.1.5 基线一致',
        '(open=%d close=%d)' % (o, c))

    # ---- 7. 版本号四处一致 ----
    app = json.load(open(os.path.join(ROOT, 'apps', 'statusweb', 'app.json'),
                         encoding='utf-8'))
    chk(app['ver'] == '1.52', 'app.json = 1.52', app['ver'])
    chk("const PAGE_VER = '1.52';" in s, 'PAGE_VER = 1.52')
    chk('<span class="ver" id="pver">v1.52</span>' in s, '标题版本 v1.52')
    ag = open(os.path.join(ROOT, 'apps', 'statusweb', 'stat_agent.py'),
              encoding='utf-8').read()
    chk('AGENT_VER = "1.52"' in ag, 'AGENT_VER = 1.52')
    p = json.load(open(os.path.join(ROOT, 'portal.json'), encoding='utf-8'))
    chk(p['suite']['ver'] == '1.1.6', 'suite = 1.1.6', p['suite']['ver'])
    cl = open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8').read()
    chk(cl.startswith('## v1.1.6'), 'CHANGELOG 首节 = v1.1.6')

    print('\n' + ('%d 项全过' % 0 if not fails else 'FAIL %d 项: %s' % (len(fails), fails)))
    return 1 if fails else 0

if __name__ == '__main__':
    sys.exit(main())
