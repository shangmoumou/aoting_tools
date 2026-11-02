# -*- coding: utf-8 -*-
"""v1.0.30 采集端测试：任务状态 + 队列 + 频率"""
import os, sys, json, time, threading
sys.path.insert(0, '/data/workspace/aoting_tools/apps/statusweb')

PASS = FAIL = 0
def ck(cond, name, extra=''):
    global PASS, FAIL
    if cond: PASS += 1; print('PASS  ' + name)
    else: FAIL += 1; print('FAIL  ' + name + ('  | ' + str(extra) if extra else ''))

import stat_agent as A

# ── 状态码映射 ──
exp = {0:'未知',1:'空闲',2:'执行中',11:'暂停',12:'阻塞',21:'正常结束',
       23:'用户取消',24:'终止',25:'补充清扫',100:'异常'}
ok = True
for k, v in exp.items():
    t, tone = A.task_status_text(k)
    if t != v: ok = False; print('   ', k, '->', t, '期望', v)
ck(ok, '状态码映射全部正确')
ck(A.task_status_text(12)[1] == 'bad', '阻塞=bad 档')
ck(A.task_status_text(11)[1] == 'warn', '暂停=warn 档')
ck(A.task_status_text(2)[1] == 'ok', '执行中=ok 档')
ck(A.task_status_text(999)[0] == '999', '未定义状态码原样显示数字')
ck(A.task_status_text(None)[0] == '-', '空状态码显示 -')

# ── 脏值过滤 ──
ck(A._clean_num(4.57089546078e-41) is None, 'denormal 4.57e-41 被剔除')
ck(A._clean_num(7.59839548372e-35) is None, 'denormal 7.59e-35 被剔除')
ck(A._clean_num(4.59135441836e-41) is None, 'denormal 4.59e-41 被剔除')
ck(A._clean_num(154.880004883) is not None, '正常面积 154.88 保留')
ck(A._clean_num(1540.09240723) is not None, '正常预计时长保留')
ck(A._clean_num(0.0) == 0.0, '0 保留（不是脏值）')
ck(A._clean_num(float('nan')) is None, 'NaN 被剔除')

# ── 构造假消息（模拟 rospy 对象）──
class Obj(object):
    def __init__(self, **kw):
        self.__slots__ = list(kw.keys())
        for k, v in kw.items(): setattr(self, k, v)

def pose():
    return Obj(secs=0, nsecs=0)

# 用你给的真实 /online_tasks_deque 数据
g1 = Obj(stamp=pose(), group_name="p2p_move", estimated_time=0.0, task_area=0.0,
         task_speed=Obj(speed=0), utils_mode='', dynamic_adjust_path=False,
         group_id="1790159853653", timertask_name='', timertask_id='',
         save_time='', is_force_end=False, list_total_size=0, list_curr_idx=0,
         group_status=-1593015442,
         group_cleaned_progress=4.57089546078e-41,
         group_cleaned_area=7.59839548372e-35,
         group_cleaned_time=4.59135441836e-41,
         marker_point_name='')
g2 = Obj(stamp=pose(), group_name="ii", estimated_time=1540.09240723,
         task_area=154.880004883, task_speed=Obj(speed=1), utils_mode="mode_2",
         dynamic_adjust_path=False, group_id="1790159978136",
         timertask_name='', timertask_id='', save_time="2026-09-23 18:39:27",
         is_force_end=False, list_total_size=0, list_curr_idx=0,
         group_status=-1511268720,
         group_cleaned_progress=3.07402844119e-41,
         group_cleaned_area=7.60086585856e-35,
         group_cleaned_time=4.59135441836e-41,
         marker_point_name='')
A._queue_cb(Obj(taskgroup_list=[g1, g2]))
q = A.queue_snapshot()
ck(q['ok'] is True, '队列解析 ok')
ck([g['name'] for g in q['groups']] == ['p2p_move', 'ii'],
   '队列顺序 = 发布顺序', q['groups'])
ck(all('group_status' not in g for g in q['groups']),
   '脏值 group_status 未带出')
ck(all(g.get('area') != 4.57e-41 for g in q['groups']), '脏面积未带出')
ck(q['groups'][1]['area'] == 154.9, '正常面积带出', q['groups'][1])
ck(q['groups'][1]['est'] == 1540, '正常预计时长带出', q['groups'][1])
ck(q['groups'][0]['id'] == "1790159853653", 'group_id 带出')

# 空队列
A._queue_cb(Obj(taskgroup_list=[]))
ck(A.queue_snapshot()['groups'] == [], '空队列 = 空列表')

# ── /online_task_single 真实数据 ──
echo = Obj(time_stamp=pose(), task_name="p2p_move", task_id="1790159853825",
           add_time="2026-09-23 18:37:33", start_time="2026-09-23 18:37:38",
           end_time='', progress=0.0, working_time=0.0, coverage_rate=0.0,
           cleaned_area=0.0, real_average_speed=0.0, real_travel_dist=0.0,
           avoidance_obstacle_number=0.0, extrication_number=0.0,
           extrication_time=0.0, extrication_success_rate=0.0,
           missed_area=0.0, missed_number=0.0, missed_time=0.0,
           start_power=0.0, end_power=0.0, power_consumption=0.0,
           cleaned_path=Obj(path_type=Obj(type=0), poses=[], path_interval=0.0),
           error_code=Obj(error_code=0, error_msg=''),
           task_status=Obj(status=11),
           task_picture=Obj(png_file_path='', png_str='', qiniu_file_key='',
                            map_name='', sub_task_list=[]),
           move_status=0, plan_path_try=False, block_triggered=False,
           supplementary_clean=False)
A._task_cb(Obj(task_echo=echo))
t = A.task_snapshot()
ck(t['ok'] is True, '当前任务解析 ok')
ck(t['name'] == 'p2p_move', '任务名取出', t['name'])
ck(t['status'] == 11, '状态码取出', t['status'])
ck(t['statusText'] == '暂停', '状态中文映射', t['statusText'])
ck(t['statusTone'] == 'warn', '状态配色档位', t['statusTone'])
ck(t['echo'].get('task_id') == "1790159853825", 'task_id 全量带出')
ck(t['echo'].get('start_time') == "2026-09-23 18:37:38", '开始时间带出')

# 改状态为执行中
echo.task_status = Obj(status=2)
A._task_cb(Obj(task_echo=echo))
ck(A.task_snapshot()['statusText'] == '执行中', '状态改为执行中')
echo.task_status = Obj(status=12)
A._task_cb(Obj(task_echo=echo))
ck(A.task_snapshot()['statusText'] == '阻塞', '状态改为阻塞')
ck(A.task_snapshot()['statusTone'] == 'bad', '阻塞=bad')

# task_echo 缺失时不炸
A._task_cb(Obj(other=1))
ck(A.task_snapshot()['name'] == '', 'task_echo 缺失不炸')
A._queue_cb(Obj(other=1))
ck(A.queue_snapshot()['groups'] == [], 'taskgroup_list 缺失不炸')

# ── 频率 ──
ck(abs(A.cur_hz() - 1.0) < 1e-9, '默认 1Hz')
for v in (0.1, 0.5, 1, 2, 5, 10):
    ck(A.set_hz(v) is True and abs(A.cur_hz() - v) < 1e-9,
       '设定 %s Hz 生效' % v)
ck(A.set_hz(0) is False, '0Hz 被拒')
ck(A.set_hz(-1) is False, '负频率被拒')
ck(A.set_hz(100) is False, '100Hz 被拒（会吃满车端CPU）')
ck(A.set_hz('abc') is False, '非数字被拒')
ck(abs(A.cur_hz() - 10) < 1e-9, '非法值不改当前频率（仍是上次合法的10）')

print('\n通过 %d / 失败 %d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
