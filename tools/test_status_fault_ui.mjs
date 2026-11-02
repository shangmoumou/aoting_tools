/**
 * 状态监控页面 —— 故障区块渲染测试。
 *
 * 用假 ctx 记录真实生成的 HTML 再断言，而不是只看变量。
 * 因为"显示了对的内容但排版/文案错了"这类问题，只有看输出才抓得到：
 * 比如话题还没订阅上时如果显示"当前无故障"，使用方会以为机器真的没事。
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HTML = path.join(__dirname, '..', 'apps', 'statusweb', 'index.html');
const src = fs.readFileSync(HTML, 'utf8');

let PASS = 0, FAIL = 0;
const chk = (n, c, e = '') => {
  if (c) { PASS++; console.log('  ok  ', n, e); }
  else   { FAIL++; console.log('  FAIL', n, e); }
};

/** 按大括号配平截取一个函数定义。 */
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('找不到函数 ' + name);
  let d = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (d === 0) return src.slice(i, k + 1); }
  }
  throw new Error('括号不配对: ' + name);
}

const box = { innerHTML: '' };
const code = `
  const esc = s => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const $ = id => BOX;
  const LV_CLS = {3:'l3', 2:'l2', 1:'l1'};
  ${grab('lvCls')}
  ${grab('renderFaults')}
  return { renderFaults, lvCls };
`;
const api = new Function('BOX', code)(box);

console.log('=== 1. 正常的故障列表 ===');
box.innerHTML = '';
api.renderFaults({
  ok: true, err: '', topic: '/diagnose_faults', type: 'v3_msgs/DiagnoseFaults',
  level: 3,
  items: [
    { code: 1589, data: 'IMU数据获取超时', level: 3, node: '/diagnose_node', t: 1790040465, tstr: '09-22 01:27:45' },
    { code: 261, data: 'SLAM_A节点心跳异常', level: 2, node: '/diagnose_node', t: 1790047267, tstr: '09-22 03:21:07' },
  ],
});
let h = box.innerHTML;
chk('故障码显示出来了', h.includes('1589'));
chk('故障内容显示出来了', h.includes('IMU数据获取超时'));
chk('第二条也在', h.includes('SLAM_A节点心跳异常'));
chk('总级别显示', h.includes('总级别 3'));
chk('条数显示', h.includes('共 2 条'));
chk('节点名显示', h.includes('/diagnose_node'));
chk('时间显示', h.includes('09-22 01:27:45'));
chk('严重项用 l3 样式', h.includes('fitem l3'));
chk('警告项用 l2 样式', h.includes('fitem l2'));
chk('话题名在标题里', h.includes('/diagnose_faults'));

console.log('\n=== 2. 空列表（真的没故障）===');
box.innerHTML = '';
api.renderFaults({ ok: true, err: '', topic: '/diagnose_faults', level: 0, items: [] });
h = box.innerHTML;
chk('显示当前无故障', h.includes('当前无故障'));
chk('不显示错误', !h.includes('未获取到'));

console.log('\n=== 3. 还没订阅上（不能谎报无故障）===');
box.innerHTML = '';
api.renderFaults({ ok: false, err: '正在等待话题 /diagnose_faults 发布…', topic: '/diagnose_faults', items: [] });
h = box.innerHTML;
chk('不显示当前无故障', !h.includes('当前无故障'));
chk('显示等待状态', h.includes('正在等待话题'));

console.log('\n=== 4. 话题未发布 / rospy 不可用 ===');
box.innerHTML = '';
api.renderFaults({ ok: false, err: '话题 /diagnose_faults 未发布（等了 10 秒仍没有）', topic: '/diagnose_faults', items: [] });
chk('显示未发布原因', box.innerHTML.includes('未发布'));
box.innerHTML = '';
api.renderFaults({ ok: false, err: 'rospy 不可用（No module named rospy）', topic: '/diagnose_faults', items: [] });
chk('显示 rospy 缺失', box.innerHTML.includes('rospy 不可用'));

console.log('\n=== 5. 未连接 / 无数据 ===');
box.innerHTML = 'X';
api.renderFaults(null);
chk('无数据时清空', box.innerHTML === '');

console.log('\n=== 6. 注入防护 ===');
box.innerHTML = '';
api.renderFaults({ ok: true, err: '', topic: '/x', level: 1,
  items: [{ code: 1, data: '<img src=x onerror=alert(1)>', level: 1, node: 'n', t: 0, tstr: '' }] });
chk('内容被转义', !box.innerHTML.includes('<img src=x'), box.innerHTML.slice(0, 0));

console.log('\n' + '='.repeat(46));
console.log('通过 ' + PASS + ' / 失败 ' + FAIL);
process.exit(FAIL ? 1 : 0);
