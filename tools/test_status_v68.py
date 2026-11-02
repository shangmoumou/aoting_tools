# -*- coding: utf-8 -*-
"""v1.0.68：彩色画面旋转 + 地图 name 找不到时用 id 兜底。

这次的重点是【两处都要真跑一遍】，不能只查源码里有没有那个字符串：
   旋转：把 rgbRotSize / rgbDrawFrame 抠出来用 node 跑，断言
        90/270 会互换宽高、变换矩阵对得上、水印在旋转之后画。
   地图 id：构造 /map_info_v3 的 dict 喂给 _map_cb，断言 id 被解析；
        再喂假 sftp，断言 name 不存在时落到 id 目录。
只查字符串的话，"函数定义了但没接线"照样能通过。
"""
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'apps', 'statusweb')
HTML = os.path.join(APP, 'index.html')
AGENT = os.path.join(APP, 'stat_agent.py')
SERVER = os.path.join(APP, 'server.py')

ok = 0
bad = []


def chk(name, cond, extra=''):
    global ok
    if cond:
        ok += 1
    else:
        bad.append(name + ((' -- ' + str(extra)) if extra else ''))


html = io.open(HTML, encoding='utf-8').read()
agent = io.open(AGENT, encoding='utf-8').read()
server = io.open(SERVER, encoding='utf-8').read()

# ───────────────────────── 一、彩色画面旋转 ─────────────────────────
chk('有 rgbRotSize', 'function rgbRotSize(' in html)
chk('有 rgbDrawFrame', 'function rgbDrawFrame(' in html)
chk('每相机一个旋转下拉', 'class="rgbrot"' in html)
chk('旋转角按相机分别记', 'RGB.rot[c]' in html)
chk('RGB 里有 rot 容器', 'rot:{}' in html.replace(' ', ''))
chk('绘制时带旋转角', 'rgbDrawFrame(cell.cv, cell.ctx, img, cell.rot' in html)
chk('改角度立刻重绘', 'cell.img' in html and 'sel.onchange' in html)
chk('有 rgbRelease', 'function rgbRelease(' in html)


def _grab(src, start_marker):
    """从 start_marker 起抠出完整函数（按大括号配平）。"""
    i = src.find(start_marker)
    if i < 0:
        return ''
    j = src.find('{', i)
    if j < 0:
        return ''
    d = 0
    for k in range(j, len(src)):
        if src[k] == '{':
            d += 1
        elif src[k] == '}':
            d -= 1
            if d == 0:
                return src[i:k + 1]
    return ''


# 水印函数也要一起抠出来（rgbDrawFrame 会调它）
fns = ''
for m in ('function rgbRotSize(', 'function rgbDrawFrame('):
    fns += _grab(html, m) + '\n'
wm = _grab(html, 'function drawWatermark(')
chk('抠到 drawWatermark', len(wm) > 200)

JS = fns + wm + r'''
// 假 canvas：记录每一步变换，好断言"先转画面、后画水印"
function mkCtx(){
  const ops = [];
  const st = {tx:0, ty:0, rot:0};
  const stack = [];
  return {
    ops: ops,
    get transform(){ return {tx: st.tx, ty: st.ty, rot: st.rot}; },
    save(){ stack.push({tx:st.tx, ty:st.ty, rot:st.rot}); },
    restore(){ const s = stack.pop(); if (s){ st.tx=s.tx; st.ty=s.ty; st.rot=s.rot; } },
    translate(x, y){ st.tx += x; st.ty += y; ops.push(['translate', x, y]); },
    rotate(a){ st.rot += a; ops.push(['rotate', a]); },
    setTransform(){ st.tx=0; st.ty=0; st.rot=0; },
    drawImage(){ ops.push(['drawImage']); },
    fillRect(){ ops.push(['fillRect']); },
    fillText(t){ ops.push(['fillText', String(t)]); },
    measureText(t){ return {width: String(t).length * 6}; },
    set font(v){}, get font(){ return ''; },
    set fillStyle(v){}, set globalAlpha(v){},
    set textAlign(v){}, set textBaseline(v){},
    set tile(v){}
  };
}
const out = {};

// ① 尺寸：90/270 互换宽高，0/180 不变
out.s0  = rgbRotSize(640, 480, 0);
out.s90 = rgbRotSize(640, 480, 90);
out.s180= rgbRotSize(640, 480, 180);
out.s270= rgbRotSize(640, 480, 270);
out.s360= rgbRotSize(640, 480, 360);
out.sm90= rgbRotSize(640, 480, -90);

// ② 90 度：画布应为 480x640，且 drawImage 之前必须有 rotate(90°)
{
  const cv = {_w:0, _h:0};
  Object.defineProperty(cv, 'width',  {get(){return cv._w;}, set(v){cv._w=v; cv.cleared=true;}});
  Object.defineProperty(cv, 'height', {get(){return cv._h;}, set(v){cv._h=v;}});
  const ctx = mkCtx();
  const img = {width: 640, height: 480};
  rgbDrawFrame(cv, ctx, img, 90, {user:'u1', tile:false});
  out.c90 = {w: cv._w, h: cv._h};
  const di = ctx.ops.findIndex(o => o[0] === 'drawImage');
  const ri = ctx.ops.findIndex(o => o[0] === 'rotate');
  out.c90order = {di: di, ri: ri};
  // 水印（fillText）必须在 drawImage 之后 —— 旋转之内画水印会跟着躺倒
  const fi = ctx.ops.findIndex(o => o[0] === 'fillText');
  out.c90wmAfter = (fi > di && di >= 0);
  out.c90rot = ctx.ops.filter(o => o[0] === 'rotate').map(o => o[1]);
}

// ③ 0 度：不该有任何 rotate，画布保持 640x480
{
  const cv = {_w:0, _h:0};
  Object.defineProperty(cv, 'width',  {get(){return cv._w;}, set(v){cv._w=v;}});
  Object.defineProperty(cv, 'height', {get(){return cv._h;}, set(v){cv._h=v;}});
  const ctx = mkCtx();
  rgbDrawFrame(cv, ctx, {width:640, height:480}, 0, {user:'u', tile:false});
  out.c0 = {w: cv._w, h: cv._h};
  out.c0rot = ctx.ops.filter(o => o[0] === 'rotate').length;
}

// ④ 270 度
{
  const cv = {_w:0, _h:0};
  Object.defineProperty(cv, 'width',  {get(){return cv._w;}, set(v){cv._w=v;}});
  Object.defineProperty(cv, 'height', {get(){return cv._h;}, set(v){cv._h=v;}});
  const ctx = mkCtx();
  rgbDrawFrame(cv, ctx, {width:640, height:480}, 270, {user:'u', tile:false});
  out.c270 = {w: cv._w, h: cv._h};
  out.c270rot = ctx.ops.filter(o => o[0] === 'rotate').map(o => o[1]);
}
console.log(JSON.stringify(out));
'''

p = subprocess.run(['node', '-e', JS], capture_output=True, text=True)
chk('旋转 JS 跑得起来', p.returncode == 0, p.stderr[:300])
res = {}
if p.returncode == 0:
    import json as _json
    try:
        res = _json.loads(p.stdout.strip().split('\n')[-1])
    except Exception as e:
        bad.append('解析旋转 JS 输出失败: %r' % (e,))

if res:
    chk('0 度尺寸不变', res.get('s0') == {'w': 640, 'h': 480}, res.get('s0'))
    chk('90 度互换宽高', res.get('s90') == {'w': 480, 'h': 640}, res.get('s90'))
    chk('180 度尺寸不变', res.get('s180') == {'w': 640, 'h': 480}, res.get('s180'))
    chk('270 度互换宽高', res.get('s270') == {'w': 480, 'h': 640}, res.get('s270'))
    chk('360 归一到 0', res.get('s360') == {'w': 640, 'h': 480}, res.get('s360'))
    chk('-90 归一到 270', res.get('sm90') == {'w': 480, 'h': 640}, res.get('sm90'))
    chk('90 度画布真的互换了',
        res.get('c90') == {'w': 480, 'h': 640}, res.get('c90'))
    c90o = res.get('c90order') or {}
    chk('先 rotate 再 drawImage',
        c90o.get('ri', -1) >= 0 and c90o.get('di', -1) > c90o.get('ri', -1),
        c90o)
    chk('水印在旋转之后画', res.get('c90wmAfter') is True, res.get('c90wmAfter'))
    chk('0 度不旋转', res.get('c0rot') == 0, res.get('c0rot'))
    chk('0 度画布不变', res.get('c0') == {'w': 640, 'h': 480}, res.get('c0'))
    chk('270 度画布互换',
        res.get('c270') == {'w': 480, 'h': 640}, res.get('c270'))

# ───────────────────────── 二、地图 id 兜底 ─────────────────────────
# 注意不能用 [^}]* 跳到 "name"：定义里 "fields": {} 就有一个 }，
# 会被它截断，导致永远匹配不上（测试自己写的坑，改 DOTALL + 非贪婪）。
chk('MAP_STATE 有 id 字段',
    re.search(r'MAP_STATE\s*=\s*\{.*?"name":\s*""\s*,\s*"id":\s*""',
              agent, re.S) is not None)
chk('_map_cb 解析 base_info.id',
    'mid = str(bi.get("id") or "").strip()' in agent)
chk('id 随状态回传', '"id": MAP_STATE["id"]' in agent)
chk('server 支持 id 参数', "self.path.split('id=')" in server)
chk('server 按候选依次试', '_sftp_read([' in server)
chk('_sftp_read 返回命中名', 'return data, \'\', name' in server
    or "return data, '', name" in server)
chk('回传命中目录名', 'X-Map-Key' in server)
chk('前端读命中名', "r.headers.get('X-Map-Key')" in html)
chk('前端请求带 id', "'&id=' + encodeURIComponent(mid)" in html)
chk('前端提示按 ID 命中', '按 ID 命中' in html)
chk('自动加载传 id', 'loadMap(name, String(bid' in html)

# 真跑 _map_cb：喂一个 /map_info_v3 结构，看 name / id 有没有都解析出来
CB = r'''
import sys, types, re, io, json
src = io.open(sys.argv[1], encoding='utf-8').read()
m = re.search(r'^def _map_cb\(msg\):.*?(?=^def |\Z)', src, re.S | re.M)
ns = {'_to_dict': lambda x: x, 'time': types.SimpleNamespace(time=lambda: 0),
      'log': lambda *a: None, 'threading': None, 'MAP_LOCK': None,
      'MAP_STATE': {'ok': False, 'err': '', 'fields': {}, 'at': 0,
                    'type': '', 'name': '', 'id': ''}}
# MAP_LOCK 需要支持 with
import threading as _t
ns['threading'] = _t
ns['MAP_LOCK'] = _t.Lock()
exec(m.group(0), ns)
msg = {"base_info": {"name": "全", "id": "cace63b0d6b344c1950c8bef595d128c",
                     "floor": {"floor": 1}},
       "other": 1}
ns['_map_cb'](msg)
print(json.dumps({"name": ns['MAP_STATE']['name'],
                  "id": ns['MAP_STATE']['id'],
                  "ok": ns['MAP_STATE']['ok']}, ensure_ascii=False))
'''
p2 = subprocess.run([sys.executable, '-c', CB, AGENT],
                    capture_output=True, text=True)
chk('_map_cb 真跑通过', p2.returncode == 0, p2.stderr[-300:])
if p2.returncode == 0:
    import json as _j2
    try:
        d = _j2.loads(p2.stdout.strip().split('\n')[-1])
        chk('解析出 name', d.get('name') == '全', d)
        chk('解析出 id',
            d.get('id') == 'cace63b0d6b344c1950c8bef595d128c', d)
        chk('解析后 ok 为真', d.get('ok') is True, d)
    except Exception as e:
        bad.append('解析 _map_cb 输出失败: %r' % (e,))

# 真跑 _sftp_read 的候选逻辑：name 不存在时应落到 id
SFTP = r'''
import sys, io, re, json, types
src = io.open(sys.argv[1], encoding='utf-8').read()
ns = {}
ns['re'] = re
ns['MAP_DIR'] = '/home/aoting/user_config/map_database'
ns['MAP_FILE'] = 'map.png'
ns['MAX_MAP_BYTES'] = 25 * 1024 * 1024
# 抠函数时常量不会被带进来，这里要手动补上。
# 用 ast 取值而不是正则抠源码文本 —— 抠出来的会是 "r'...'" 这种带引号和
# r 前缀的字面量，直接 compile 就成了匹配引号本身的正则，中文名全部误判。
import ast as _ast
_MAP_NAME_OK = None
for _n in _ast.walk(_ast.parse(src)):
    if isinstance(_n, _ast.Assign):
        for _t in _n.targets:
            if getattr(_t, 'id', None) == 'MAP_NAME_OK':
                _MAP_NAME_OK = _ast.literal_eval(_n.value.args[0])
ns['MAP_NAME_OK'] = re.compile(_MAP_NAME_OK or r'^.+$')
ns['SESS_LOCK'] = __import__('threading').Lock()
ns['SESS'] = types.SimpleNamespace(ssh='FAKE')
ns['IOError'] = IOError

# 只允许 id 目录存在：模拟"名字改过、磁盘上还是 id"
EXIST = {'cace63b0d6b344c1950c8bef595d128c'}
OPENED = []

class FakeSftp(object):
    def stat(self, p):
        key = p.split('/')[-2]
        if key not in EXIST:
            raise IOError('no such file')
        return types.SimpleNamespace(st_size=1234)
    def open(self, p, mode):
        OPENED.append(p)
        return io.BytesIO(b'PNG-BYTES')
    def close(self):
        pass

class FakeSSH(object):
    def open_sftp(self):
        return FakeSftp()

ns['SESS'] = types.SimpleNamespace(ssh=FakeSSH())

# 抠出三个函数
out = {}
for fname in ('_map_name_ok', '_sftp_read', '_sftp_read_any'):
    m = re.search(r'^def %s\(.*?(?=^def |\Z)' % fname, src, re.S | re.M)
    if not m:
        print(json.dumps({'err': 'not found ' + fname})); sys.exit(1)
    exec(m.group(0), ns)

d, e, used = ns['_sftp_read'](['全', 'cace63b0d6b344c1950c8bef595d128c'])
# bytes 不能直接进 JSON，用 latin-1 解码保真（断言仍按 'PNG-BYTES' 比对）
out['fallback'] = {'data': (d or b'').decode('latin-1'),
                   'err': e, 'used': used}
out['opened'] = OPENED[:]

# 反例：两个都不存在，应报错且 used 为空
EXIST.clear()
d2, e2, used2 = ns['_sftp_read'](['nope', 'nope2'])
out['none'] = {'data': d2 is None, 'used': used2, 'err': e2}

# 反例：name 就存在，应直接用 name，不试 id
EXIST.add('全'); EXIST.add('cace63b0d6b344c1950c8bef595d128c')
d3, e3, used3 = ns['_sftp_read'](['全', 'cace63b0d6b344c1950c8bef595d128c'])
out['byName'] = {'used': used3}

# 反例：非法名字（含 ..）应被挡
d4, e4, u4 = ns['_sftp_read'](['../../etc'])
out['bad'] = {'data': d4 is None, 'err': e4}
print(json.dumps(out, ensure_ascii=False))
'''
p3 = subprocess.run([sys.executable, '-c', SFTP, SERVER],
                    capture_output=True, text=True)
chk('_sftp_read 真跑通过', p3.returncode == 0, p3.stderr[-300:])
if p3.returncode == 0:
    import json as _j3
    try:
        o = _j3.loads(p3.stdout.strip().split('\n')[-1])
        fb = o.get('fallback') or {}
        chk('name 找不到时落到 id', fb.get('used') ==
            'cace63b0d6b344c1950c8bef595d128c', fb)
        chk('落到 id 后读到了数据', fb.get('data') == 'PNG-BYTES', fb)
        chk('打开的是 id 目录下的 map.png',
            any('cace63b0d6b344c1950c8bef595d128c/map.png' in x
                for x in o.get('opened', [])), o.get('opened'))
        nn = o.get('none') or {}
        chk('两级都没有时报错', nn.get('data') is True and nn.get('used') == '',
            nn)
        chk('报错里带上两个目录', 'nope' in (nn.get('err') or '')
            and 'nope2' in (nn.get('err') or ''), nn.get('err'))
        chk('name 存在时优先用 name',
            (o.get('byName') or {}).get('used') == '全', o.get('byName'))
        bd = o.get('bad') or {}
        chk('含 .. 的名字被挡', bd.get('data') is True, bd)
    except Exception as e:
        bad.append('解析 _sftp_read 输出失败: %r' % (e,))

# ─────────────────── 三、静态结构 ───────────────────
seg = html
# 只数静态区（脚本之前）的 div 配对，脚本里拼的字符串不算
i = seg.find('<script')
static = seg[:i] if i > 0 else seg
chk('静态区 div 配对',
    static.count('<div') == static.count('</div>'),
    '%d vs %d' % (static.count('<div'), static.count('</div>')))

# 单引号禁令（脚本整份经 shell 下发，正文里出现单引号会让命令行变形）
body = agent.split('"""', 2)[-1]
chk('stat_agent 正文无单引号', "'" not in body)

print('PASS %d  FAIL %d' % (ok, len(bad)))
for b in bad:
    print('  FAIL ' + b)
sys.exit(1 if bad else 0)
