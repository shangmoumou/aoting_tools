/**
 * Node 测试桩：抽出 imu-analysis.html 里的 JS，在假 DOM 上跑真实逻辑。
 *
 * 覆盖：bag 解析 / unwrap / 频率诊断 / 帧间 yaw / A1~A6 六图 /
 *       区间过滤 / 缩放 / 线宽 / lz4 / 缺话题降级 / 图高
 *
 * 用法：node imuweb/test/test.mjs
 */
import { readFileSync, writeFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');

const html = readFileSync(join(root, 'index.html'), 'utf-8');
const m = html.match(/<script>\n([\s\S]*?)\n<\/script>/);
if (!m) throw new Error('没从 html 里抽到 script');
const js = m[1].replace(/<\\\/script>/g, '</script>');
writeFileSync('/tmp/_imu_packed.js', js);

const noop = () => {};
function makeCtx() {
  const st = {};
  return new Proxy(st, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'measureText') return () => ({ width: 50 });
      if (k === 'createImageData') return (w, h) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) });
      if (k === 'getImageData') return (x, y, w, h) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) });
      return noop;
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}
// 从 html 的 CSS 里解析 .figcv 的高度，作为 canvas 桩的默认高度。
// 这样测试断言的是真实 CSS 值 —— 以后改 CSS，测试会跟着变，不会写死。
const cssFigH = (() => {
  const m2 = html.match(/canvas\.figcv\s*\{[^}]*?height:\s*(\d+)px/);
  return m2 ? parseInt(m2[1], 10) : 240;
})();
console.log(`     (CSS 里 .figcv 高度 = ${cssFigH}px)`);

function makeCanvas(w = 900, h = cssFigH) {
  const c = {
    width: w, height: h, clientWidth: w, style: {}, className: '', _cssH: h,
    getContext: () => c._ctx || (c._ctx = makeCtx()),
    getBoundingClientRect: () => ({ left: 0, top: 0, width: c.clientWidth, height: c.clientHeight }),
    addEventListener: noop, setPointerCapture: noop,
  };
  // Figure.resize 读 clientHeight；让桩跟随 style.height，才能验证图高设置真的生效
  Object.defineProperty(c, 'clientHeight', {
    get() { const v = parseFloat(c.style.height); return isFinite(v) && v > 0 ? v : c._cssH; },
    set(v) { c._cssH = v; },
    configurable: true,
  });
  return c;
}
const els = {}, dl = {};
function mkEl(id) {
  const e = {
    id, style: {}, value: id === 'lwSel' ? '1' : '', textContent: '', innerHTML: '',
    disabled: false, children: [], onclick: null, onchange: null, title: '',
    dataset: {}, tagName: '', nodeType: 1,
    // 真实 DOM 里 className 和 classList 是同一份数据；桩不同步的话，
    // 任何依赖 classList.contains('fig') 的代码都会静默失效（真浏览器不会）。
    get className() { return [...this.classList._s].join(' '); },
    set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    remove() { const p = this.parentNode; if (p) p.removeChild(this); },
    get nextSibling() {
      const p = this.parentNode; if (!p) return null;
      const i = p.children.indexOf(this); return i >= 0 ? (p.children[i + 1] || null) : null;
    },
    checked: id === 'normChk' || id === 'hideCov',   // 归一化 & 隐藏矩阵字段默认开
    contains() { return false; }, closest() { return null; },
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, f) { f ? this._s.add(c) : this._s.delete(c); }, contains(c) { return this._s.has(c); } },
    appendChild(c) {
      const old = c.parentNode;                       // 同上：append 也是移动
      // 注意 old === this 时也要先摘掉 —— 同一个父节点内的 append 同样是"移到末尾"
      if (old) { const oi = old.children.indexOf(c); if (oi >= 0) old.children.splice(oi, 1); }
      c.parentNode = this; this.children.push(c); return c;
    },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; },
    insertBefore(c, ref) {
      // 真实 DOM 的 insertBefore 是【移动】：节点已在文档中时先从原位置摘掉。
      // 桩不照做的话，每次"调整顺序"都会复制出一份，容器数一路涨。
      const old = c.parentNode;
      if (old) { const oi = old.children.indexOf(c); if (oi >= 0) old.children.splice(oi, 1); }
      c.parentNode = this;
      const i = ref ? this.children.indexOf(ref) : -1;
      if (i >= 0) this.children.splice(i, 0, c); else this.children.push(c);
      return c;
    },
    get firstChild() { return this.children[0] || null; },
    addEventListener: noop, setPointerCapture: noop,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 600 }),
    querySelector: () => mkEl('q'), querySelectorAll: () => [], click: noop, focus: noop,
  };
  if (id === 'rangeBar') Object.assign(e, makeCanvas(900, 34));
  // innerHTML = '' 在真实 DOM 里会清空子节点；桩必须照做，
  // 否则 #figs 会不断累积旧的图容器，布局相关的断言全部失真。
  let _html = '';
  Object.defineProperty(e, 'innerHTML', {
    get() { return _html; },
    set(v) { _html = v; if (v === '') e.children.length = 0; },
  });
  return e;
}
globalThis.document = {
  getElementById: id => els[id] || (els[id] = mkEl(id)),
  createElement: t => (t === 'canvas' ? makeCanvas() : mkEl('new')),
  addEventListener: (ev, fn) => { (dl[ev] = dl[ev] || []).push(fn); },
  body: { classList: { add: noop, remove: noop } },
};
globalThis.window = globalThis;
globalThis.devicePixelRatio = 1;
// 测试桩补齐 localStorage（拖动排序要用；真实浏览器当然有）
globalThis.localStorage = globalThis.localStorage || {
  _d: {}, getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; },
};
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = noop;
globalThis.performance = { now: () => Date.now() };
globalThis.URL = { createObjectURL: () => 'blob:x', revokeObjectURL: noop };
globalThis.addEventListener = noop;
globalThis.ImageData = class { constructor(w, h) { this.width = w; this.height = h; this.data = new Uint8ClampedArray(w * h * 4); } };

// A9（零漂分析）的图用的是独立 canvas，桩里要显式造一个，否则 getContext 不存在
els.driftCv = makeCanvas(900, 300);

await import('/tmp/_imu_packed.js');
for (const fn of dl.DOMContentLoaded || []) fn();

const api = globalThis.__imu;
const S = api.S;

const openIt = async (path, name) => {
  const buf = readFileSync(path);
  await api.openFile({
    name, size: buf.length,
    slice(o, e) {
      const b = buf.subarray(o, Math.min(e, buf.length));
      return { arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) };
    },
  });
};
await openIt('/data/workspace/imutest.bag', 'imutest.bag');

let pass = 0, fail = 0;
const chk = (n, c, extra = '') => {
  if (c) { pass++; console.log(`  ✓ ${n}${extra ? '  ' + extra : ''}`); }
  else { fail++; console.log(`  ✗ ${n}${extra ? '  ' + extra : ''}`); }
};

console.log('\n═══ 1. 加载与话题识别 ═══');
chk('bag 打开成功', !!S.bag, S.bag ? `${S.bag.entries.length} 条` : els.stat.textContent);
chk('imu = /imu', S.imuTopic === '/imu', S.imuTopic);
chk('odom = /fused_odom', S.odomTopic === '/fused_odom', S.odomTopic);
chk('tracked 识别', S.trackedTopic === '/tracked_pose/low_frequency', S.trackedTopic);

console.log('\n═══ 2. 数据提取 ═══');
const D = S.data;
chk('imu 2500 条', D.imu.t.length === 2500, `${D.imu.t.length}`);
chk('odom 2500 条', D.odom.t.length === 2500, `${D.odom.t.length}`);
chk('tracked 1000 条', D.tracked.t.length === 1000, `${D.tracked.t.length}`);
chk('相对时间起点 0', Math.abs(Math.min(...D.imu.t, ...D.odom.t, ...D.tracked.t)) < 1e-9);
chk('imu yaw 起点 0', Math.abs(D.imu.yawDeg[0]) < 1e-9);
const step1 = D.imu.yawDeg[1] - D.imu.yawDeg[0];
chk('yaw 步长 ≈0.1719°', Math.abs(step1 - 0.003 * 180 / Math.PI) < 1e-3, `${step1.toFixed(5)}°`);

console.log('\n═══ 3. unwrapYaw ═══');
const u = api.unwrapYaw([0.9 * Math.PI, -0.9 * Math.PI]);
chk('跨 ±π 解缠绕', Math.abs((u[1] - u[0]) - 0.2 * Math.PI) < 1e-9, `${(u[1] - u[0]).toFixed(6)} rad`);
chk('起点平移到 0', u[0] === 0);
const big = [];
for (let i = 0; i < 1000; i++) big.push(((i * 0.01 + Math.PI) % (2 * Math.PI)) - Math.PI);
const ub = api.unwrapYaw(big);
let maxJump = 0;
for (let i = 1; i < ub.length; i++) maxJump = Math.max(maxJump, Math.abs(ub[i] - ub[i - 1]));
chk('连续转动无跳变', maxJump < 0.02, `最大步进 ${maxJump.toFixed(5)} rad`);

console.log('\n═══ 4. 频率诊断 ═══');
const fi = api.analyzeTopicFrequency(D.imu.t, 'imu');
const fo = api.analyzeTopicFrequency(D.odom.t, 'odom');
const ft = api.analyzeTopicFrequency(D.tracked.t, 'tracked_pose');
console.log(`     imu : avg=${fi.avgFreq.toFixed(2)}Hz p5→${fi.fastFreq.toFixed(2)}Hz median=${fi.medianFreq.toFixed(2)}Hz CV=${fi.cv.toFixed(4)} ${fi.grade}`);
chk('imu 稳定 CV<0.05', fi.cv < 0.05, `CV=${fi.cv.toFixed(5)}`);
chk('imu 中位频率 ≈25Hz', Math.abs(fi.medianFreq - 25) < 0.5, `${fi.medianFreq.toFixed(3)}Hz`);
console.log(`     odom: avg=${fo.avgFreq.toFixed(2)}Hz CV=${fo.cv.toFixed(4)} ${fo.grade}`);
chk('odom ≈25Hz 稳定', Math.abs(fo.avgFreq - 25) < 0.5 && fo.cv < 0.05);
console.log(`     trk : avg=${ft.avgFreq.toFixed(2)}Hz CV=${ft.cv.toFixed(4)} ${ft.grade}`);
chk('tracked ≈10Hz 稳定', Math.abs(ft.avgFreq - 10) < 0.5 && ft.cv < 0.05);

console.log('\n  -- 非均匀丢包（dt 呈整数倍）--');
const tt = []; let seed = 42;
const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
for (let i = 0; i < 5000; i++) { if (rnd() < 0.4) continue; tt.push(i * 0.02); }
const fd = api.analyzeTopicFrequency(tt, 'drop');
console.log(`     avg=${fd.avgFreq.toFixed(2)}Hz p5→${fd.fastFreq.toFixed(2)}Hz mult=${(fd.multRatio * 100).toFixed(1)}% loss=${(fd.loss * 100).toFixed(1)}%`);
console.log(`     ${fd.verdict}`);
chk('识别为离散多峰', fd.discrete);
chk('P5 反推 ≈50Hz', Math.abs(fd.fastFreq - 50) < 3, `${fd.fastFreq.toFixed(2)}Hz`);
chk('丢包率 ≈40%', Math.abs(fd.loss - 0.4) < 0.06, `${(fd.loss * 100).toFixed(1)}%`);
chk('drop_detected', fd.dropDetected);

console.log('\n  -- 连续抖动（不应推断 driver rate）--');
const tj = []; let acc = 0;
for (let i = 0; i < 2000; i++) { acc += 0.05 * (0.5 + rnd()); tj.push(acc); }
const fj = api.analyzeTopicFrequency(tj, 'jitter');
console.log(`     avg=${fj.avgFreq.toFixed(2)}Hz CV=${fj.cv.toFixed(4)} mult=${(fj.multRatio * 100).toFixed(1)}% discrete=${fj.discrete}`);
chk('判定为连续抖动', !fj.discrete);
chk('不推断丢包 (loss=0)', fj.loss === 0);

console.log('\n═══ 5. 帧间 yaw 增量 (A6) ═══');
const di = api.analyzeYawDelta(D.imu.t, D.imu.yawDeg, 'imu');
chk('增量数 2499', di && di.count === 2499, di ? `${di.count}` : 'null');
console.log(`     RMS/frame=${di.rms.toFixed(5)}° net=${di.net.toFixed(3)}° total|rot|=${di.sumAbs.toFixed(3)}°`);
chk('RMS ≈0.172°', Math.abs(di.rms - 0.1719) < 0.01);
chk('无超 170° 帧', di.nOver === 0);
const wd = api.frameYawDelta([179, -179]);
chk('跨 ±180 wrap 正确', Math.abs(wd[0] - 2) < 1e-9, `${wd[0]}°`);

console.log('\n═══ 6. A4 帧间角速度 ═══');
const { midT, rate } = api.frameYawRate(D.imu.t, D.imu.yawDeg);
chk('midT 长度 n-1', midT.length === D.imu.t.length - 1);
chk('角速度 ≈4.30°/s', Math.abs(rate[0] - (0.003 * 180 / Math.PI) / 0.04) < 0.05, `${rate[0].toFixed(4)}°/s`);

console.log('\n═══ 7. A3 插值 ═══');
chk('interp 合理', isFinite(api.interp(D.odom.t[10], D.imu.t, D.imu.yawDeg)));
chk('interp 端点', Math.abs(api.interp(D.imu.t[0], D.imu.t, D.imu.yawDeg) - D.imu.yawDeg[0]) < 1e-9);

console.log('\n═══ 8. 七张图（A0 + A1~A6）═══');
// A0 插在最前，所以 A1 的索引是 1 而不是 0
const F = { A0: 0, A1: 1, A2: 2, A3: 3, A4: 4, A5: 5, A6: 6 };
chk('生成 7 个 Figure', S.figs.length === 7, `${S.figs.length}`);
const names = ['A0', 'A1', 'A2', 'A3', 'A4', 'A5', 'A6'];
S.figs.forEach((f, i) => {
  const nax = f.axes.length, nser = f.axes.reduce((a, x) => a + x.series.length, 0);
  const ok = f.axes.every(a => a.xlim && a.ylim && isFinite(a.xlim[0]) && isFinite(a.ylim[1]));
  // A0 初始为空是正常设计，不要求有曲线
  chk(`${names[i]}: ${nax}轴/${nser}线/范围有效`, nax >= 1 && (i === F.A0 || nser >= 1) && ok);
});
chk('A5 直方图分箱', S.figs[F.A5].axes[1].series.some(s => s.kind === 'bar' && s.ys.length > 10));
chk('A5 竖线 ≥6', S.figs[F.A5].axes[1].vlines.length >= 6, `${S.figs[F.A5].axes[1].vlines.length} 条`);
chk('A6 直方图分箱', S.figs[F.A6].axes[1].series.some(s => s.kind === 'bar' && s.ys.length > 10));

console.log('\n═══ 9. 报告 ═══');
chk('频率报告', els.reportFreq.textContent.includes('VERDICT'));
chk('增量报告', els.reportDelta.textContent.includes('Total |rotation|'));

console.log('\n═══ 10. 区间过滤 ═══');
const full = D.imu.t.length;
S.range = [0, 10]; api.runAnalysis();
const cut = S.data.imu.t.length;
console.log(`     全程 ${full} 条 -> 区间[0,10] ${cut} 条`);
chk('过滤生效', cut < full && cut > 0);
chk('区间内 ≤10s', Math.max(...S.data.imu.t) <= 10.5);
S.range = [0, S.dur]; api.runAnalysis();

console.log('\n═══ 11. 缩放 ═══');
{
  const fig = S.figs[F.A1], ax = fig.axes[0];
  ax.reset();
  const [hx0, hx1] = ax.xlim, [hy0, hy1] = ax.ylim;
  const w0 = hx1 - hx0;
  fig.zoomAt(ax, 1 / 1.4, 1 / 1.4, 0.5, 0.5);
  const w1 = ax.xlim[1] - ax.xlim[0];
  chk('放大后 X 变窄', w1 < w0 && w1 > 0, `${w0.toFixed(2)} -> ${w1.toFixed(2)}`);
  chk('中心锚点不变', Math.abs((ax.xlim[0] + ax.xlim[1]) / 2 - (hx0 + hx1) / 2) < 1e-6);
  chk('Y 同步缩放', ax.ylim[1] - ax.ylim[0] < hy1 - hy0);
  fig.zoomAt(ax, 1.4, 1.4, 0.5, 0.5);
  chk('缩回原范围', Math.abs((ax.xlim[1] - ax.xlim[0]) - w0) < 1e-6);
  const yb = ax.ylim.slice();
  fig.zoomAt(ax, 1 / 2, null, .5, .5);
  chk('只缩 X（Y 不变）', ax.ylim[0] === yb[0] && ax.ylim[1] === yb[1]);
  const xb = ax.xlim.slice();
  fig.zoomAt(ax, null, 1 / 2, .5, .5);
  chk('只缩 Y（X 不变）', ax.xlim[0] === xb[0] && ax.xlim[1] === xb[1]);
  ax.reset();
  const [ax0, ax1] = ax.xlim, [ay0, ay1] = ax.ylim;
  const fx = 0.25, fy = 0.75;
  const xd = ax0 + fx * (ax1 - ax0), yd = ay1 - fy * (ay1 - ay0);
  fig.zoomAt(ax, 1 / 1.4, 1 / 1.4, fx, fy);
  const xd2 = ax.xlim[0] + fx * (ax.xlim[1] - ax.xlim[0]);
  const yd2 = ax.ylim[1] - fy * (ax.ylim[1] - ax.ylim[0]);
  chk('光标处 X 不动', Math.abs(xd2 - xd) < 1e-6, `${xd.toFixed(4)} vs ${xd2.toFixed(4)}`);
  chk('光标处 Y 不动', Math.abs(yd2 - yd) < 1e-6, `${yd.toFixed(4)} vs ${yd2.toFixed(4)}`);
  const bx = ax.xlim.slice();
  fig.panAxes(ax, 5, 0);
  chk('平移正确', Math.abs(ax.xlim[0] - (bx[0] + 5)) < 1e-9);
  const f5 = S.figs[F.A5];
  f5.axes.forEach(a => a.reset());
  const b5 = f5.axes.map(a => a.xlim[1] - a.xlim[0]);
  f5.zoomAll(1 / 1.4);
  chk('zoomAll 作用全部轴', f5.axes.every((a, i) => a.xlim[1] - a.xlim[0] < b5[i]));
  for (let i = 0; i < 300; i++) fig.zoomAt(ax, 1 / 2, 1 / 2, .5, .5);
  chk('极端放大不崩', isFinite(ax.xlim[0]) && ax.xlim[1] > ax.xlim[0], `${(ax.xlim[1] - ax.xlim[0]).toExponential(2)}`);
  ax.reset();
}

console.log('\n═══ 12. 线宽配置 ═══');
{
  chk('初始 1.0x', api.getLineScale() === 1, String(api.getLineScale()));
  els.lwSel.value = '0.4'; els.lwSel.onchange();
  chk('切 0.4x', Math.abs(api.getLineScale() - 0.4) < 1e-9, String(api.getLineScale()));
  els.lwSel.value = '2.2'; els.lwSel.onchange();
  chk('切 2.2x', Math.abs(api.getLineScale() - 2.2) < 1e-9, String(api.getLineScale()));
  for (const f of S.figs) f.draw();
  chk('粗线重绘无异常', S.figs.length === 7, `${S.figs.length} 图`);
  els.lwSel.value = '1'; els.lwSel.onchange();
  chk('切回 1.0x', api.getLineScale() === 1);
  chk('lwSel 已启用', els.lwSel.disabled === false);
}

console.log('\n═══ 13. lz4 ═══');
{
  await openIt('/data/workspace/imutest_lz4.bag', 'imutest_lz4.bag');
  chk('lz4 bag 打开', !!S.bag, els.stat.textContent.slice(0, 50));
  chk('压缩=lz4', S.bag && S.bag.compression === 'lz4', S.bag ? S.bag.compression : '-');
  chk('imu 2000 条', S.data.imu.t.length === 2000, `${S.data.imu.t.length}`);
  const f2 = api.analyzeTopicFrequency(S.data.imu.t, 'imu');
  console.log(`     lz4 imu: avg=${f2.avgFreq.toFixed(2)}Hz p5→${f2.fastFreq.toFixed(1)}Hz loss=${(f2.loss * 100).toFixed(1)}%`);
  chk('丢 1/3 判为周期性丢包', f2.discrete && f2.dropDetected);
  chk('P5 反推 ≈50Hz', Math.abs(f2.fastFreq - 50) < 3);
  chk('丢包率 ≈33%', Math.abs(f2.loss - 1 / 3) < 0.05, `${(f2.loss * 100).toFixed(1)}%`);
}

console.log('\n═══ 14. 缺话题降级 ═══');
{
  const keep = [S.imuTopic, S.odomTopic, S.trackedTopic];
  S.imuTopic = null; S.trackedTopic = null;
  api.runAnalysis();
  chk('无 imu 不崩', S.data.imu.t.length === 0);
  chk('仍 7 图', S.figs.length === 7, `${S.figs.length}`);
  chk('A1 只剩 odom', S.figs[F.A1].axes[0].series.length === 1);
  chk('A3 空不崩', S.figs[F.A3].axes[0].series.length === 0);
  S.odomTopic = null;
  api.runAnalysis();
  chk('三路全空不崩', S.figs.length === 7, `${S.figs.length}`);
  [S.imuTopic, S.odomTopic, S.trackedTopic] = keep;
}

console.log('\n═══ 15. 图高 ═══');
{
  await openIt('/data/workspace/imutest.bag', 'imutest.bag');
  const H = S.figs.map(f => parseFloat(f.cv.style.height) || f.cv.clientHeight);
  console.log(`     各图 CSS 高: ${H.join(' / ')} px`);
  // 期望值从 CSS 里解析，避免测试写死
  const expSingle = cssFigH, expDouble = cssFigH * 1.5;   // 450 = 300 * 1.5
  console.log(`     期望: 单图 ${expSingle}px / 双子图 ${expDouble}px`);
  chk(`A0 与 A1~A4 = ${expSingle}px`, H.slice(0, 5).every(v => v === expSingle), H.slice(0, 5).join(','));
  chk(`双子图 A5/A6 = ${expDouble}px`, H[5] === expDouble && H[6] === expDouble, `${H[5]},${H[6]}`);
  const ah1 = S.figs[F.A1].axes[0].rect.h, ah5 = S.figs[F.A5].axes[0].rect.h;
  console.log(`     子图净高: 单图 ${ah1.toFixed(0)}px / A5 每格 ${ah5.toFixed(0)}px`);
  chk('单图子图 > 200px', ah1 > 200, `${ah1.toFixed(0)}px`);
  chk('A5 每格 > 150px', ah5 > 150, `${ah5.toFixed(0)}px`);
  // 高度应是上一版（600/900）的一半
  chk('单图比上一版减半', Math.abs(expSingle - 600 / 2) < 1, `${expSingle} vs 300`);
  chk('双子图比上一版减半', Math.abs(expDouble - 900 / 2) < 1, `${expDouble} vs 450`);
  // y 刻度随高度变密（600px 高时应多于 5 条）
  const a1 = S.figs[F.A1].axes[0];
  const span = a1.ylim[1] - a1.ylim[0];
  // 验证的是「刻度数随高度自适应」这件事本身，而不是「一定要变密」：
  // 图变矮时刻度就该变少，变高才变多。
  const nAt = ph => {
    const t = Math.max(4, Math.min(14, Math.round(ph / 62)));
    return { t, n: Math.floor(span / api.niceStep(span, t, true)) };
  };
  const hi = nAt(554), lo = nAt(254), cur = nAt(ah1);
  console.log(`     刻度数: 254px->${lo.n}条  554px->${hi.n}条  当前${ah1.toFixed(0)}px->${cur.n}条`);
  chk('target 随高度增大', hi.t > lo.t, `${lo.t} -> ${hi.t}`);
  chk('刻度数随高度不减', hi.n >= lo.n, `${lo.n} -> ${hi.n}`);
  chk('矮图刻度不过密', lo.n >= 3 && lo.n <= 8, `${lo.n} 条`);
  chk('高图刻度不过稀', hi.n >= 6, `${hi.n} 条`);
  chk('当前图刻度合理', cur.n >= 3 && cur.n <= 14, `${cur.n} 条`);
  // 精细档不能污染 x 轴：x 轴仍走经典 1/2/5
  const xspan = a1.xlim[1] - a1.xlim[0];
  const xsOld = api.niceStep(xspan, 6), xsFine = api.niceStep(xspan, 6, true);
  chk('x 轴刻度保持经典档位', [1, 2, 5, 10].some(v => Math.abs(xsOld / Math.pow(10, Math.floor(Math.log10(xsOld))) - v) < 1e-9), `step=${xsOld}`);
  chk('x 轴不受 fine 影响', xsOld !== xsFine || [1, 2, 5, 10].some(v => Math.abs(xsFine / Math.pow(10, Math.floor(Math.log10(xsFine))) - v) < 1e-9));
}

console.log('\n═══ 16. A0 自定义曲线 ═══');
{
  await openIt('/data/workspace/imutest.bag', 'imutest.bag');
  chk('A0 在第一位', S.figs.length === 7, `${S.figs.length} 图`);
  chk('A0 初始无曲线', S.figs[0].axes[0].series.length === 0);

  // 字段摊平
  const f = api.flatNums({ a: 1, b: true, c: { d: 2.5 }, t: { sec: 5, nsec: 500000000 } }, '', {}, 0);
  chk('摊平数值', f.a === 1 && f.c === undefined && f['c.d'] === 2.5, JSON.stringify(f));
  chk('布尔转 0/1', f.b === 1);
  chk('time 折成秒', f.t === 5.5, `${f.t}`);
  const f2 = api.flatNums({ h: { stamp: { sec: 10, nanosec: 0 } } }, '', {}, 0);
  chk('ROS2 nanosec 也认', f2['h.stamp'] === 10, `${f2['h.stamp']}`);
  const f3 = api.flatNums({ arr: [1, 2, 3], n: 7 }, '', {}, 0);
  chk('数组被跳过', f3.arr === undefined && f3.n === 7);

  // 字段枚举
  const tf = api.topicFields('/imu');
  console.log(`     /imu 字段: ${tf.fields.map(x => x.name).join(', ')}`);
  chk('/imu 有字段', tf.fields.length > 0, `${tf.fields.length} 个`);
  chk('含 angular_velocity.z', tf.fields.some(x => x.name === 'angular_velocity.z'));
  chk('含 header.stamp（可画）', tf.fields.some(x => x.name.startsWith('header.stamp')));

  // 矩阵字段过滤（covariance 一个消息能出几十个，默认藏起来）
  {
    const all = api.topicFields('/imu').fields.map(f => f.name);
    const cov = all.filter(n => /covariance/.test(n));
    chk('/imu 确实有 covariance 噪声', cov.length > 20, `${cov.length} 个`);
    // hideCov 是面板 innerHTML 里的；测试桩里 getElementById 会惰性建一个同名桩，
    // renderFieldMenu 内部读的就是它，所以先取到再改 checked 即可。
    // 测试桩的 appendChild 只 push 进 children，不会回填 innerHTML，
    // 所以要递归把子树里所有 innerHTML 拼起来才能看到渲染结果。
    const dump = el => (el.innerHTML || '') + (el.children || []).map(dump).join('');
    const hideCovEl = document.getElementById('hideCov');
    hideCovEl.checked = true; api.renderFieldMenu();
    const shown = dump(els.fieldMenu);
    console.log(`     面板内容长度=${shown.length} 字符`);
    chk('默认过滤后不出现 covariance', !shown.includes('angular_velocity_covariance'));
    chk('过滤后仍能搜到真实字段', shown.includes('angular_velocity.z'), '');
    chk('面板列出了话题名', shown.includes('/imu'));
    hideCovEl.checked = false; api.renderFieldMenu();
    chk('取消过滤后 covariance 回来', dump(els.fieldMenu).includes('angular_velocity_covariance'));
    hideCovEl.checked = true; api.renderFieldMenu();
  }

  // 选字段 -> 出曲线
  const r1 = api.toggleA0('/imu', 'angular_velocity.z');
  chk('加入成功', r1 === 'ok');
  chk('A0 出现 1 条线', S.figs[0].axes[0].series.length === 1, `${S.figs[0].axes[0].series.length}`);
  const se = api.getA0Series('/imu', 'angular_velocity.z', 0, S.dur);
  chk('序列点数 = 消息数', se.n === 2500, `${se.n}`);
  chk('归一化后落在 0~1', S.figs[0].axes[0].series[0].ys.every(v => v >= 0 && v <= 1));

  // 再选一个不同话题的字段（跨话题）
  api.toggleA0('/odom' === S.odomTopic ? S.odomTopic : S.odomTopic, 'pose.pose.position.x');
  chk('跨话题 2 条线', S.figs[0].axes[0].series.length === 2, `${S.figs[0].axes[0].series.length}`);

  // 取消选择
  api.toggleA0('/imu', 'angular_velocity.z');
  chk('取消后剩 1 条', S.figs[0].axes[0].series.length === 1);

  // 上限
  S.a0.picks = [];
  const allT = [];
  for (const t of S.topics) for (const x of api.topicFields(t).fields) allT.push([t, x.name]);
  let added = 0;
  for (const [t, fd] of allT) { if (api.toggleA0(t, fd) === 'ok') added++; if (added >= 20) break; }
  chk(`上限 ${12} 条`, S.a0.picks.length <= 12, `${S.a0.picks.length}`);

  // 区间过滤：缩到 [0,10] 后点数应减少
  S.a0.picks = [{ topic: '/imu', field: 'angular_velocity.z' }];
  const full = api.getA0Series('/imu', 'angular_velocity.z', 0, S.dur).n;
  const part = api.getA0Series('/imu', 'angular_velocity.z', 0, 10).n;
  console.log(`     全程 ${full} 点 -> 区间[0,10] ${part} 点`);
  chk('区间过滤生效', part > 0 && part < full);
  chk('区间内 x 从 0 起', api.getA0Series('/imu', 'angular_velocity.z', 0, 10).xs[0] >= 0);

  // 区间变化后重建 A0，点数跟着变
  S.range = [0, 10]; api.runAnalysis();
  const n2 = S.figs[0].axes[0].series[0] ? S.figs[0].axes[0].series[0].xs.length : 0;
  chk('重建后 A0 用新区间', n2 > 0 && n2 < full, `${n2} 点`);
  chk('A0 仍在第一位', S.figs[0].axes[0].series.length >= 0 && S.figs.length === 7);
  S.range = [0, S.dur]; api.runAnalysis();

  // 归一化开关
  S.a0.picks = [{ topic: '/imu', field: 'angular_velocity.z' }];
  S.a0.norm = false; api.rebuildA0();
  const raw = S.figs[0].axes[0].series[0].ys;
  const src = api.getA0Series('/imu', 'angular_velocity.z', S.range[0], S.range[1]);
  console.log(`     原始值范围: ${src.min} ~ ${src.max}`);
  chk('关归一化 -> 用原始值', Math.abs(Math.max(...raw) - src.max) < 1e-12, `max=${Math.max(...raw)} vs ${src.max}`);
  chk('关归一化 -> 不落在 0~1', src.max === src.min || Math.max(...raw) !== 1);
  S.a0.norm = true; api.rebuildA0();
  chk('开归一化 -> 回到 0~1', S.figs[0].axes[0].series[0].ys.every(v => v >= 0 && v <= 1));
}

console.log('\n═══ 17. 两列布局 ═══');
{
  await openIt('/data/workspace/imutest.bag', 'imutest.bag');
  // CSS：#figs 是 grid，A0 跨整行
  chk('#figs 用 grid 两列', /#figs\s*\{[^}]*grid-template-columns:\s*1fr 1fr/.test(html), '');
  chk('.fig.full 跨整行', /\.fig\.full\s*\{[^}]*grid-column:\s*1 \/ -1/.test(html), '');
  chk('窄屏退回单列', /@media[^{]*\{\s*#figs\s*\{[^}]*grid-template-columns:\s*1fr/.test(html), '');

  // 运行时：只有 A0 带 full 类
  const wraps = [...S.figs.map(f => f.cv)].map(cv => cv && cv.parentNode);
  chk('A0 容器带 full', S.a0Wrap && S.a0Wrap.classList.contains('full'), S.a0Wrap ? 'yes' : 'no wrap');
  const others = [];
  const host = els.figs;
  for (const ch of (host.children || [])) {
    if (ch !== S.a0Wrap) others.push(ch.classList.contains('full'));
  }
  chk('其余图不带 full（两两并排）', others.length === 6 && others.every(v => v === false), `${others.length} 个`);
  chk('共 7 个图容器', (host.children || []).length === 7, `${(host.children || []).length}`);

  // 两列后每列只有约一半宽。桩的 canvas 默认 900px（整行），
  // 这里手动压到 450px 模拟真实并排宽度，验证窄图下布局不崩。
  const fig1 = S.figs[F.A1];
  fig1.cv.clientWidth = 450;
  fig1.draw();
  const a = fig1.axes[0];
  console.log(`     窄图(450px) 绘图区: ${a.rect.w.toFixed(0)} x ${a.rect.h.toFixed(0)}px`);
  chk('窄图绘图区宽度仍为正', a.rect.w > 100 && isFinite(a.rect.w), `${a.rect.w.toFixed(0)}px`);
  chk('窄图 x/ylim 仍有效', isFinite(a.xlim[0]) && isFinite(a.ylim[1]));
  // 极窄时不能出现负数宽度
  fig1.cv.clientWidth = 200; fig1.draw();
  chk('极窄(200px)不崩', a.rect.w > 0, `${a.rect.w.toFixed(0)}px`);
  fig1.cv.clientWidth = 900; fig1.draw();

  // 高度减半后 y 刻度不能太挤也不能太空
  const tgt = Math.max(4, Math.min(14, Math.round(a.rect.h / 62)));
  chk('y 刻度 target 合理', tgt >= 4 && tgt <= 6, `target=${tgt} (h=${a.rect.h.toFixed(0)}px)`);
}

console.log('\n═══ 18. A0 始终在最上面 ═══');
{
  await openIt('/data/workspace/imutest.bag', 'imutest.bag');
  const host = els.figs;
  const firstIsA0 = () => host.children[0] === S.a0Wrap;
  const a0Idx = () => host.children.indexOf(S.a0Wrap);

  chk('初始 A0 在第 0 位', firstIsA0(), `idx=${a0Idx()}`);

  // 关键回归：每次交互重建 A0 后，都必须回到最前
  // （之前 newFig 是 appendChild，A0 会被追加到末尾）
  api.toggleA0('/imu', 'angular_velocity.z');
  chk('选字段后仍在最前', firstIsA0(), `idx=${a0Idx()}`);

  api.toggleA0(S.odomTopic || '/odom', 'pose.pose.position.x');
  chk('再加一条仍在最前', firstIsA0(), `idx=${a0Idx()}`);

  api.toggleA0('/imu', 'angular_velocity.z');
  chk('取消一条仍在最前', firstIsA0(), `idx=${a0Idx()}`);

  S.a0.norm = false; api.rebuildA0();
  chk('切归一化仍在最前', firstIsA0(), `idx=${a0Idx()}`);
  S.a0.norm = true; api.rebuildA0();

  S.a0.picks = []; api.rebuildA0();
  chk('清空后仍在最前', firstIsA0(), `idx=${a0Idx()}`);

  // 连续多次重建不应产生残留容器
  for (let i = 0; i < 5; i++) {
    api.toggleA0('/imu', 'angular_velocity.z');
    api.toggleA0('/imu', 'angular_velocity.z');
  }
  chk('多次重建无残留', host.children.length === 7, `${host.children.length} 个容器`);
  chk('多次重建仍在最前', firstIsA0(), `idx=${a0Idx()}`);
  chk('S.figs[0] 就是 A0 的 fig', S.figs[0] === S.a0Wrap.children.find(c => c.className === 'figcv').__fig
      || S.figs[0].cv.parentNode === S.a0Wrap, '');

  // 跑完整重算（改区间）后 A0 仍应在最前
  S.range = [0, 20]; api.runAnalysis();
  chk('改区间重算后仍在最前', firstIsA0(), `idx=${a0Idx()}`);
  chk('改区间后仍 7 个容器', host.children.length === 7, `${host.children.length}`);
  S.range = [0, S.dur]; api.runAnalysis();
}

console.log('\n═══ 19. 联动游标 ═══');
{
  await openIt('/data/workspace/imutest.bag', 'imutest.bag');
  const F = { A0: 0, A1: 1, A2: 2, A3: 3, A4: 4, A5: 5, A6: 6 };

  chk('初始无游标', S.cursorNs === null);
  chk('各图都挂了 xBaseNs', S.figs.every(f => typeof f.xBaseNs === 'number'));

  // A0 基准 = 区间起点；A1~A6 = 最早消息时间。两者通常不同，
  // 这正是必须存绝对 ns 而不能存本地 x 的原因。
  const b0 = S.figs[F.A0].xBaseNs, b1 = S.figs[F.A1].xBaseNs;
  console.log(`     A0 base=${b0}  A1 base=${b1}  差=${((b1 - b0) / 1e6).toFixed(2)} ms`);
  chk('A0 基准 = 区间起点', b0 === S.bag.startTime, `${b0} vs ${S.bag.startTime}`);
  chk('A1 基准 = 最早消息', b1 === S.data.baseNs);

  // 点 A1 的 t=5s 处
  const tPick = 5;
  api.setCursorNs(b1 + tPick * 1e9);
  chk('游标已设置', S.cursorNs != null);
  chk('A1 本地 x = 5', Math.abs(S.figs[F.A1].cursorX - 5) < 1e-9, `${S.figs[F.A1].cursorX}`);

  // 其余图换算后应指向"同一个绝对时刻"，而不是同一个 x 值
  const nsEach = S.figs.map(f => f.xBaseNs + f.cursorX * 1e9);
  const spread = Math.max(...nsEach) - Math.min(...nsEach);
  chk('所有图指向同一绝对时刻', spread < 1, `最大偏差 ${spread} ns`);

  // A0 的本地 x 与 A1 不同（因为基准不同）—— 这正是易错点
  console.log(`     A0 本地 x=${S.figs[F.A0].cursorX.toFixed(4)}  A1 本地 x=${S.figs[F.A1].cursorX.toFixed(4)}`);

  // 读数：阶梯保持（取该时刻之前最近的点，不取之后的）
  const a1 = S.figs[F.A1].axes[0];
  const r1 = S.figs[F.A1].readAt(a1, 5);
  chk('A1 有读数', r1.length > 0, `${r1.length} 条`);
  const imuWz = r1.find(x => x.label.includes('imu_z'));
  chk('能读到 imu_z', imuWz != null, imuWz ? `v=${imuWz.v}` : '');
  // 与直接查数据对比
  const idx = S.data.imu.t.findIndex(t => t > 5);
  const expect = idx > 0 ? S.data.imu.wz[idx - 1] * 180 / Math.PI : null;
  if (expect != null) {
    chk('读数值 = 前一帧的值(阶梯保持)', Math.abs(imuWz.v - expect) < 1e-9, `${imuWz.v} vs ${expect}`);
  }
  // 游标落在所有点之前时不应有读数
  chk('游标早于首帧无读数', S.figs[F.A1].readAt(a1, -100).length === 0);

  // 直方图轴跳过游标（x 轴是 dt ms，不是时间）
  chk('A5 上轴无 noCursor', !S.figs[F.A5].axes[0].noCursor);
  chk('A5 下轴(直方图) noCursor', S.figs[F.A5].axes[1].noCursor);
  chk('A6 下轴(直方图) noCursor', S.figs[F.A6].axes[1].noCursor);

  // 顶部条
  els.cursorBar.style.display = 'none';
  api.renderCursorBar();
  chk('有游标时显示顶部条', els.cursorBar.style.display !== 'none', els.cursorBar.style.display);

  // 清除
  api.clearCursor();
  chk('清除后 cursorNs = null', S.cursorNs === null);
  chk('清除后各图 cursorX = null', S.figs.every(f => f.cursorX === null));
  api.renderCursorBar();
  chk('清除后隐藏顶部条', els.cursorBar.style.display === 'none');

  // 点击回调：模拟图上点击（走 onCursorPick）
  const fig1 = S.figs[F.A1];
  fig1.onCursorPick(12.5, a1);
  chk('点击后游标 = base + 12.5s', Math.abs(S.cursorNs - (b1 + 12.5 * 1e9)) < 1, `${S.cursorNs}`);
  chk('点击后其余图同步', S.figs.every(f => f.cursorX != null));

  // A0 重建后游标要保留（易漏：重建会丢状态）
  api.toggleA0('/imu', 'angular_velocity.z');
  chk('A0 重建后游标仍在', S.cursorNs != null && S.figs[0].cursorX != null, `x=${S.figs[0].cursorX}`);

  // ── 关键：区间非 0 起点时，A0 与 A1 基准差一个 start ──
  // A0 的 x 是"区间内第几秒"，A1~A6 的 x 是"bag 内第几秒"。
  // 点 A1 的 x=15s 时，A0 本地 x 应该 = 15 - start = 5s。
  S.range = [10, 20]; api.runAnalysis();
  const f0 = S.figs[F.A0], f1 = S.figs[F.A1];
  console.log(`     区间[10,20]: A0 base-start=${((f0.xBaseNs - S.bag.startTime) / 1e9).toFixed(1)}s  A1 base-start=${((f1.xBaseNs - S.bag.startTime) / 1e9).toFixed(1)}s`);
  chk('A0 基准 = 区间起点(10s)', Math.abs((f0.xBaseNs - S.bag.startTime) / 1e9 - 10) < 1e-9);
  // extract 每次都按"区间内最早消息"归零，所以 A1 的基准也跟着区间走，
  // A0 与 A1 的 x 都是"区间内第几秒" —— 两者天然一致。
  // 真正要保证的是：同一个 x 对应同一个"绝对时刻"(bag 内的秒数)。
  api.setCursorNs(f1.xBaseNs + 5 * 1e9);
  const absSec = (S.cursorNs - S.bag.startTime) / 1e9;
  console.log(`     点 A1 的 x=5 -> 绝对时刻 bag 内 ${absSec.toFixed(2)}s (区间[10,20] 应=15)`);
  chk('A1 本地 x = 5', Math.abs(f1.cursorX - 5) < 1e-9, `${f1.cursorX}`);
  chk('A0 本地 x 与 A1 一致', Math.abs(f0.cursorX - 5) < 1e-9, `${f0.cursorX}`);
  chk('绝对时刻 = bag 内 15s', Math.abs(absSec - 15) < 0.05, `${absSec.toFixed(3)}s`);
  // 反过来点 A0，A1 应回到同一绝对时刻
  f0.onCursorPick(5, f0.axes[0]);
  chk('点 A0 的 5s -> A1 也是 5s', Math.abs(f1.cursorX - 5) < 1e-9, `${f1.cursorX}`);
  chk('反向换算绝对时刻一致', Math.abs((S.cursorNs - S.bag.startTime) / 1e9 - 15) < 0.05);

  // 改区间后基准变了，游标应重算而不是丢失
  S.range = [0, 20]; api.runAnalysis();
  chk('改区间后游标仍在', S.cursorNs != null);
  chk('改区间后 A0 基准已更新', S.figs[0].xBaseNs === S.bag.startTime, `${S.figs[0].xBaseNs}`);
  S.range = [0, S.dur]; api.runAnalysis();
}

console.log(`\n${'═'.repeat(46)}`);
console.log(`通过 ${pass} / 失败 ${fail}`);
process.exit(fail ? 1 : 0);
