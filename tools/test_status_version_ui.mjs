/**
 * 状态监控页面 —— 版本信息区块测试。
 *
 * 用假 DOM 记录真实生成的 HTML 再断言。要盯的是这三件事：
 *   1) 三个来源各自独立：SLAM 话题没订阅时，软件/固件版本必须照常显示，
 *      不能因为一个取不到整个卡片就不渲染；
 *   2) 固件版本已经从"硬件状态"搬到"版本信息"，不能两边都显示、也不能
 *      哪边都没有；
 *   3) 取不到时要说清原因，而不是干巴巴一个 "-" —— 看到 "-" 的人分不清
 *      是"机器真的没这个"还是"没连上"。
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

const box = { innerHTML: '' };
let copied = '';
const code = `
  const esc = s => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const $ = id => BOX;
  const S = { lastData: null };
  const setMsg = () => {};
  const fmtUp = s => String(s);
  const navigator = { clipboard: { writeText: s => { COPIED.v = s; return Promise.resolve(); } } };
  ${grab('num')}
  ${grab('hwCell')}
  ${grab('hasVal')}
  ${grab('fmtSpd')}
  ${grab('boolOf')}
  ${grab('gb')}
  ${grab('humanBytes')}
  ${grab('verCell')}
  ${grab('srcNote')}
  ${grab('shortSrc')}
  ${grab('renderVers')}
  ${grab('renderHw')}
  ${grab('lvCls')}
  ${grab('renderFaults')}
  ${grab('copySnap')}
  return { renderVers, renderHw, copySnap, shortSrc, hasVal,
           setData: d => { S.lastData = d; } };
`;
const api = new Function('BOX', 'COPIED', code)(box, { get v() { return copied; }, set v(x) { copied = x; } });

const HW_OK = {
  ok: true, err: '', topic: '/hardware_state', type: 'v3_msgs/HardwareState',
  fields: {
    Version: '55_V4_03_08_P2.6', bat_state: 1, bat_voltage: 25.8,
    bat_current: 1.9, bat_percentage: 24, bat_real_percentage: 21,
    manual: true, e_stop: 0, clean_device_mode: '',
  },
};
const SW_OK = {
  ok: true, err: '', version: 'V3.2.11.0_gitdev',
  src: '/home/aoting/hrg_bee_dev_v3/version.yaml',
};
const SLAM_OK = {
  ok: true, err: '', topic: '/slam_a_status', type: 'slam_msgs/SlamStatus',
  fields: { sw_version: '2.7.5-linux64', docker_image: 'axbot:1.0.0' },
};

console.log('=== 1. 三个来源都有 ===');
box.innerHTML = '';
api.renderVers({ swver: SW_OK, hw: HW_OK, slam: SLAM_OK });
let h = box.innerHTML;
chk('标题是版本信息', h.includes('版本信息'));
chk('软件版本显示', h.includes('V3.2.11.0_gitdev'));
chk('固件版本显示', h.includes('55_V4_03_08_P2.6'));
chk('SLAM版本显示', h.includes('2.7.5-linux64'));
chk('三个格子都在', (h.match(/hwcell/g) || []).length === 3,
    String((h.match(/hwcell/g) || []).length));
chk('软件版本带来源（哪个工作空间）', h.includes('hrg_bee_dev_v3/version.yaml'));
chk('固件版本标注来源话题', h.includes('/hardware_state'));
chk('SLAM版本标注来源话题', h.includes('/slam_a_status'));

console.log('\n=== 2. SLAM 话题没订阅 —— 只影响自己那一格 ===');
box.innerHTML = '';
api.renderVers({
  swver: SW_OK, hw: HW_OK,
  slam: { ok: false, err: '话题 /slam_a_status 未发布（等了 10 秒仍没有）',
          fields: {}, topic: '/slam_a_status' },
});
h = box.innerHTML;
chk('软件版本照常', h.includes('V3.2.11.0_gitdev'));
chk('固件版本照常', h.includes('55_V4_03_08_P2.6'));
chk('SLAM 显示占位符', h.includes('>-<'));
chk('SLAM 说出原因', h.includes('未发布'));
chk('不谎报有 SLAM 版本', !h.includes('2.7.5-linux64'));

console.log('\n=== 3. SLAM 已订阅但还没消息 ===');
box.innerHTML = '';
api.renderVers({ swver: SW_OK, hw: HW_OK,
                 slam: { ok: true, err: '', fields: {}, topic: '/slam_a_status' } });
h = box.innerHTML;
chk('提示等消息而不是报未订阅', h.includes('暂无消息'), h.slice(0, 0));
chk('不误报成错误', !h.includes('未发布'));

console.log('\n=== 4. version.yaml 没有 / 缺字段 ===');
box.innerHTML = '';
api.renderVers({
  swver: { ok: false, err: '未找到 version.yaml（试过 hrg_bee_dev_v3 / aotingos）',
           version: '', src: '' },
  hw: HW_OK, slam: SLAM_OK,
});
h = box.innerHTML;
chk('软件版本显示占位', h.includes('>-<'));
chk('说出没找到的原因', h.includes('未找到 version.yaml'));
chk('固件版本不受影响', h.includes('55_V4_03_08_P2.6'));

console.log('\n=== 5. 硬件状态里不该再有固件版本 ===');
box.innerHTML = '';
api.renderHw(HW_OK);
let hh = box.innerHTML;
chk('硬件状态卡片保留', hh.includes('硬件状态'));
chk('充电状态还在', hh.includes('充电中'));
chk('固件版本已移走', !hh.includes('固件版本'), '（应只在版本信息那栏）');

console.log('\n=== 6. 复制快照里也要有版本信息 ===');
copied = '';
api.setData({ host: 'aoting', up: 100, cpu: 50, mem_used: 100, mem_total: 200,
              load: [1, 2, 3], top_cpu: [], top_mem: [],
              swver: SW_OK, hw: HW_OK, slam: SLAM_OK,
              faults: { ok: true, err: '', items: [], level: 0,
                        topic: '/diagnose_faults' } });
api.copySnap();
chk('含版本信息段', copied.includes('版本信息'), copied.slice(0, 80));
chk('含软件版本', copied.includes('软件版本: V3.2.11.0_gitdev'));
chk('含固件版本', copied.includes('固件版本: 55_V4_03_08_P2.6'));
chk('含SLAM版本', copied.includes('SLAM版本: 2.7.5-linux64'));
chk('硬件状态段保留', copied.includes('硬件状态'));
chk('硬件段里不再重复固件',
    copied.split('硬件状态')[1].indexOf('固件') < 0);

console.log('\n=== 7. 字段全缺时不崩 ===');
box.innerHTML = '';
api.renderVers({});
h = box.innerHTML;
chk('仍渲染出卡片', h.includes('版本信息'));
chk('三格都是占位', (h.match(/>-</g) || []).length === 3,
    String((h.match(/>-</g) || []).length));

console.log('\n' + '='.repeat(46));
console.log('  通过 ' + PASS + '，失败 ' + FAIL);
console.log('='.repeat(46));
process.exit(FAIL ? 1 : 0);
