import fs from 'fs';
const html = fs.readFileSync('/data/workspace/aoting_tools/apps/statusweb/index.html','utf8');

/* 取出 renderTasks 及其依赖：从 toneCls 定义到 fmtSec 结束 */
const a = html.indexOf("function toneCls(");
const b = html.indexOf("/* ── 采集频率 ──");
let src = html.slice(a, b);
/* 依赖的几个小工具 */
/* progBar 定义在切片范围之外（499 行，切片从 toneCls 697 行开始），
   v1.0.61 引入它之后这里一直没带上，renderTasks 一调用就 ReferenceError。
   单独抠出来补进依赖，而不是把切片起点挪到 499 —— 中间那两百行还引用了
   别的东西，全 eval 进来只会引入新的未定义。 */
const _pb = html.slice(html.indexOf('function progBar('));
const progBarSrc = _pb.slice(0, _pb.indexOf('\n}') + 2);
const dep = `
${progBarSrc}
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function num(v){ const n=parseFloat(v); return isFinite(n)?n:null; }
function hasVal(v){ return !(v===''||v===null||v===undefined); }
function hwCell(k,v,unit){ return '<div class="hwcell">'+esc(k)+'|'+esc(v)+(unit?esc(unit):'')+'</div>'; }
function barPct(v){ const n=Math.max(0,Math.min(100,v||0)); return '<div class="bar" data-pct="'+n.toFixed(1)+'"></div>'; }
const BOX = globalThis.BOX = {};
/* 假 DOM 需要 style —— v1.1.2 起 glog 相关函数会写 el.style.color，
   桩里没有就直接 TypeError，掩盖本文件的真实结果。 */
function $(id){ if(!BOX[id]) BOX[id]={innerHTML:'', textContent:'', style:{}, value:'', checked:false, dataset:{}, classList:{add(){},remove(){},toggle(){}}}; return BOX[id]; }
`;
const mod = new Function(dep + src + '\n; return {renderTasks, fmtSec, toneCls};')();
const {renderTasks, fmtSec, toneCls} = mod;

let pass=0, fail=0;
const ok=(c,m)=>{ if(c){pass++; console.log('ok   '+m);} else {fail++; console.log('FAIL '+m);} };

/* ---- 用你给的真实数据 ---- */
const data = {
  task: {ok:true, err:'', name:'p2p_move', status:11, statusText:'暂停',
         statusTone:'warn', at:1, type:'v3_msgs/OnlineTaskSingle',
         topic:'/online_task_single',
         echo:{task_name:'p2p_move', task_id:'1790159853825',
               start_time:'2026-09-23 18:37:38', end_time:'', progress:0.0,
               cleaned_area:0.0, working_time:0.0, real_travel_dist:0.0,
               avoidance_obstacle_number:0.0, extrication_number:0.0,
               error_code:{error_code:0, error_msg:''}}},
  queue: {ok:true, err:'', at:1, type:'v3_msgs/OnlineTasksDeque',
          topic:'/online_tasks_deque',
          groups:[{name:'p2p_move', id:'1790159853653', area:null, est:null},
                  {name:'ii', id:'1790159978136', area:154.9, est:1540}]}
};

renderTasks(data);
let h = BOX['tasks'].innerHTML;

ok(h.includes('任务状态'), '卡片标题出现');
ok(h.includes('p2p_move'), '当前任务名显示');
ok(h.includes('暂停'), '状态中文显示');
ok(h.includes('状态码 11'), '状态码原样带出');
ok(h.includes('1790159853825'), '任务 ID 显示');
ok(h.includes('2026-09-23 18:37:38'), '开始时间显示');

/* 队列：竖排 + 顺序 + 当前高亮 */
const rows = (h.match(/class="qrow[^"]*"/g) || []);
ok(rows.length === 2, '队列 2 行竖排，实际 ' + rows.length);
const names = [...h.matchAll(/class="qnm">([^<]*)</g)].map(m=>m[1]);
ok(JSON.stringify(names) === JSON.stringify(['p2p_move','ii']),
   '队列顺序 = 发布顺序: ' + names.join(','));
ok(h.includes('qon'), '当前任务行高亮(qon)');
const hl = h.match(/<div class="qrow qon">[\s\S]*?class="qnm">([^<]*)</);
ok(hl && hl[1] === 'p2p_move', '高亮的是当前任务 p2p_move');
ok(h.includes('当前'), '有「当前」标签');
ok(h.includes('154.9 m²'), '队列面积显示');
ok(h.includes('预计'), '队列预计时长显示');
ok(h.includes('25 分钟') || h.includes('分钟'), '预计时长人读格式: '
   + (h.match(/预计 [^<]*/)||[''])[0]);

/* 脏值不应出现 */
ok(!h.includes('4.57089546078e-41'), '脏进度未显示');
ok(!h.includes('-1593015442'), '脏状态码未显示');
ok(!h.includes('7.59839548372e-35'), '脏面积未显示');

/* 状态配色 */
renderTasks({task:{ok:true,err:'',name:'clean',status:2,statusText:'执行中',
                   statusTone:'ok',echo:{}}, queue:{ok:true,err:'',groups:[]}});
ok(BOX['tasks'].innerHTML.includes('执行中'), '执行中显示');

renderTasks({task:{ok:true,err:'',name:'clean',status:12,statusText:'阻塞',
                   statusTone:'bad',echo:{}}, queue:{ok:true,err:'',groups:[]}});
h = BOX['tasks'].innerHTML;
ok(h.includes('阻塞'), '阻塞显示');
ok(h.includes('机器自动判断'), '阻塞单独给出原因说明');
ok(h.includes('err'), '阻塞用红色档位');

/* 未订阅上：显示原因，不留空白 */
renderTasks({task:{ok:false,err:'话题 /online_task_single 未发布（等了 10 秒仍没有）',
                   echo:{}}, queue:{ok:false,err:'rospy 不可用',groups:[]}});
h = BOX['tasks'].innerHTML;
ok(h.includes('未发布'), '任务话题未发布时给出原因');
ok(h.includes('rospy 不可用'), '队列不可用时给出原因');
ok(!h.includes('队列为空'), '不可用时不说「队列为空」');

/* 已订阅但没消息 */
renderTasks({task:{ok:true,err:'',name:'',status:null,statusText:'-',
                   statusTone:'',echo:{}},
             queue:{ok:true,err:'',groups:[]}});
h = BOX['tasks'].innerHTML;
ok(h.includes('暂无消息'), '已订阅无消息 → 暂无消息');
ok(h.includes('队列为空'), '空队列 → 队列为空');

/* 未连接 */
renderTasks(null);
ok(BOX['tasks'].innerHTML === '', '未连接时任务栏清空');

/* fmtSec */
ok(fmtSec(45) === '45 秒', 'fmtSec 45秒');
ok(fmtSec(90) === '1 分钟', 'fmtSec 90秒→1分钟');
ok(fmtSec(3725) === '1 小时 2 分钟', 'fmtSec 3725→1小时2分钟, 实际 '+fmtSec(3725));
ok(toneCls('bad') === 'err', 'toneCls bad→err');

console.log('\nok ' + pass + ' / FAIL ' + fail);
process.exit(fail ? 1 : 0);
