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
    disabled: false, className: '', children: [], onclick: null, onchange: null, title: '',
    // normChk 默认【不勾选】（A0 画原始值）；hideCov 默认勾选（藏矩阵字段）
    checked: id === 'hideCov',
    contains() { return false; }, closest() { return null; },
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, f) { f ? this._s.add(c) : this._s.delete(c); }, contains(c) { return this._s.has(c); } },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
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
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = noop;
globalThis.performance = { now: () => Date.now() };
globalThis.URL = { createObjectURL: () => 'blob:x', revokeObjectURL: noop };
globalThis.addEventListener = noop;
globalThis.navigator = { clipboard: null };
globalThis.ImageData = class { constructor(w, h) { this.width = w; this.height = h; this.data = new Uint8ClampedArray(w * h * 4); } };

els.driftCv = makeCanvas(900, 300);
await import('/tmp/_imu_a9.js');
els.driftCv = makeCanvas(900, 300);
await import('/tmp/_imu_a9.js');
for (const fn of dl.DOMContentLoaded || []) fn();
const api = globalThis.__imu; const S = api.S;
const buf = readFileSync('/data/workspace/imu_state.bag');
await api.openFile({ name:'x', size:buf.length, slice(o,e){const b=buf.subarray(o,Math.min(e,buf.length));return {arrayBuffer:async()=>b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength)};} });
console.log('sel=', JSON.stringify(S.drift.sel));
const base = S.data.baseNs;
console.log('base', base, 'bagStart', S.bag.startTime);
const idx = S.bag.byTopic.get('/imu_state');
console.log('idxlen', idx && idx.length);
console.log('stamp0', S.bag.entries[idx[0]].stamp);
const e = S.bag.latestBefore('/imu_state', base + 20*1e8);
console.log('latestBefore', e && e.stamp);
console.log('decode', e && JSON.stringify(S.bag.decode(e)));
console.log('flat', e && JSON.stringify(api.flatNums(S.bag.decode(e), '', Object.create(null), 0)));
console.log('sampleAt', JSON.stringify(api.driftSampleAt(S.drift.sel.x, base + 20*1e8, 0)));
console.log('sampleAt2', JSON.stringify(api.driftSampleAt(S.drift.sel.x, base + 20*1e8, 2)));
