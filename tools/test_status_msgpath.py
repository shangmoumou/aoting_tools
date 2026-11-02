#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 stat_agent 的消息类型加载（cln_msgs 在工作空间 devel 下的场景）。"""
import os, sys, shutil, tempfile, importlib

SRC = '/data/workspace/aoting_tools/apps/statusweb/stat_agent.py'
sys.path.insert(0, os.path.dirname(SRC))
import stat_agent

OK = FAIL = 0
def chk(name, cond, extra=''):
    global OK, FAIL
    if cond:
        OK += 1; print('  ok   %s %s' % (name, extra))
    else:
        FAIL += 1; print('  FAIL %s %s' % (name, extra))

def build_fake(root, ws, with_msg=True, pyver='python3.8', space='devel'):
    """造一个 catkin 工作空间布局。

    with_msg=True  → 编译产物：devel/lib/pythonX/dist-packages/cln_msgs/msg/
    with_msg=False → 源码目录：src/cln_msgs/（只有 .msg 文本，不能 import）
    """
    if with_msg:
        d = os.path.join(root, ws, space, 'lib', pyver, 'dist-packages', 'cln_msgs', 'msg')
        os.makedirs(d)
        open(os.path.join(os.path.dirname(d), '__init__.py'), 'w').close()
        open(os.path.join(d, '__init__.py'), 'w').write(
            'class DiagnoseFaults(object):\n    __slots__ = ["alarms"]\n')
        return d
    d = os.path.join(root, ws, 'src', 'cln_msgs')
    os.makedirs(d)
    open(os.path.join(d, 'DiagnoseFaults.msg'), 'w').write('int32 x\n')
    open(os.path.join(d, 'package.xml'), 'w').write('<package/>')
    return d

print('[1] 产物在 aotingos/devel 下 —— 应能加载')
root = tempfile.mkdtemp(prefix='ws_', dir='/data/workspace')
build_fake(root, 'aotingos', with_msg=True)
# 干扰项：同名源码目录（没有 msg/ 包），不能让它抢先
build_fake(root, 'aotingos', with_msg=False)
stat_agent.MSG_WS = ('hrg_bee_dev_v3', 'aotingos')
stat_agent.MSG_HOMES = (root,)
try:
    cls = stat_agent._load_msg_class('cln_msgs/DiagnoseFaults')
    chk('加载成功', cls.__name__ == 'DiagnoseFaults', cls.__name__)
    chk('命中的是 devel 产物路径',
        any('devel' in p for p in stat_agent.MSG_TRIED),
        str(stat_agent.MSG_TRIED[:2]))
except Exception as e:
    chk('加载成功', False, repr(e))

print('[2] 源码目录不能被当成可用路径')
try:
    paths = stat_agent._msg_site_paths()
    src_mis = [p for p in paths if p.rstrip('/').endswith('src')]
    chk('src 未混入 site 候选', not src_mis, str(src_mis))
except Exception as e:
    chk('_msg_site_paths 不炸', False, repr(e))

print('[3] 工作空间名换成 hrg_bee_dev_v3 也能命中')
root2 = tempfile.mkdtemp(prefix='ws2_', dir='/data/workspace')
build_fake(root2, 'hrg_bee_dev_v3', with_msg=True)
stat_agent.MSG_HOMES = (root2,)
for k in ('cln_msgs', 'cln_msgs.msg'):
    sys.modules.pop(k, None)
try:
    cls = stat_agent._load_msg_class('cln_msgs/DiagnoseFaults')
    chk('hrg_bee_dev_v3 加载成功', cls.__name__ == 'DiagnoseFaults')
except Exception as e:
    chk('hrg_bee_dev_v3 加载成功', False, repr(e))

print('[4] 完全没有产物时：应抛错且记录试过的路径')
# 前面两步把路径塞进了 sys.path，这里必须清干净，否则旧成果会被复用，
# 测出来"明明没有产物却加载成功" —— 是测试自身的污染，不是产品行为。
for p in list(stat_agent.MSG_TRIED):
    while p in sys.path:
        sys.path.remove(p)
stat_agent.MSG_TRIED[:] = []
# 造一个"有 devel 空间但里面没有 cln_msgs"的工作空间：
# 候选路径存在 → 会被记进 MSG_TRIED → 页面能回传"试过哪些"。
root3 = tempfile.mkdtemp(prefix='ws3_', dir='/data/workspace')
os.makedirs(os.path.join(root3, 'aotingos', 'devel', 'lib', 'python3.8',
                         'dist-packages'))
stat_agent.MSG_HOMES = (root3,)
for k in ('cln_msgs', 'cln_msgs.msg'):
    sys.modules.pop(k, None)
err = None
try:
    stat_agent._load_msg_class('cln_msgs/DiagnoseFaults')
except Exception as e:
    err = e
chk('抛错', err is not None)
chk('MSG_TRIED 有内容（供页面回传）', len(stat_agent.MSG_TRIED) > 0,
    str(len(stat_agent.MSG_TRIED)))

print('[5] 单引号禁令（脚本会被 shell_quote 单引号包裹）')
import re
s = open(SRC, encoding='utf-8').read()
bad = []
for i, l in enumerate(s.splitlines(), 1):
    if l.strip().startswith('#'):
        continue
    if re.search(r"'[^'\n]*'", l):
        bad.append(i)
chk('无单引号字面量', not bad, str(bad[:5]))

print('[6] python2 语法禁区（无 f-string / walrus）')
# ":=" 在本文件里合法存在于两处：注释里的说明，和 ROS 重映射字符串
# "__name:="。判定时要剥掉注释和字符串再找，否则是误报。
# 先去掉文档字符串块（"""..."""），否则文档里那句"不使用 walrus :="
# 会被自己当成 walrus —— 典型的自指误报。
s_code = re.sub(r'"""[\s\S]*?"""', '""', s)
s_code = re.sub(r"'''[\s\S]*?'''", "''", s_code)
code_lines = []
for l in s_code.splitlines():
    l = l.split('#')[0]
    l = re.sub(r'"[^"\n]*"', '""', l)
    code_lines.append(l)
code = '\n'.join(code_lines)
chk('无 f-string', not re.search(r'\bf["\']', code))
chk('无 walrus', ':=' not in code,
    str([l for l in code_lines if ':=' in l][:3]))

print('[7] server 侧要 source 工作空间 setup.bash（否则 cln_msgs 不在 PYTHONPATH）')
srv_src = open('/data/workspace/aoting_tools/apps/statusweb/server.py',
               encoding='utf-8').read()
chk('候选含 hrg_bee_dev_v3', 'hrg_bee_dev_v3' in srv_src)
chk('候选含 aotingos', 'aotingos' in srv_src)
i_ros = srv_src.find('/opt/ros/noetic/setup.bash')
i_ws = srv_src.find('/home/aoting/hrg_bee_dev_v3/devel/setup.bash')
chk('ROS 本体排在工作空间之前（叠加顺序不能反）',
    i_ros >= 0 and i_ws >= 0 and i_ros < i_ws)

for r in (root, root2, root3):
    shutil.rmtree(r, ignore_errors=True)
print('\n%d ok / %d fail' % (OK, FAIL))
sys.exit(1 if FAIL else 0)
