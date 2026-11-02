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
const near = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;

// ══════════ 1. 纯函数 ══════════
console.log('\n═══ 1. 工具函数 ═══');
{
  const rows = api.padRows([['a', 'bb', '1'], ['cc', 'd', '22']]);
  chk('padRows 数字右对齐', rows[0].startsWith('a ') && rows[1].startsWith('cc'), JSON.stringify(rows));
  chk('padRows 列宽一致', rows[0].length === rows[1].length, `${rows[0].length} vs ${rows[1].length}`);
  chk('fmtSigned 带正号', api.fmtSigned(1.5, 3) === '+1.500', api.fmtSigned(1.5, 3));
  chk('fmtSigned 负数', api.fmtSigned(-1.5, 3) === '-1.500', api.fmtSigned(-1.5, 3));
  chk('fmtSigned NaN 显示 —', api.fmtSigned(NaN) === '—');
  chk('fmtSci 0 不显示 0e+0', api.fmtSci(0) === '0', api.fmtSci(0));
  const f = api.linfit([0, 1, 2, 3], [1, 3, 5, 7]);
  chk('linfit 斜率正确', near(f.slope, 2), `${f.slope}`);
  chk('linfit 截距正确', near(f.intercept, 1), `${f.intercept}`);
  chk('linfit 完美线性 R²=1', near(f.r2, 1, 1e-12), `${f.r2}`);
  chk('linfit 点数不足返回 null', api.linfit([1], [1]) === null);
  chk('biasGrade 分级', /战术级/.test(api.biasGrade(0.5)) && /消费级/.test(api.biasGrade(50)) && /标定/.test(api.biasGrade(500)),
    api.biasGrade(50));
  chk('°/h 换算常数', near(api.RAD_TO_DEG_PER_H, RAD * 3600, 1e-6), `${api.RAD_TO_DEG_PER_H}`);
}

// ══════════ 2. 载入 bag 与自动识别 ══════════
console.log('\n═══ 2. 自动识别陀螺三轴组 ═══');
await openIt('/data/workspace/imu_state.bag', 'imu_state.bag');
chk('bag 已加载', !!S.bag, `${S.bag.entries.length} 条`);
const G = api.detectGyroGroups();
chk('识别到 1 个三轴组', G.length === 1, JSON.stringify(G.map(g => g.prefix)));
chk('识别出 raw_angular_velocity_avg_10s', G[0] && G[0].prefix === 'raw_angular_velocity_avg_10s', G[0] && G[0].prefix);
chk('三轴齐全', !!(G[0].axes.x && G[0].axes.y && G[0].axes.z));
chk('下拉已填充（未选择+组+自定义）', els.driftGroup.children.length === 3,
  els.driftGroup.children.map(o => o.value).join('|'));
chk('默认选中第 0 组', els.driftGroup.value === '0', els.driftGroup.value);
chk('三轴字段已就位', S.drift.sel.x && S.drift.sel.x.field === 'raw_angular_velocity_avg_10s.x',
  JSON.stringify(S.drift.sel.x));
chk('GYRO_FIELD_RE 不匹配 covariance', !api.GYRO_FIELD_RE.test('angular_velocity_covariance.0'));

// ══════════ 3. 采样（win=0，单帧） ══════════
console.log('\n═══ 3. 记录采样点（原始 rad/s） ═══');
const base = S.data.baseNs;
const IDX = [0, 2, 5, 10, 20, 40];
const expected = i => 0.0174533 * (1 + i * 0.01);
els.driftWin.value = '0';
api.clearCursor();
chk('无游标时报错提示', api.driftAddPick() === '请先在图上单击，定位游标', String(api.driftAddPick()));
for (const i of IDX) {
  api.setCursorNs(base + i * 100000000);
  const err = api.driftAddPick();
  chk(`采点 i=${i} 成功`, err === null, err || '');
}
chk('采样点数量正确', S.drift.picks.length === IDX.length, `${S.drift.picks.length}`);
let maxErr = 0;
S.drift.picks.forEach((p, k) => { maxErr = Math.max(maxErr, Math.abs(p.x - expected(IDX[k]))); });
chk('存的是原始 rad/s（未换算）', maxErr === 0, `最大误差 ${maxErr}`);
chk('不是 deg', Math.abs(S.drift.picks[0].x - 0.0174533 * RAD) > 1e-6, `deg 会是 ${(0.0174533 * RAD).toFixed(6)}`);
chk('y/z 比例正确 (0.5 / 0.25)', near(S.drift.picks[0].y, S.drift.picks[0].x * 0.5) && near(S.drift.picks[0].z, S.drift.picks[0].x * 0.25));
chk('win=0 时样本数=1', S.drift.picks.every(p => p.nx === 1));
chk('按时间排序', S.drift.picks.every((p, k) => k === 0 || p.tNs >= S.drift.picks[k - 1].tNs));

// ══════════ 4. 窗口均值 ══════════
console.log('\n═══ 4. 采样窗口（压噪声） ═══');
api.driftClearPicks();
els.driftWin.value = '2';
api.setCursorNs(base + 10 * 100000000);      // t=1.0s，窗口 ±1s → i=0..20
chk('窗口采样成功', api.driftAddPick() === null);
{
  const p = S.drift.picks[0];
  chk('窗口内样本数 = 21', p.nx === 21, `${p.nx}`);
  chk('均值 = 理论值 0.0174533×1.1', near(p.x, 0.0174533 * 1.1, 1e-12), `${p.x}`);
  chk('窗口内标准差 > 0', p.sdx > 0, `${p.sdx.toExponential(3)}`);
}

// ══════════ 5. 表格 ══════════
console.log('\n═══ 5. 采样点表格 ═══');
api.driftClearPicks();
els.driftWin.value = '0';
for (const i of IDX) { api.setCursorNs(base + i * 100000000); api.driftAddPick(); }
api.renderDriftTable();
{
  const t = els.driftTable.textContent;
  chk('表头含各列', /t_rel\(s\)/.test(t) && /墙上时间/.test(t) && /X\(deg\/s\)/.test(t) && /\|b\|\(deg\/s\)/.test(t));
  chk('行数 = 采样点数+1', t.split('\n').length === IDX.length + 1, `${t.split('\n').length}`);
  chk('首行 t_rel = 0.000', /\b0\.000\b/.test(t));
  chk('含墙上时间格式', /\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}/.test(t));
  chk('deg/s 值正确', t.includes((expected(0) * RAD).toFixed(7).replace(/^/, '+')), (expected(0) * RAD).toFixed(7));
}

// ══════════ 6. 图 ══════════
console.log('\n═══ 6. 零漂曲线图 ═══');
api.renderDriftFigure();
{
  const fig = S._driftFig;
  chk('图已创建', !!fig && fig.axes.length === 1);
  const a = fig.axes[0];
  chk('三轴各一条曲线', a.series.length === 3, `${a.series.length}`);
  chk('Y 轴单位是 deg/s', /deg\/s/.test(a.ylabel), a.ylabel);
  chk('Y 有零参考线', a.yZeroLine === true);
  let ok = true;
  S.drift.picks.forEach((p, k) => { if (Math.abs(a.series[0].ys[k] - p.x * RAD) > 1e-15) ok = false; });
  chk('曲线值 = 原始 rad/s 转成 deg/s', ok, `${a.series[0].ys[0]}`);
  chk('X 轴 = 相对时刻', near(a.series[0].xs[1], 0.2, 1e-9), `${a.series[0].xs[1]}`);
}

// ══════════ 7. 未结束选择时不生成报告 ══════════
console.log('\n═══ 7. 结束选择前 ═══');
chk('未结束', S.drift.closed === false);
api.renderDriftReport();
chk('提示去点结束', /结束选择并分析/.test(els.driftReport.textContent), els.driftReport.textContent);

// ══════════ 8. 报告 ══════════
console.log('\n═══ 8. 零漂报告 ═══');
api.driftToggleDone();
chk('已结束', S.drift.closed === true);
api.renderDriftReport();
const rep = els.driftReport.textContent;
console.log('\n──────── 报告全文 ────────');
console.log(rep);
console.log('──────────────────────────\n');
chk('五个小节齐全',
  /一、每轴零偏统计/.test(rep) && /二、相邻采样点的零偏变化/.test(rep) &&
  /三、最大变化发生在哪两帧/.test(rep) && /四、零漂趋势/.test(rep) && /五、量级参考/.test(rep));
chk('跨度 = 4.0s', /4\.0s/.test(rep));
// 值随 t 线性：rad = 0.0174533*(1+0.1t) → 每秒 0.1 deg/s
// 三轴合成 |Δ| = Δx·√(1+0.25+0.0625) = 1.1456·Δx → 0.1×1.1456 = 1.146e-1
chk('每秒变化恒为 1.146e-1（三轴合成）', (rep.match(/1\.146e-1/g) || []).length >= 5,
  `${(rep.match(/1\.146e-1/g) || []).length} 处`);
chk('逐轴速率在第三节为 1.000e-1', /X  #5→#6.*1\.000e-1/.test(rep));
// 最后一个区间 #5→#6: Δi=20 → Δ=0.2 deg/s = 720 °/h
chk('最大区间 ΔX = 720 °/h', /#5→#6/.test(rep) && /720\.000/.test(rep));
chk('单轴最大落在 #5→#6', /单轴最大：X 轴在 #5→#6/.test(rep));
chk('三轴合成最大也是 #5→#6', /三轴合成最大：#5→#6/.test(rep));
chk('趋势 R² = 1.000', /1\.000/.test(rep) && /R²/.test(rep));

// ── 单位换算回归：这三列曾经各差一个 57.3 倍（180/π）──
// X 的零偏 = 1.000 deg/s（t=0），斜率 = 0.1 (deg/s)/s
//   起始零偏 = 1.0 deg/s × 3600        = 3600 °/h
//   斜率     = 0.1 × 3600 × 3600       = 1296000 °/h per h
//   1σ(deg/s) 必须 = 1σ(°/h) / 3600（同一量，只是单位不同）
{
  // 从【四、】里取 X 那一行的数值：斜率(deg/s per h) / 斜率(°/h per h) / 起始零偏(°/h) / R²
  const sec4 = rep.split('【四、')[1] || '';
  // 按表头取列，别用硬编码下标 —— 以后插一列就会静默读错
  const lines4 = sec4.split('\n').filter(l => l.trim());
  const hi = lines4.findIndex(l => /(^|\s)轴(\s|$)/.test(l));   // 小节标题那行不算
  // 必须按【两个及以上空格】切：列名本身含空格（如"斜率(deg/s per h)"），
  // 而 padRows 用双空格做列分隔，内部只有单空格。
  const SPLIT = /\s{2,}/;
  const hdr4 = (lines4[hi] || '').trim().split(SPLIT);
  const rowX = (lines4.slice(hi + 1).find(l => /^\s*X\s/.test(l)) || '').trim().split(SPLIT);
  const col = n => { const i = hdr4.indexOf(n); return i >= 0 ? parseFloat(rowX[i]) : NaN; };
  const slopePerH = col('斜率(°/h per h)'), interceptH = col('起始零偏(°/h)');
  chk('表头含 deg/s 与 °/h 双单位',
    hdr4.includes('起始零偏(deg/s)') && hdr4.includes('起始零偏(°/h)'), hdr4.join(','));
  chk('起始零偏 deg/s 与 °/h 相差 3600 倍', near(col('起始零偏(deg/s)') * 3600, interceptH, 1),
    `${col('起始零偏(deg/s)')} vs ${interceptH}`);
  // X 零偏 = 1.000 deg/s（t=0）、斜率 = 0.1 (deg/s)/s
  //   起始零偏 = 1.0 deg/s × 3600  = 3600 °/h
  //   斜率     = 0.1 × 3600 × 3600 = 1296000 °/h per h
  chk('起始零偏 ≈ 3600 °/h（不是 206264）', near(interceptH, 3600, 2), `${interceptH}`);
  chk('趋势斜率 ≈ 1296000 °/h per h', near(slopePerH, 1296000, 200), `${slopePerH}`);
}
{
  const sec1 = rep.split('【一、')[1] || '';
  const xl = (sec1.split('\n').find(l => l.trim().startsWith('X ')) || '').trim().split(/\s{2,}|\s+/);
  const sdDeg = parseFloat(xl[4]), sdH = parseFloat(xl[5]);
  chk('1σ(deg/s) 与 1σ(°/h) 自洽（差 3600 倍）',
    near(sdH / 3600, sdDeg, Math.abs(sdDeg) * 1e-3 + 1e-9), `${sdDeg} vs ${(sdH / 3600).toFixed(6)}`);
}
chk('含量级评估', /当前最大零偏/.test(rep));
chk('含温漂提示', /温漂/.test(rep));

// 单位换算精确校验
{
  const p0 = S.drift.picks[0], pN = S.drift.picks[S.drift.picks.length - 1];
  const dRad = pN.x - p0.x;
  chk('Δ 换算 °/h 正确', near(dRad * RPH, 0.0174533 * 0.01 * 40 * RPH, 1e-3),
    `${(dRad * RPH).toFixed(4)} °/h`);
  chk('CSV 头正确', (els.driftReport._csv.split('\n')[0]).startsWith('idx,t_rel_s,wall_clock,x_rad_s'));
  const c1 = els.driftReport._csv.split('\n')[1].split(',');
  chk('CSV 行数 = 采样点数', els.driftReport._csv.split('\n').length === IDX.length + 1);
  chk('CSV 第 4 列是原始 rad/s', near(parseFloat(c1[3]), expected(0), 1e-15), c1[3]);
  chk('CSV 第 7 列是 deg/s', near(parseFloat(c1[6]), expected(0) * RAD, 1e-7), c1[6]);
  chk('CSV 第 10 列是 °/h', near(parseFloat(c1[9]), expected(0) * RPH, 1e-3), c1[9]);
}

// ══════════ 9. 编辑采样点 ══════════
console.log('\n═══ 9. 删除 / 清空 / 继续添加 ═══');
api.driftRemoveLast();
chk('删除上一条后少一个', S.drift.picks.length === IDX.length - 1, `${S.drift.picks.length}`);
api.driftToggleDone();
chk('继续添加：回到未结束', S.drift.closed === false);
api.setCursorNs(base + 45 * 100000000);
chk('可继续采样', api.driftAddPick() === null, `${S.drift.picks.length} 点`);
api.driftClearPicks();
chk('清空', S.drift.picks.length === 0 && S.drift.closed === false);
// 只有 1 个点就结束 -> 明确告知需要 2 个
api.setCursorNs(base + 1e8);
api.driftAddPick();
api.driftToggleDone();
api.renderDriftReport();
chk('只有 1 点时给出提示', /至少需要 2 个采样点/.test(els.driftReport.textContent), els.driftReport.textContent);
api.driftClearPicks();

// ══════════ 10. 换区间后 t_rel 跟随 ══════════
console.log('\n═══ 10. 改区间后基准跟随 ═══');
els.driftWin.value = '0';
for (const i of [10, 30]) { api.setCursorNs(base + i * 100000000); api.driftAddPick(); }
const relBefore = (S.drift.picks[0].tNs - S.data.baseNs) / 1e9;
S.range = [1, 3];
api.runAnalysis();
const relAfter = (S.drift.picks[0].tNs - S.data.baseNs) / 1e9;
chk('基准变了', !near(relBefore, relAfter, 1e-6), `${relBefore.toFixed(3)} → ${relAfter.toFixed(3)}`);
chk('采样点没丢', S.drift.picks.length === 2);
api.renderDriftTable();
chk('表格 t_rel 跟着变', els.driftTable.textContent.includes(relAfter.toFixed(3)), relAfter.toFixed(3));

// ══════════ 11. 另一个 bag（/imu 话题） ══════════
console.log('\n═══ 11. 换 bag：imutest.bag ═══');
await openIt('/data/workspace/imutest.bag', 'imutest.bag');
{
  const g2 = api.detectGyroGroups();
  chk('识别到 /imu 的 angular_velocity', g2.some(g => /\/imu$/.test(g.topic) && g.prefix === 'angular_velocity'),
    g2.map(g => `${g.topic}·${g.prefix}`).join(' | '));
  chk('换 bag 后采样点已清空', S.drift.picks.length === 0);
  chk('换 bag 后自动选中新组', !!S.drift.sel.x, JSON.stringify(S.drift.sel.x));
  const b2 = S.data.baseNs;
  els.driftWin.value = '0.5';
  for (const t of [1, 5, 9]) { api.setCursorNs(b2 + t * 1e9); chk(`imutest 采点 t=${t}s`, api.driftAddPick() === null); }
  api.driftToggleDone();
  api.renderDriftReport();
  chk('第二个 bag 也能出报告', /一、每轴零偏统计/.test(els.driftReport.textContent));
}

console.log(`\n${fail === 0 ? '✓ 全部通过' : '✗ 有失败'} : ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
