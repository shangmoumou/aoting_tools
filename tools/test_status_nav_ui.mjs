import fs from 'fs';
const html = fs.readFileSync('/data/workspace/aoting_tools/apps/statusweb/index.html','utf8');

/* 取出 renderNav 及其依赖（navDir 在本文件里紧挨着它前面） */
const a = html.indexOf("function navDir(");
const b = html.indexOf("/* 秒 → ");
let src = html.slice(a, b);
const dep = `
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function num(v){ const n=parseFloat(v); return isFinite(n)?n:null; }
function hasVal(v){ return !(v===''||v===null||v===undefined); }
function hwCell(k,v,unit){ return '<div class="hwcell">'+esc(k)+'|'+esc(v)+(unit?esc(unit):'')+'</div>'; }
const PAGE_VER = ${JSON.stringify((html.match(/const PAGE_VER = '([^']+)'/)||[])[1] || '0')};
const BOX = globalThis.BOX = {};
/* 假 DOM：除了 innerHTML/textContent，还得有 style —— v1.1.2 起 glog 相关函数
   会写 el.style.color，桩里没有就直接 TypeError，掩盖本文件的真实结果。 */
function $(id){ if(!BOX[id]) BOX[id]={innerHTML:'', textContent:'', style:{}, value:'', checked:false, dataset:{}, classList:{add(){},remove(){},toggle(){}}}; return BOX[id]; }
`;
const mod = new Function(dep + src + '\n; return {renderNav, navDir, renderAgentVer};')();
const {renderNav, navDir, renderAgentVer} = mod;

let pass=0, fail=0;
const ok=(c,m)=>{ if(c){pass++; console.log('ok   '+m);} else {fail++; console.log('FAIL '+m);} };

/* ---- 你给的真实 /cmd_vel 数据 ---- */
renderNav({ok:true, err:'', lin_x:0.499769168169, ang_z:-2.51382537867e-06,
           at:1, age:0.2, live:true, type:'geometry_msgs/Twist',
           topic:'/cmd_vel'});
let h = BOX['nav'].innerHTML;

ok(h.includes('导航状态'), '卡片标题出现');
ok(h.includes('/cmd_vel'), '标题带话题名');
ok(h.includes('0.500'), '线速度三位小数: 0.500 m/s');
ok(h.includes('m/s'), '线速度带单位 m/s');
ok(h.includes('rad/s'), '角速度带单位 rad/s');
ok(h.includes('正在发布'), 'live 时标「正在发布」');
ok(!h.includes('已停止发布'), 'live 时不说已停止');
ok(h.includes('度/s') || h.includes('°/s'), '角速度额外给度每秒');
ok(!h.includes('NaN'), '极小角速度不产生 NaN');
ok(h.includes('前进'), '0.499 m/s 判为前进');

/* 近似静止：两个值都在阈值内 */
renderNav({ok:true, err:'', lin_x:0.0, ang_z:-2.51382537867e-06,
           at:1, age:0.2, live:true, topic:'/cmd_vel'});
h = BOX['nav'].innerHTML;
ok(h.includes('静止'), '接近零 -> 静止');

/* ---- 停发：保留上次值，但必须说清是多久之前的 ---- */
renderNav({ok:true, err:'', lin_x:0.499769168169, ang_z:0.3,
           at:1, age:4.2, live:false, topic:'/cmd_vel'});
h = BOX['nav'].innerHTML;
ok(h.includes('已停止发布'), '停发 -> 明说已停止发布');
ok(h.includes('4.2 秒'), '停发 -> 说清是 4.2 秒前的值');
ok(h.includes('0.500'), '停发 -> 仍保留最后一次的值');
ok(h.includes('当前无控制指令'), '停发 -> 说明当前没有控制指令');
ok(!h.includes('正在发布'), '停发 -> 不再标正在发布');

/* ---- 话题压根没发布：不能显示 0，要说清原因 ---- */
renderNav({ok:false, err:'当前无控制指令（话题未发布）', lin_x:null,
           ang_z:null, at:0, age:null, live:false, topic:'/cmd_vel'});
h = BOX['nav'].innerHTML;
ok(h.includes('当前无控制指令'), '未发布 -> 明说当前无控制指令');
ok(!h.includes('0.000 m/s'), '未发布 -> 不显示 0（会被当成真的静止）');

/* ---- rospy 不可用 ---- */
renderNav({ok:false, err:'rospy 不可用（No module named rospy）',
           lin_x:null, ang_z:null, at:0, age:null, live:false,
           topic:'/cmd_vel'});
h = BOX['nav'].innerHTML;
ok(h.includes('rospy 不可用'), 'rospy 不可用 -> 给出原因');

/* ---- 已订阅但还没消息 ---- */
renderNav({ok:true, err:'', lin_x:null, ang_z:null, at:0, age:null,
           live:false, topic:'/cmd_vel'});
h = BOX['nav'].innerHTML;
ok(h.includes('暂无消息'), '已订阅无消息 -> 暂无消息');
ok(!h.includes('已停止发布'), '从没收到过消息时不说「已停止发布」');

/* ---- 方向判断 ---- */
ok(navDir(0.5, 0.0) === '前进', 'navDir 前进');
ok(navDir(-0.5, 0.0) === '后退', 'navDir 后退');
ok(navDir(0.0, 0.3) === '左转', 'navDir 左转（z>0 逆时针）');
ok(navDir(0.0, -0.3) === '右转', 'navDir 右转');
ok(navDir(0.5, 0.3) === '前进 · 左转', 'navDir 组合: ' + navDir(0.5, 0.3));
ok(navDir(0.0, 0.0) === '静止', 'navDir 静止');

/* ---- 服务端没给 nav 字段（= 车上跑的是旧脚本）----
   这一条专防"整栏消失"：字段缺失时如果什么都不画，用户只会以为
   没做这个功能，根本想不到是本地服务没重启。 */
renderNav(null);
h = BOX['nav'].innerHTML;
ok(h.includes('未获取到导航状态数据'), 'nav 缺失 -> 必须画出来并说明原因');
ok(h.includes('导航状态'), 'nav 缺失 -> 仍显示栏目标题');
ok(h.includes('8766'), 'nav 缺失 -> 提示去重启 8766 本地服务');
ok(!h.includes('0.000'), 'nav 缺失 -> 不伪造 0 值');

/* ---- 车端采集脚本版本比对 ---- */
/* 页面版本从 HTML 读，不要写死：写死后每次升版本这条就会误报，
   看着像"功能坏了"，实际只是号变了（踩过一次）。 */
const PV = (html.match(/const PAGE_VER = '([^']+)'/) || [])[1];
renderAgentVer(PV);
ok(BOX['agentver'] && BOX['agentver'].innerHTML.includes('与页面一致'),
   'agent_ver 与页面一致(=' + PV + ') -> 正常提示');
renderAgentVer('0.1');
ok(BOX['agentver'].innerHTML.includes('≠'), 'agent_ver 旧 -> 提示版本不一致');
ok(BOX['agentver'].innerHTML.includes('重新点卡片'),
   'agent_ver 旧 -> 告诉用户怎么办');
renderAgentVer(null);
ok(BOX['agentver'].innerHTML.includes('旧版'), 'agent_ver 缺失 -> 判定为旧脚本');
ok(html.indexOf('renderAgentVer(d.agent_ver)') > 0, 'render() 里会调用版本比对');
ok(html.indexOf('id="agentver"') > 0, '页面里有 agentver 容器');

/* ---- 主流程确实会渲染它 ---- */
const idx = html.search(/renderNav\(\s*d\.nav\s*[,)]/);
ok(idx > 0, 'render() 里调用了 renderNav');
ok(html.indexOf("id=\"nav\"") > 0, '页面里有 nav 容器');
ok(html.indexOf("$('nav').innerHTML = ''") > 0, '未连接时清空导航栏');
const cs = html.slice(html.indexOf('function copySnap('),
                      html.indexOf("$('bcopy')"));
ok(cs.includes('d.nav') && cs.includes('导航状态'), '复制快照带导航状态');

console.log('\nok ' + pass + ' / FAIL ' + fail);
process.exit(fail ? 1 : 0);
