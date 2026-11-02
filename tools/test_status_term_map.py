# -*- coding: utf-8 -*-
"""v1.0.35 测试：终端（交互式 shell）+ 地图

重点盯这几件事：
  1) 地图名会拼进远端路径 —— 路径穿越必须挡住（本轮新代码里真出过这个洞）
  2) 终端必须是【交互式】shell：cd 到下一句要还生效，否则不能叫终端
  3) 终端输出缓冲要有上限，跑个 yes 不能把内存吃光
  4) 客户端落后太多时整段重发，不能接不上（会显示断片）
"""
import os, sys, inspect

sys.path.insert(0, '/data/workspace/aoting_tools/apps/statusweb')

PASS = FAIL = 0
def ck(cond, name, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1; print('ok   ' + name)
    else:
        FAIL += 1; print('FAIL ' + name + ('  | ' + str(extra) if extra else ''))

import server as S

print('\n[1] 地图名安全校验（路径穿越）')
BAD = [
    ('..',                      '单点'),
    ('../..',                   '多级上级目录'),
    ('../../../../etc/shadow',  '读系统口令文件'),
    ('..%2f..',                 '编码绕过'),
    ('.',                       '当前目录'),
    ('.hidden',                 '隐藏目录'),
    ('a/b',                     '带斜杠'),
    ('/etc/passwd',             '绝对路径'),
    ('HGTV/../../etc',          '名字合法但后面带穿越'),
    ('a b',                     '带空格（命令分隔）'),
    ('a;b',                     '分号注入'),
    ('$(id)',                   '命令替换'),
    ('`id`',                    '反引号注入'),
    ('a|id',                    '管道注入'),
    ('a\nid',                   '换行注入'),
    ('',                        '空'),
    ('x' * 65,                  '超长'),
]
GOOD = ['HGTV', 'map_1', 'map-2', 'Build_1789465201', '测试地图', 'A1']

for n, why in BAD:
    ck(not S._map_name_ok(n), '挡住 %s' % why, repr(n)[:40])
for n in GOOD:
    ck(S._map_name_ok(n), '放行正常地图名 %s' % n)

print('\n[2] 终端：必须是交互式 shell（cd 要能保持）')
src = inspect.getsource(S.TermSession)
ck('invoke_shell' in src, '用 invoke_shell 而非 exec_command')
ck('exec_command' not in src.split('class TermSession')[1].split('class ')[0],
   '终端内部不再用 exec_command')
# 注释里必须写明为什么，否则以后有人为省事换回 exec_command
ck('交互式' in src or 'cd' in src, '注释说明了 cd 要保持状态')

print('\n[3] 终端输出缓冲上限')
ck(hasattr(S, 'TERM_MAX_CHARS') and S.TERM_MAX_CHARS > 0,
   '存在输出上限常量', getattr(S, 'TERM_MAX_CHARS', None))
ck(S.TERM_MAX_CHARS <= 5 * 1024 * 1024, '上限不至于撑爆内存', S.TERM_MAX_CHARS)

print('\n[4] 增量拉取：落后太多要整段重发')
fsrc = inspect.getsource(S.TermSession.fetch)
ck('reset' in fsrc, 'fetch 会返回 reset 标记')
ck('since' in fsrc, 'fetch 接受 since 偏移')

print('\n[5] 接口路由存在')
all_src = inspect.getsource(S)
for p in ['/api/term', '/api/map', '/api/maps']:
    ck(p in all_src, '存在路由 %s' % p)

print('\n[6] 地图：取图走 sftp，未连接时要说人话')
ck(hasattr(S, '_sftp_read'), '存在 _sftp_read')
ck(hasattr(S, '_list_maps'), '存在 _list_maps（话题没发布时可手选）')
r = S._sftp_read('HGTV')
ck(isinstance(r, tuple) and r[0] is None and r[1],
   '未连接机器时给出原因', r[1] if len(r) > 1 else r)
ck('未连接' in (r[1] or ''), '原因写明"未连接机器"', r[1])

r2 = S._sftp_read('../etc')
ck(r2[0] is None and '不合法' in (r2[1] or ''), '非法地图名被挡在取图之前', r2[1])

print('\n[7] 地图根目录常量')
ck(hasattr(S, 'MAX_MAP_BYTES'), '存在取图大小上限')
ck(S.MAX_MAP_BYTES > 0, '大小上限为正', getattr(S, 'MAX_MAP_BYTES', None))

print('\n[8] 采集端：地图名真的被提出来')
# 这里不查源码字符串（字段名一改测试就碎，之前踩过），
# 而是真调 _map_cb + map_snapshot，看它到底吐出什么。
import stat_agent as A

ck(A.MAP_TOPIC == '/map_info_v3', '话题名正确', A.MAP_TOPIC)

snap0 = A.map_snapshot()
ck(isinstance(snap0, dict) and 'name' in snap0,
   'map_snapshot 含 name 字段', sorted(snap0.keys()) if isinstance(snap0, dict) else snap0)
ck(snap0.get('topic') == '/map_info_v3', '快照带话题名（页面要显示来源）', snap0.get('topic'))


class _BI(object):
    """仿 rospy 子消息。

    必须带 __slots__：_to_dict 靠 __slots__ 递归，而 genpy 生成的
    ROS 消息都带 __slots__。这里如果不带，就会被降级成 str(对象)，
    测出来的是"假失败"（第一版测试就是这么栽的）。
    """
    __slots__ = ['name', 'id', 'area']

    def __init__(self, name):
        self.name = name
        self.id = '1789465201_110698200'
        self.area = 545.08


class _Msg(object):
    """仿 rospy 消息：嵌套对象 + __slots__ 全量遍历。"""
    __slots__ = ['base_info', 'create_traj']

    def __init__(self, name):
        self.base_info = _BI(name)
        self.create_traj = None


A._map_cb(_Msg('HGTV'))
s1 = A.map_snapshot()
ck(s1.get('name') == 'HGTV', '从 base_info.name 取出地图名', s1.get('name'))
ck(s1.get('ok') is True, '收到后标记 ok', s1.get('ok'))
ck('create_traj' in s1.get('fields', {}) or 'base_info' in s1.get('fields', {}),
   '其余字段全量收（不写死字段名）', sorted(s1.get('fields', {}).keys()))

# 没有 name 时不能崩，且要留空让页面退回手动选
A._map_cb(_Msg(''))
s2 = A.map_snapshot()
ck(s2.get('name') == '', '地图名为空时留空串（页面退回手选）', repr(s2.get('name')))

print('\n' + '=' * 50)
print('通过 %d / 失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
