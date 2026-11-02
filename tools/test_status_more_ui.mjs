/**
 * v1.0.34 页面测试：定位状态 / 磁盘 / 温度频率 / 自定义话题 / 版本栏加宽
 *
 * 用假 DOM 记录真实生成的 HTML 再断言。重点：
 *   1) 定位：字段缺失不能整栏消失；不可靠时要有显眼提示
 *   2) 磁盘：目录统计"进行中"要说清楚，不能显示成 0
 *   3) 自定义话题：截断必须如实写出来，内容要能原样展示
 *   4) 版本栏：用加宽的 vergrid（原 hwgrid 会把长版本号压掉）
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

const BOX = {};
const code = `
  const esc = s => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const $ = id => { if(!BOX[id]) BOX[id] = {innerHTML:'', querySelectorAll: () => []};
                    return BOX[id]; };
  const setMsg = () => {};
  const duRefresh = () => {};
  ${grab('num')}
  ${grab('hwCell')}
  ${grab('hwCellK')}
  ${grab('yesno')}
  ${grab('boolOf')}
  ${grab('fmtSpd')}
  ${grab('gb')}
  ${grab('humanBytes')}
  ${grab('renderAxslam')}
  ${grab('renderDisk')}
  ${grab('renderCustom')}
  ${grab('thermalCell')}
  ${grab('freqCell')}
  return { renderAxslam, renderDisk, renderCustom, thermalCell, freqCell,
           humanBytes, gb };
`;
const api = new Function('BOX', code)(BOX);

// ═══════ 1. 定位状态：用你给的真实 /ax_slam_state ═══════
api.renderAxslam({
  ok: true, err: '', topic: '/ax_slam_state', type: 'v3_msgs/AxSlamState',
  fields: { state: 2, reliable: true, position_quality: 10,
            relocating: false, lidar_matching_score: 0.666000008583,
            wheel_slipping: false, lidar_reliable: true,
            position_loss_progress: 0.0 },
});
let h = BOX['axslam'].innerHTML;
chk(h.includes('/ax_slam_state'), '标题带话题名');
chk(h.includes('定位状态'), '卡片标题正确');
chk(h.includes('10'), '定位质量 10 显示出来');
chk(h.includes('0.666'), '雷达匹配值三位小数');
chk(h.includes('是') && h.includes('否'), '布尔字段翻成是/否');
chk(!h.includes('undefined'), '没有 undefined 漏出来');

// 不可靠 -> 必须有显眼提示（定位不可靠会走错路线）
api.renderAxslam({ ok: true, err: '', topic: '/ax_slam_state',
                   fields: { state: 3, reliable: false, position_quality: 2,
                             relocating: true, lidar_reliable: false } });
h = BOX['axslam'].innerHTML;
chk(h.includes('定位不可靠'), '不可靠时给出显眼提示');
chk(h.includes('正在重定位'), '重定位时提示结果不可信');

// 字段缺失 / 服务端没给 -> 不能整栏消失
api.renderAxslam(null);
h = BOX['axslam'].innerHTML;
chk(h.includes('定位状态') && h.includes('未获取到'), '字段缺失时仍渲染并说明原因', h.slice(0, 60));

api.renderAxslam({ ok: false, err: '话题 /ax_slam_state 未发布（等了 10 秒仍没有）',
                   fields: {} });
h = BOX['axslam'].innerHTML;
chk(h.includes('未发布'), '订阅失败时说原因而不是空白');

// ═══════ 2. 磁盘 ═══════
api.renderDisk({
  total: { mount: '/', total: 240000, used: 180000, free: 60000, pct: 75.0 },
  du: { busy: false, at: 1, sec: 12.3, items: [
    { dir: '/var', ok: true, bytes: 26491545, err: '' },
    { dir: '/usr', ok: true, bytes: 5300569638, err: '' },
    { dir: '/home/aoting', ok: false, bytes: 0, err: '目录不存在' },
  ] },
});
h = BOX['disk'].innerHTML;
chk(h.includes('磁盘'), '卡片标题');
chk(h.includes('234.4') || h.includes('175.8'), '容量换算成 GB', h.slice(0, 200));
chk(h.includes('75'), '占用百分比显示');
chk(h.includes('/var') && h.includes('/usr'), '每个目录都列出来');
chk(h.includes('4.94 GB') || h.includes('5.3 GB') || h.includes('4.94'),
    '目录大小换算可读', h.slice(0, 400));
chk(h.includes('目录不存在'), '统计失败的目录说明原因');
chk(h.includes('重新统计'), '提供重新统计入口');

// 首次统计中：不能显示成 0
api.renderDisk({ total: { mount: '/', total: 100, used: 50, free: 50, pct: 50 },
                 du: { busy: true, at: 0, sec: 0, items: [] } });
h = BOX['disk'].innerHTML;
chk(h.includes('统计中'), '统计中明确提示，不显示 0');

// 快满了要告警
api.renderDisk({ total: { mount: '/', total: 100, used: 95, free: 5, pct: 95 },
                 du: { busy: false, at: 1, sec: 1, items: [] } });
h = BOX['disk'].innerHTML;
chk(h.includes('写满'), '磁盘将满时告警');

api.renderDisk(null);
chk(BOX['disk'].innerHTML.includes('未获取到'), '字段缺失时仍渲染并说明');

// ═══════ 3. 自定义话题 ═══════
api.renderCustom({ '/odom_origin': { ok: true, err: '', type: 'nav_msgs/Odometry',
                   at: 1, fields: { twist: { twist: { linear: { x: 0.27 } } } },
                   note: '' } });
h = BOX['ctlist'].innerHTML;
chk(h.includes('/odom_origin'), '话题名列出来');
chk(h.includes('nav_msgs/Odometry'), '消息类型显示');
chk(h.includes('0.27'), '内容原样展示');
chk(h.includes('×'), '有取消订阅入口');

// 截断必须如实说明，不能悄悄少给
api.renderCustom({ '/big': { ok: true, err: '', type: 'x/Big', at: 1,
                   fields: { a: 1 }, note: '内容过大，仅回传前 12000 字符' } });
h = BOX['ctlist'].innerHTML;
chk(h.includes('内容过大'), '截断时如实提示');

api.renderCustom({ '/t': { ok: false, err: '话题未发布，或名字不对（注意大小写和开头的 /）',
                   type: '', at: 0, fields: null, note: '' } });
h = BOX['ctlist'].innerHTML;
chk(h.includes('名字不对'), '话题名写错时给出可操作的提示');

api.renderCustom({});
chk(BOX['ctlist'].innerHTML.includes('还没订阅'), '空状态有说明');

// ═══════ 4. 温度 / 频率 ═══════
let t = api.thermalCell({ ok: true, c: 62.5, src: 'x86_pkg_temp', n: 3 });
chk(t.includes('62.5') && t.includes('°C'), '温度带单位和数值');
t = api.thermalCell({ ok: true, c: 91.0, src: 'x86_pkg_temp' });
chk(t.includes('bad'), '高温标红', t.slice(0, 120));
t = api.thermalCell({ ok: false, err: '本机未暴露温度传感器' });
chk(t.includes('未暴露'), '取不到时说原因而不是 0');

let f = api.freqCell({ ok: true, mhz: 2400, max: 3000, n: 8 });
chk(f.includes('2400') && f.includes('8 核'), '频率与核数显示');
f = api.freqCell({ ok: true, mhz: 1200, max: 3000, n: 8 });
chk(f.includes('已降频'), '明显低于最大频率时提示降频');
f = api.freqCell({ ok: false, err: '本机未暴露 CPU 频率' });
chk(f.includes('未暴露'), '取不到时说原因');

// ═══════ 5. 版本栏必须用加宽的 vergrid ═══════
const iVers = src.indexOf("function renderVers(");
const segVers = src.slice(iVers, iVers + 2200);
chk(segVers.includes('vergrid'), '版本栏用 vergrid（比 hwgrid 宽一倍）');
chk(src.indexOf('.vergrid{') > 0, 'vergrid 样式已定义');
const vg = src.slice(src.indexOf('.vergrid{'), src.indexOf('.vergrid{') + 260);
chk(vg.includes('300px'), 'vergrid 最小宽度 300px（原 128px）', vg.slice(0, 80));
chk(vg.includes('word-break'), '长版本号允许换行而不是被裁掉');

// ═══════ 6. 赛博朋克配色确实改了 ═══════
chk(src.includes('--acc:#00e6ff') || src.includes('--acc: #00e6ff'), '主色改为霓虹青');
chk(src.includes('#05060f'), '底色压到近黑深蓝');
chk(src.includes('linear-gradient(var(--grid)'), '加了网格背景');

console.log('');
console.log('通过 ' + PASS + ' / 失败 ' + FAIL);
process.exit(FAIL ? 1 : 0);
