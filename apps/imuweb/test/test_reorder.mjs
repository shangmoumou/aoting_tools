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
if (!m) throw new Error('no script');
writeFileSync('/tmp/_imu_a9.js', m[1]);

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

els.driftCv = makeCanvas(900, 300);
await import('/tmp/_imu_a9.js');
for (const fn of dl.DOMContentLoaded || []) fn();

const api = globalThis.__imu;
const S = api.S;
const RAD = 180 / Math.PI;
const RPH = RAD * 3600;


// ══════════ 排序（拖动 + ↑↓ 按钮） ══════════
// 背景：这些功能此前已写进 src，但没写测试 —— 结果打出去的包是旧的，
// 用户一直没拿到。这里补上，顺带锁死"顺序改动真的落到了 DOM"。
let pass = 0, fail = 0;
const chk = (n, c, extra = '') => {
  if (c) { pass++; console.log(`  ✓ ${n}${extra ? '  ' + extra : ''}`); }
  else { fail++; console.log(`  ✗ ${n}${extra ? '  ' + extra : ''}`); }
};
const host = document.getElementById('figs');

// 桩的 insertBefore 没有"移动"语义（真 DOM 会把节点从原位置摘掉），
// 不补上就会复制出重复节点：moveBlock 明明只动一次，DOM 里却出现两个 A1。
{
  const _ib = host.insertBefore.bind(host);
  host.insertBefore = (c, ref) => {
    const old = c.parentNode;
    if (old) { const i = old.children.indexOf(c); if (i >= 0) old.children.splice(i, 1); }
    return _ib(c, ref);
  };
}

/** 造一个可排序的块（.fig 或 details），塞进 #figs */
function mkBlock(key, isDetails) {
  const e = document.createElement(isDetails ? 'details' : 'div');
  if (isDetails) e.tagName = 'DETAILS';
  e.className = isDetails ? '' : 'fig';
  e.dataset.fid = key;
  host.appendChild(e);
  return e;
}
const ids = () => api.figBlocks().map(api.figKeyOf);

console.log('\n═══ 1. figKeyOf 识别两类块 ═══');
host.children.length = 0;
const A = mkBlock('A0', false), D7 = mkBlock('A7', true);
chk('.fig 能认出来', api.figKeyOf(A) === 'A0', api.figKeyOf(A));
chk('details 能认出来', api.figKeyOf(D7) === 'A7', api.figKeyOf(D7));
chk('没 fid 的返回 null', api.figKeyOf(document.createElement('div')) === null);

console.log('\n═══ 2. reorderBlocks 排序 ═══');
host.children.length = 0;
['A0', 'A1', 'A2', 'A7', 'A9'].forEach((k, i) => mkBlock(k, i >= 3));
chk('初始顺序', ids().join(',') === 'A0,A1,A2,A7,A9', ids().join(','));
api.reorderBlocks(['A7', 'A0']);
chk('指定顺序生效', ids().join(',') === 'A7,A0,A1,A2,A9', ids().join(','));
chk('没提到的块补到末尾', ids().slice(2).join(',') === 'A1,A2,A9');
api.reorderBlocks(['A9', 'A0', 'A1', 'A2', 'A7']);
chk('全量重排正确', ids().join(',') === 'A9,A0,A1,A2,A7', ids().join(','));
api.reorderBlocks(['X9', 'A0']);
chk('不存在的 key 被忽略', ids().join(',') === 'A0,A9,A1,A2,A7', ids().join(','));

console.log('\n═══ 3. moveBlock 上移 / 下移 ═══');
host.children.length = 0;
['A0', 'A1', 'A2'].forEach(k => mkBlock(k, false));
chk('下移 A0', api.moveBlock('A0', 1) === true && ids().join(',') === 'A1,A0,A2', ids().join(','));
chk('再下移 A0 到末尾', api.moveBlock('A0', 1) === true && ids().join(',') === 'A1,A2,A0', ids().join(','));
chk('已在末尾：返回 false 且不动', api.moveBlock('A0', 1) === false && ids().join(',') === 'A1,A2,A0');
chk('上移 A0', api.moveBlock('A0', -1) === true && ids().join(',') === 'A1,A0,A2', ids().join(','));
chk('上移到首位', api.moveBlock('A0', -1) === true && ids().join(',') === 'A0,A1,A2', ids().join(','));
chk('已在首位：返回 false', api.moveBlock('A0', -1) === false && ids().join(',') === 'A0,A1,A2');
chk('不存在的 key 返回 false', api.moveBlock('ZZ', 1) === false);
chk('两边都有块时是交换而非覆盖', (() => {
  const before = ids().join(','); api.moveBlock('A1', 1); api.moveBlock('A1', -1);
  return ids().join(',') === before;
})(), ids().join(','));

console.log('\n═══ 4. 记忆与恢复 ═══');
host.children.length = 0;
['A0', 'A1', 'A2'].forEach(k => mkBlock(k, false));
api.moveBlock('A2', -1); api.moveBlock('A2', -1);      // A2 → 最前
chk('移动后已保存', JSON.parse(localStorage.getItem(api.FIG_ORDER_KEY)).join(',') === 'A2,A0,A1',
  localStorage.getItem(api.FIG_ORDER_KEY));
api.reorderBlocks(['A0', 'A1', 'A2']);                  // 打乱 DOM，靠记忆恢复
api.applyFigOrder();
chk('applyFigOrder 按记忆恢复', ids().join(',') === 'A2,A0,A1', ids().join(','));
api.resetFigOrder();
chk('resetFigOrder 回到默认', ids().slice(0, 3).join(',') === 'A0,A1,A2', ids().join(','));
chk('reset 后记忆已清除', localStorage.getItem(api.FIG_ORDER_KEY) === null);

console.log('\n═══ 5. ↑↓ 按钮 ═══');
host.children.length = 0;
const f1 = mkBlock('A0', false);
api.appendMoveBtns(f1, 'A0', false);
const btns = f1.children.filter(c => c.className === 'mvbtn');
chk('生成 2 个按钮', btns.length === 2, `${btns.length}`);
chk('文本是 ↑ ↓', btns.map(b => b.textContent).join('') === '↑↓', btns.map(b => b.textContent).join(''));
mkBlock('A1', false);
let collapsed = false;
btns[1].onclick({ preventDefault: () => { collapsed = true; }, stopPropagation: () => {} });
chk('点 ↓ 触发下移', collapsed && ids().join(',') === 'A1,A0', ids().join(','));
btns[0].onclick({ preventDefault: () => {}, stopPropagation: () => {} });
chk('点 ↑ 触发上移', ids().join(',') === 'A0,A1', ids().join(','));
chk('summary 里的点击被拦下（不误折叠）', collapsed === true);

console.log('\n═══ 6. 报告：deg/s 双单位 + 结论段 ═══');
// 这两个是用户这次要的东西：每个量都要能看到"多少度/秒"，末尾要有"零漂到底怎么样"
await (async () => {
  const { readFileSync } = await import('fs');
  const buf = readFileSync('/data/workspace/imu_state.bag');
  await api.openFile({ name: 'imu_state.bag', size: buf.length,
    slice(o, e) { const b = buf.subarray(o, Math.min(e, buf.length));
      return { arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) }; } });
})();
const base = S.data.baseNs;
els.driftWin.value = '0';
for (const i of [0, 10, 20, 30, 40]) { api.setCursorNs(base + i * 1e8); api.driftAddPick(); }
api.driftToggleDone();
api.renderDriftReport();
const rep = els.driftReport.textContent;
chk('有【六、结论】', /【六、结论】/.test(rep));
chk('结论含"零偏量级"', /零偏量级/.test(rep));
chk('结论同时给出 deg/s', /deg\/s/.test(rep.split('【六、结论】')[1] || ''));
chk('结论有"实际影响（°/min）"', /°\/min/.test(rep));
chk('结论有"稳定性"', /稳定性/.test(rep));
chk('结论有"总体"判定', /→ 总体：/.test(rep));
chk('结论有"建议"', /→ 建议：/.test(rep));
chk('结论有前提说明（静止假设）', /前提/.test(rep));
{
  const s1 = rep.split('【一、')[1].split('【二、')[0];
  chk('第一节同时有 deg/s 与 °/h 列', /均值\(deg\/s\)/.test(s1) && /均值\(°\/h\)/.test(s1));
  chk('第一节极差也是双单位', /极差\(deg\/s\)/.test(s1) && /极差\(°\/h\)/.test(s1));
  const s2 = rep.split('【二、')[1].split('【三、')[0];
  chk('第二节三轴各给双单位', (s2.match(/ΔX\(deg\/s\)/g) || []).length === 1 && /ΔY\(deg\/s\)/.test(s2) && /ΔZ\(deg\/s\)/.test(s2));
  const s3 = rep.split('【三、')[1].split('【四、')[0];
  chk('第三节有 Δ(deg/s)', /Δ\(deg\/s\)/.test(s3) && /Δ\(°\/h\)/.test(s3));
  // 数值自洽：同一行的 deg/s 与 °/h 必须差 3600 倍（差 57.3 倍是曾经踩过的坑）
  const line = s3.split('\n').find(l => /^\s*X\s+#/.test(l)) || '';
  const nums = line.trim().split(/\s{2,}|\s+/).slice(2).map(parseFloat);
  chk('Δ(°/h) = Δ(deg/s)×3600', Math.abs(nums[1] - nums[0] * 3600) < 1e-3 * Math.abs(nums[1]) + 1e-6,
    `${nums[0]} vs ${nums[1]}`);
}

console.log(`\n${fail === 0 ? '✓ 全部通过' : '✗ 有失败'} : ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
