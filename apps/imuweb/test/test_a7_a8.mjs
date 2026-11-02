/**
 * imuweb 测试：A0 原始值（不换算单位）+ A7/A8 原始消息面板
 *
 * 用法：node aoting_tools/apps/imuweb/test/test_a7_a8.mjs
 */
import { readFileSync, writeFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');

const html = readFileSync(join(root, 'index.html'), 'utf-8');
const m = html.match(/<script>\n([\s\S]*?)\n<\/script>/);
if (!m) throw new Error('没从 html 里抽到 script');
writeFileSync('/tmp/_imu_a7.js', m[1]);

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
function makeCanvas(w = 900, h = 300) {
  const c = { width: w, height: h, clientWidth: w, style: {}, className: '', _cssH: h,
    getContext: () => c._ctx || (c._ctx = makeCtx()),
    getBoundingClientRect: () => ({ left: 0, top: 0, width: c.clientWidth, height: c.clientHeight }),
    addEventListener: noop, setPointerCapture: noop };
  Object.defineProperty(c, 'clientHeight', {
    get() { const v = parseFloat(c.style.height); return isFinite(v) && v > 0 ? v : c._cssH; },
    set(v) { c._cssH = v; }, configurable: true });
  return c;
}
const els = {}, dl = {};
function mkEl(id) {
  const e = { id, style: {}, value: id === 'lwSel' ? '1' : '', textContent: '', innerHTML: '',
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
    // normChk 默认【不勾选】（A0 画原始值）；hideCov 默认勾选（藏矩阵字段）
    checked: id === 'hideCov',
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
    insertBefore(c, ref) { c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1;
      if (i >= 0) this.children.splice(i, 0, c); else this.children.push(c); return c; },
    get firstChild() { return this.children[0] || null; },
    addEventListener: noop, setPointerCapture: noop,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 600 }),
    querySelector: () => mkEl('q'), querySelectorAll: () => [], click: noop, focus: noop };
  if (id === 'rangeBar') Object.assign(e, makeCanvas(900, 34));
  let _html = '';
  Object.defineProperty(e, 'innerHTML', { get() { return _html; }, set(v) { _html = v; if (v === '') e.children.length = 0; } });
  // select 的 options 要能读回来，才能验证下拉填充
  Object.defineProperty(e, 'options', { get() { return e.children.filter(c => c.tag === 'option' || c._opt); } });
  return e;
}
globalThis.document = {
  getElementById: id => els[id] || (els[id] = mkEl(id)),
  createElement: t => {
    const e = t === 'canvas' ? makeCanvas() : mkEl('new');
    if (t === 'option') { e.tag = 'option'; e._opt = true; }
    return e;
  },
  addEventListener: (ev, fn) => { (dl[ev] = dl[ev] || []).push(fn); },
  body: { classList: { add: noop, remove: noop }, appendChild: noop, removeChild: noop },
  execCommand: () => true,
  activeElement: null,
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
globalThis.navigator = { clipboard: null };
globalThis.ImageData = class { constructor(w, h) { this.width = w; this.height = h; this.data = new Uint8ClampedArray(w * h * 4); } };

// A9 零漂图用的是独立 canvas，桩里要显式造一个（否则 getContext 不存在）
els.driftCv = makeCanvas(900, 300);

await import('/tmp/_imu_a7.js');
for (const fn of dl.DOMContentLoaded || []) fn();

const api = globalThis.__imu;
const S = api.S;

const openIt = async (path, name) => {
  const buf = readFileSync(path);
  await api.openFile({ name, size: buf.length,
    slice(o, e) { const b = buf.subarray(o, Math.min(e, buf.length));
      return { arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) }; } });
};

let pass = 0, fail = 0;
const chk = (n, c, extra = '') => {
  if (c) { pass++; console.log(`  ✓ ${n}${extra ? '  ' + extra : ''}`); }
  else { fail++; console.log(`  ✗ ${n}${extra ? '  ' + extra : ''}`); }
};

// ══════════ 1. A0 原始值（用写入弧度值的 /imu_state bag） ══════════
console.log('\n═══ 1. A0 = 原始值，不换算单位 ═══');
await openIt('/data/workspace/imu_state.bag', 'imu_state.bag');
const F = 'raw_angular_velocity_avg_10s.x';
const se = api.getA0Series('/imu_state', F, 0, (S.bag.endTime - S.bag.startTime) / 1e9);
const EXP0 = 0.0174533;                    // 写进 bag 的弧度值
const DEG0 = EXP0 * 180 / Math.PI;         // 若被转成度则是 1.0000004
chk('取到 50 点', se.n === 50, `${se.n}`);
chk('ys[0] == 原始弧度值', Math.abs(se.ys[0] - EXP0) < 1e-12, `${se.ys[0]}`);
chk('ys[0] 不是度', Math.abs(se.ys[0] - DEG0) > 1e-6, `deg 会是 ${DEG0.toFixed(6)}`);
let maxErr = 0;
se.ys.forEach((v, i) => { maxErr = Math.max(maxErr, Math.abs(v - EXP0 * (1 + i * 0.01))); });
chk('全部点逐一对上原始序列', maxErr === 0, `最大误差 ${maxErr}`);

// 归一化默认必须关：否则 Y 轴是 0~1，看着像"值被改了"
chk('归一化默认关闭', els.normChk.checked === false, `checked=${els.normChk.checked}`);
chk('S.a0.norm === false', S.a0.norm === false, `${S.a0.norm}`);
api.toggleA0('/imu_state', F);
const a0fig = S.figs[0];
const a0ax = a0fig.axes[0];
chk('A0 是第一个图', a0fig === S.figs[0]);
chk('Y 轴标题不是 Normalized', !/Normalized/.test(a0ax.ylabel || ''), a0ax.ylabel);
chk('A0 Y 轴范围 = 原始值域', Math.abs(a0ax.ylim[1] - se.max) < Math.abs(se.max) * 0.2 + 1e-9,
  `ylim=[${a0ax.ylim[0].toFixed(6)}, ${a0ax.ylim[1].toFixed(6)}] 数据[${se.min.toFixed(6)}, ${se.max.toFixed(6)}]`);

// ══════════ 2. fmtRosWall ══════════
console.log('\n═══ 2. fmtRosWall（墙上时间） ═══');
// 2026-09-03 16:10:49.980 CST(+8) = UTC 08:10:49.980
const ns = Date.UTC(2026, 8, 3, 8, 10, 49, 980) * 1e6;
const wall = api.fmtRosWall(ns);
chk('格式为 MM-DD HH:MM:SS.mmm', /^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$/.test(wall), wall);
chk('东八区下显示 09-03 16:10:49.980', wall === '09-03 16:10:49.980', wall);
chk('null/NaN 不崩', api.fmtRosWall(null) === '--' && api.fmtRosWall(NaN) === '--');

// ══════════ 3. A7 面板 ══════════
console.log('\n═══ 3. A7 · 原始消息 ═══');
await openIt('/data/workspace/imutest.bag', 'imutest.bag');
chk('bag 打开', !!S.bag, `${S.bag.entries.length} 条 / ${S.topics.length} 话题`);
api.buildRawTopicSelector();
const sel = els.rawTopicSel;
chk('话题下拉已填充', sel.children.length === S.topics.length, `${sel.children.length} 个选项`);
chk('默认选中 imu 话题', sel.value === '/imu', sel.value);

const base = S.data.baseNs != null ? S.data.baseNs : S.bag.startTime;
// 在 5s 处放游标
api.setCursorNs(base + 5e9);
api.renderRawMsg();
const info = els.rawInfo, pre = els.rawJson;
chk('有游标时 info 显示话题与时刻', /\/imu @/.test(info.innerHTML), info.textContent.slice(0, 60));
chk('JSON 区有内容', pre.innerHTML.length > 50, `${pre.innerHTML.length} 字符`);
chk('JSON 含 angular_velocity', /angular_velocity/.test(pre.innerHTML));
chk('JSON 含高亮 span', /class="jk"/.test(pre.innerHTML));
chk('info 含墙上时间', /\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}/.test(info.innerHTML));

// 上/下条
const t0 = S.cursorNs;
api.stepRaw(1);
const t1 = S.cursorNs;
chk('下一条：游标前移', t1 > t0, `Δ=${((t1 - t0) / 1e6).toFixed(1)}ms`);
api.renderRawMsg();
chk('下一条后 JSON 仍可渲染', els.rawJson.innerHTML.length > 50);
api.stepRaw(-1);
chk('上一条：回到原处', S.cursorNs === t0, `Δ=${((S.cursorNs - t0) / 1e6).toFixed(1)}ms`);

// 边界：跳到最早之前
api.setCursorNs(S.bag.startTime - 1000);
api.renderRawMsg();
chk('游标早于首帧时不假装有数据', /之前没有/.test(els.rawInfo.textContent), els.rawInfo.textContent);

// ══════════ 4. A8 对照表 ══════════
console.log('\n═══ 4. A8 · 游标时刻对照 ═══');
api.setCursorNs(base + 5e9);
api.renderCompare();
const ct = els.cmpTable;
chk('对照表有内容', ct.textContent.length > 50, `${ct.textContent.length} 字符`);
chk('CSV 头正确', (ct._csv || '').split('\n')[0] === 'topic,t_rel_s,wall_clock,field,value');
const csvLines = (ct._csv || '').split('\n');
chk('CSV 有数据行', csvLines.length > 3, `${csvLines.length - 1} 行`);
chk('CSV 每行 5 列', csvLines.slice(1).every(l => l.split(',').length === 5));
chk('含 imu 话题', /\/imu,/.test(ct._csv));
const covHits = csvLines.filter(l => /covariance/.test(l)).length;
chk('默认过滤 covariance 矩阵字段', covHits === 0, `${covHits} 行含 covariance`);
chk('info 显示话题数/字段数', /\d+ 个话题 · \d+ 个数值字段/.test(els.cmpInfo.textContent), els.cmpInfo.textContent);

// ══════════ 5. 无游标 / 无 bag 的降级 ══════════
console.log('\n═══ 5. 降级 ═══');
api.clearCursor();
api.renderRawMsg();
chk('无游标时提示去点击', /单击/.test(els.rawInfo.textContent), els.rawInfo.textContent);
api.renderCompare();
chk('无游标时对照表也提示', /单击/.test(els.cmpInfo.textContent), els.cmpInfo.textContent);

// ══════════ 6. msgToJson ══════════
console.log('\n═══ 6. msgToJson ═══');
const j = api.msgToJson({ a: 1.23456789, b: new Float64Array([1, 2, 3]), c: 'x', d: true, e: null });
chk('数字保留 6 位小数', /1\.234568/.test(j), j.split('\n')[1]);
chk('TypedArray 折叠为 Array(n)', /Array\(3\)/.test(j));
chk('字符串/布尔/null 正常', /"c": "x"/.test(j) && /"d": true/.test(j) && /"e": null/.test(j));

console.log(`\n${fail === 0 ? '✓ 全部通过' : '✗ 有失败'} : ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
