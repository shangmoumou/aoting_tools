/**
 * chart.js —— 零依赖 Canvas 2D 轻量绘图引擎
 *
 * 提供 matplotlib 风格的最小可用子集:
 *   Figure  一个 canvas，纵向叠 1~2 个 Axes
 *   Axes    带标题/坐标轴/网格/图例，可放多条曲线或直方图
 *   交互    左键拖框放大 · 双击或按钮复位 · 点击图例显隐 · 悬停读数
 *
 * 只依赖浏览器原生 Canvas 2D，不引任何第三方库，可离线运行。
 */

// ───────── 调色板 & 小工具 ─────────

/**
 * 浅色主题配色。
 * 画布、文字、网格的颜色都从这里取，换主题只改这一处。
 */
const THEME = {
  bg:           '#f7f8fa',   // 画布底色
  plotBg:       '#ffffff',   // 绘图区
  text:         '#1f2328',   // 标题 / 图例文字
  dim:          '#57606a',   // 刻度 / 轴标签
  grid:         'rgba(0,0,0,.07)',
  axis:         'rgba(0,0,0,.28)',
  zero:         'rgba(0,0,0,.30)',
  legendBg:     'rgba(255,255,255,.93)',
  legendBorder: 'rgba(0,0,0,.14)',
  tipBg:        'rgba(255,255,255,.97)',
  tipBorder:    'rgba(0,0,0,.18)',
  sel:          'rgba(0,137,123,.12)',
  selBorder:    '#00897b',
  cursor: '#c62828',
  hover:        'rgba(0,0,0,.35)',
  defaultLine:  '#57606a',
};

/**
 * 全局线宽倍率 —— 由界面上的「线宽」下拉控制。
 * 每条曲线自己有 lw（A3 漂移线就该比别的细），这里只做整体缩放，
 * 所以调完之后各条线之间的粗细关系仍然保留。
 */
let LINE_SCALE = 1;
export function setLineScale(v) { LINE_SCALE = Math.max(0.2, Math.min(6, v)); }
export function getLineScale() { return LINE_SCALE; }

const PALETTE = [
  '#4caf50', '#ff9800', '#42a5f5', '#e91e63', '#9c27b0',
  '#00bcd4', '#cddc39', '#ff5722', '#607d8b', '#8bc34a',
];

const FINE_CANDS = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

export function niceStep(span, target = 5, fine = false) {
  if (!(span > 0) || !isFinite(span)) return 1;
  const raw = span / Math.max(1, target);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  if (fine) {
    // y 轴专用：取对数距离最近的候选，让实际条数尽量贴住 target。
    // 经典档只有 1/2/5/10，n 落在 5~10 之间时会直接跳到 10，
    // 网格一下少一半（600px 高的图只画出 4 条），所以这里补了中间档。
    let best = 1, bd = Infinity;
    for (const c of FINE_CANDS) {
      const d = Math.abs(Math.log(n / c));
      if (d < bd) { bd = d; best = c; }
    }
    return best * mag;
  }
  const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return m * mag;
}

/** 刻度文字：按数量级自动决定小数位 */
export function fmtTick(v, step) {
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1e5 || (a < 1e-3 && a > 0)) return v.toExponential(1);
  let d = 0;
  if (step < 0.001) d = 5; else if (step < 0.01) d = 4;
  else if (step < 0.1) d = 3; else if (step < 1) d = 2;
  else if (step < 10) d = 1;
  else if (step >= 10) d = 0;
  return v.toFixed(d);
}

export function fmtNum(v) {
  if (v == null || !isFinite(v)) return '–';
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e6 || a < 1e-4)) return v.toExponential(3);
  if (Number.isInteger(v)) return String(v);
  return String(+v.toFixed(6));
}

// ───────── Series ─────────

/**
 * 一条曲线 / 一组直方图。
 * xs, ys 为 number[]；kind: 'line' | 'bar'
 * opts: { color, dash:[], marker:'.'|'x'|'s'|'none', alpha, lw, label }
 */
export class Series {
  constructor(xs, ys, opts = {}) {
    this.xs = xs;
    this.ys = ys;
    this.kind = opts.kind || 'line';
    this.color = opts.color || PALETTE[0];
    this.dash = opts.dash || null;
    this.marker = opts.marker || 'none';
    this.alpha = opts.alpha == null ? 1 : opts.alpha;
    this.lw = opts.lw || 1.2;
    this.label = opts.label || '';
    this.visible = opts.visible !== false;
  }
  get n() { return this.xs.length; }
}

// ───────── Axes ─────────

export class Axes {
  constructor(title, xlabel, ylabel, opts = {}) {
    this.title = title || '';
    this.xlabel = xlabel || '';
    this.ylabel = ylabel || '';
    this.series = [];
    this.xlim = null;      // null = 自动
    this.ylim = null;
    this.home = null;      // 复位用的初始范围
    this.yZeroLine = !!opts.yZeroLine;   // 是否画 y=0 参考线
    this.legend = opts.legend !== false;
    this.vlines = [];           // 竖向参考线 [{ v, color, dash, w, alpha }]
    this.rect = { x: 0, y: 0, w: 0, h: 0 };   // 由 Figure 布局时写入
    this._plot = { x: 0, y: 0, w: 0, h: 0 };
    this._legendHits = [];
    this.gridX = opts.gridX !== false;
    this.noCursor = !!opts.noCursor;   // x 轴不是时间（如 dt 直方图）-> 不画游标
  }
  add(xs, ys, opts) { const s = new Series(xs, ys, opts); this.series.push(s); return s; }
  bar(data, opts) { return this.add(null, data, Object.assign({ kind: 'bar' }, opts)); }
  clear() { this.series.length = 0; this.xlim = this.ylim = this.home = null; }

  /** 可见曲线在当前 x 窗口内的 y 范围 */
  yRangeInX(x0, x1) {
    let lo = Infinity, hi = -Infinity, any = false;
    for (const s of this.series) {
      if (!s.visible) continue;
      if (s.kind === 'bar') { for (const v of s.ys) { if (v < lo) lo = v; if (v > hi) hi = v; any = true; } continue; }
      for (let i = 0; i < s.xs.length; i++) {
        const x = s.xs[i];
        if (x < x0 || x > x1) continue;
        const y = s.ys[i];
        if (!isFinite(y)) continue;
        if (y < lo) lo = y;
        if (y > hi) hi = y;
        any = true;
      }
    }
    if (!any) return [0, 1];
    if (lo === hi) { const p = Math.max(1e-9, Math.abs(lo) * 0.1); return [lo - p, hi + p]; }
    const pad = (hi - lo) * 0.06;
    return [lo - pad, hi + pad];
  }
  xRangeAll() {
    let lo = Infinity, hi = -Infinity;
    for (const s of this.series) {
      if (!s.visible || s.kind === 'bar') continue;
      for (const x of s.xs) { if (x < lo) lo = x; if (x > hi) hi = x; }
    }
    if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
    if (lo === hi) return [lo - 1, hi + 1];
    return [lo, hi];
  }
  /** 自动定范围（对 line 图；bar 图的 x 是分箱边界，单独处理） */
  autoScale() {
    const [x0, x1] = this.xRangeAll();
    this.xlim = [x0, x1];
    this.ylim = this.yRangeInX(x0, x1);
    if (!this.home) this.home = { x: this.xlim.slice(), y: this.ylim.slice() };
  }
  /** 直方图：x 由数据范围定，y 从 0 起 */
  autoScaleBar(binEdges) {
    this.xlim = [binEdges[0], binEdges[binEdges.length - 1]];
    let hi = 0;
    for (const s of this.series) if (s.visible && s.kind === 'bar') for (const v of s.ys) if (v > hi) hi = v;
    this.ylim = [0, hi * 1.08 || 1];
    if (!this.home) this.home = { x: this.xlim.slice(), y: this.ylim.slice() };
  }
  reset() { if (this.home) { this.xlim = this.home.x.slice(); this.ylim = this.home.y.slice(); } else this.autoScale(); }
}

// ───────── Figure ─────────

export class Figure {
  constructor(canvas, opts = {}) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.axes = [];
    this.suptitle = opts.suptitle || '';
    this.pad = { l: 70, r: 14, t: opts.suptitle ? 34 : 12, b: 34 };
    this.axisGap = opts.axisGap || 30;
    this.onHover = null;          // (axes, xData) => string[] 自定义读数
    this._sel = null;             // 框选状态
    this._hover = null;
    // ── 联动游标 ──
    // cursorX 是"本地"数据坐标；xBaseNs 是 x=0 对应的绝对时间(ns)。
    // 各图基准不同（A0 用区间起点，A1~A6 用最早消息），所以全局游标存绝对 ns，
    // 绘制时再换算到本地坐标，这样点一张图，其余图同一时刻也能对齐。
    this.cursorX = null;
    this.xBaseNs = 0;
    this.onCursorPick = null;     // (xdLocal, axes) => void
    this.showCursorReadout = true;
    this._bind();
  }
  add(a) { this.axes.push(a); return a; }
  clear() { for (const a of this.axes) a.clear(); }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(320, this.cv.clientWidth || 800);
    const h = Math.max(120, this.cv.clientHeight || 260);
    const cw = Math.round(w * dpr), ch = Math.round(h * dpr);
    if (this.cv.width !== cw) this.cv.width = cw;
    if (this.cv.height !== ch) this.cv.height = ch;
    this.cssW = w; this.cssH = h; this.dpr = dpr;
    return { w, h };
  }

  layout(w, h) {
    const n = this.axes.length;
    if (!n) return;
    const avail = h - this.pad.t - this.pad.b - this.axisGap * (n - 1);
    const ah = Math.max(40, avail / n);
    for (let i = 0; i < n; i++) {
      const y = this.pad.t + i * (ah + this.axisGap);
      this.axes[i].rect = { x: this.pad.l, y, w: w - this.pad.l - this.pad.r, h: ah };
    }
  }

  /**
   * 以绘图区内相对位置 (fx, fy)（0~1）为中心缩放。
   * kx / ky 为缩放系数：<1 放大视野收窄，>1 缩小视野放大。
   * 传 null 表示该轴不缩放（用于 Shift 只缩 X、Alt 只缩 Y）。
   */
  zoomAt(a, kx, ky, fx = 0.5, fy = 0.5) {
    if (!a || !a.xlim || !a.ylim) return;
    const [x0, x1] = a.xlim, [y0, y1] = a.ylim;
    if (kx != null && kx > 0) {
      const sx = x1 - x0;
      const nsx = Math.min(Math.max(sx * kx, 1e-12), (a.home ? (a.home.x[1] - a.home.x[0]) : sx) * 50);
      const xd = x0 + fx * sx;
      a.xlim = [xd - fx * nsx, xd + (1 - fx) * nsx];
    }
    if (ky != null && ky > 0) {
      const sy = y1 - y0;
      const nsy = Math.min(Math.max(sy * ky, 1e-12), (a.home ? (a.home.y[1] - a.home.y[0]) : sy) * 50);
      const yd = y1 - fy * sy;
      a.ylim = [yd - (1 - fy) * nsy, yd + fy * nsy];
    }
  }

  /** 对整个图所有 axes 统一缩放（图头 +/- 按钮用），以中心为锚点 */
  zoomAll(k) { for (const a of this.axes) this.zoomAt(a, k, k); this.draw(); }

  /** 按数据单位平移 */
  panAxes(a, dx, dy) {
    if (!a || !a.xlim) return;
    a.xlim = [a.xlim[0] + dx, a.xlim[1] + dx];
    a.ylim = [a.ylim[0] + dy, a.ylim[1] + dy];
  }

  draw() {
    const { w, h } = this.resize();
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = THEME.bg;
    ctx.fillRect(0, 0, w, h);
    this.layout(w, h);

    if (this.suptitle) {
      ctx.fillStyle = THEME.text;
      ctx.font = '600 12px system-ui, sans-serif';
      ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
      ctx.fillText(this.suptitle, 10, 20);
    }
    for (const a of this.axes) this._drawAxes(ctx, a);
    if (this._sel) this._drawSel(ctx);
    if (this._hover) this._drawHover(ctx, this._hover);
    if (this.cursorX != null) this._drawCursor(ctx);
  }

  _drawAxes(ctx, a) {
    const R = a.rect;
    if (R.w < 30 || R.h < 30) return;
    if (!a.xlim || !a.ylim) a.autoScale();
    const [x0, x1] = a.xlim, [y0, y1] = a.ylim;
    const px = R.x, py = R.y + 22, pw = R.w, ph = Math.max(10, R.h - 22 - 26);
    a._plot = { x: px, y: py, w: pw, h: ph };

    // 标题
    ctx.fillStyle = THEME.text;
    ctx.font = '11px system-ui, sans-serif';
    ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
    if (a.title) ctx.fillText(a.title, R.x, R.y + 13);

    const X = v => px + (v - x0) / (x1 - x0 || 1) * pw;
    const Y = v => py + ph - (v - y0) / (y1 - y0 || 1) * ph;
    a._X = X; a._Y = Y;

    // 绘图区底
    ctx.fillStyle = THEME.plotBg;
    ctx.fillRect(px, py, pw, ph);

    // 网格
    const xStep = niceStep(x1 - x0, Math.max(4, Math.round(pw / 130)));
    // y 刻度按像素高度给：图变高后多给几条，免得网格空荡
    const yStep = niceStep(y1 - y0, Math.max(4, Math.min(14, Math.round(ph / 62))), true);
    ctx.lineWidth = 1;
    ctx.strokeStyle = THEME.grid;
    ctx.beginPath();
    for (let v = Math.ceil(y0 / yStep) * yStep; v <= y1; v += yStep) {
      const y = Math.round(Y(v)) + .5;
      ctx.moveTo(px, y); ctx.lineTo(px + pw, y);
    }
    if (a.gridX) {
      for (let v = Math.ceil(x0 / xStep) * xStep; v <= x1; v += xStep) {
        const x = Math.round(X(v)) + .5;
        ctx.moveTo(x, py); ctx.lineTo(x, py + ph);
      }
    }
    ctx.stroke();

    // y=0 参考线
    if (a.yZeroLine && y0 < 0 && y1 > 0) {
      const y = Math.round(Y(0)) + .5;
      ctx.strokeStyle = THEME.zero;
      ctx.beginPath(); ctx.moveTo(px, y); ctx.lineTo(px + pw, y); ctx.stroke();
    }

    // 刻度文字
    ctx.fillStyle = THEME.dim;
    ctx.font = '10px system-ui, sans-serif';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let v = Math.ceil(y0 / yStep) * yStep; v <= y1; v += yStep) {
      ctx.fillText(fmtTick(v, yStep), px - 6, Y(v));
    }
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (let v = Math.ceil(x0 / xStep) * xStep; v <= x1; v += xStep) {
      ctx.fillText(fmtTick(v, xStep), X(v), py + ph + 5);
    }

    // 轴标签
    ctx.fillStyle = THEME.dim;
    ctx.font = '10px system-ui, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    if (a.xlabel) ctx.fillText(a.xlabel, px + pw / 2, py + ph + 30);
    if (a.ylabel) {
      ctx.save();
      ctx.translate(px - 52, py + ph / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
      ctx.fillText(a.ylabel, 0, 0);
      ctx.restore();
    }

    // 数据（裁剪到绘图区）
    ctx.save();
    ctx.beginPath(); ctx.rect(px, py, pw, ph); ctx.clip();
    for (const s of a.series) {
      if (!s.visible) continue;
      if (s.kind === 'bar') this._drawBar(ctx, a, s, X, Y, px, py, pw, ph);
      else this._drawLine(ctx, a, s, X, Y, px, py, pw, ph);
    }
    // 竖向参考线（P5 / 中位数 / 均值 / RMS 等）
    if (a.vlines && a.vlines.length) {
      ctx.save();
      ctx.beginPath(); ctx.rect(px, py, pw, ph); ctx.clip();
      for (const v of a.vlines) {
        const x = X(v.v);
        if (x < px - 1 || x > px + pw + 1) continue;
        ctx.globalAlpha = v.alpha == null ? 1 : v.alpha;
        ctx.strokeStyle = v.color || THEME.defaultLine;
        ctx.lineWidth = v.w || 1;
        if (v.dash) ctx.setLineDash(v.dash);
        ctx.beginPath(); ctx.moveTo(x, py); ctx.lineTo(x, py + ph); ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.restore();
      ctx.globalAlpha = 1;
    }
    ctx.restore();

    // 边框
    ctx.strokeStyle = THEME.axis;
    ctx.lineWidth = 1;
    ctx.strokeRect(px + .5, py + .5, pw - 1, ph - 1);

    if (a.legend) this._drawLegend(ctx, a, px, py, pw);
  }

  _drawLine(ctx, a, s, X, Y, px, py, pw, ph) {
    const n = s.xs.length;
    if (!n) return;
    ctx.save();
    ctx.globalAlpha = s.alpha;
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.lw * LINE_SCALE;
    if (s.dash) ctx.setLineDash(s.dash);
    ctx.beginPath();
    let started = false, prevUp = null;
    for (let i = 0; i < n; i++) {
      const x = s.xs[i], y = s.ys[i];
      if (!isFinite(y)) { started = false; continue; }
      const sx = X(x), sy = Y(y);
      if (!started) { ctx.moveTo(sx, sy); started = true; }
      else ctx.lineTo(sx, sy);
      prevUp = sy;
    }
    ctx.stroke();
    ctx.setLineDash([]);

    // marker：点太多时不画，否则既慢又糊成一团
    if (s.marker && s.marker !== 'none' && n <= 2500) {
      ctx.fillStyle = s.color;
      const mk = 0.65 + 0.35 * LINE_SCALE;      // 线细时点也小一点
      const r = (s.marker === '.' ? 1.7 : 2.2) * mk;
      for (let i = 0; i < n; i++) {
        const y = s.ys[i];
        if (!isFinite(y)) continue;
        const sx = X(s.xs[i]), sy = Y(y);
        if (sx < px - 4 || sx > px + pw + 4) continue;
        if (s.marker === 'x') {
          ctx.strokeStyle = s.color; ctx.lineWidth = Math.max(0.6, s.lw * LINE_SCALE);
          ctx.beginPath();
          ctx.moveTo(sx - r, sy - r); ctx.lineTo(sx + r, sy + r);
          ctx.moveTo(sx + r, sy - r); ctx.lineTo(sx - r, sy + r);
          ctx.stroke();
        } else if (s.marker === 's') {
          ctx.fillRect(sx - r, sy - r, r * 2, r * 2);
        } else {
          ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2); ctx.fill();
        }
      }
    }
    ctx.restore();
  }

  /** 直方图：xs 为 null，用 ys + this._bins 画 */
  _drawBar(ctx, a, s, X, Y, px, py, pw, ph) {
    const edges = s._edges;
    const counts = s.ys;
    if (!edges || !counts.length) return;
    ctx.save();
    ctx.fillStyle = s.color;
    ctx.globalAlpha = s.alpha;
    for (let i = 0; i < counts.length; i++) {
      const xa = X(edges[i]), xb = X(edges[i + 1]);
      const wpx = Math.max(0.6, xb - xa);
      const yv = Y(counts[i]);
      const top = Math.min(yv, Y(0));
      const hh = Math.abs(Y(0) - yv);
      if (hh <= 0) continue;
      ctx.fillRect(xa, top, wpx, hh);
    }
    ctx.globalAlpha = 1;
    if (s.edgeColor) {
      ctx.strokeStyle = s.edgeColor; ctx.lineWidth = 0.4;
      for (let i = 0; i < counts.length; i++) {
        const xa = X(edges[i]), xb = X(edges[i + 1]);
        const yv = Y(counts[i]);
        ctx.strokeRect(xa, Math.min(yv, Y(0)), Math.max(0.6, xb - xa), Math.abs(Y(0) - yv));
      }
    }
    ctx.restore();
  }

  _drawLegend(ctx, a, px, py, pw) {
    const items = a.series.filter(s => s.label);
    if (!items.length) return;
    ctx.font = '10px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    const lh = 13, swW = 14;
    let bw = 0;
    for (const s of items) bw = Math.max(bw, ctx.measureText(s.label).width);
    bw += swW + 12;
    const bh = items.length * lh + 6;
    const bx = Math.min(px + pw - bw - 6, px + pw - bw - 6);
    const by = py + 4;
    a._legendHits = [];
    ctx.fillStyle = THEME.legendBg;
    ctx.strokeStyle = THEME.legendBorder; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.rect(bx, by, bw, bh); ctx.fill(); ctx.stroke();
    items.forEach((s, i) => {
      const yy = by + 3 + i * lh + lh / 2;
      a._legendHits.push({ x: bx, y: by + 3 + i * lh, w: bw, h: lh, s });
      ctx.globalAlpha = s.visible ? 1 : 0.35;
      ctx.strokeStyle = s.color; ctx.lineWidth = Math.min(7, 1.6 * LINE_SCALE);
      if (s.dash) ctx.setLineDash(s.dash);
      ctx.beginPath(); ctx.moveTo(bx + 5, yy); ctx.lineTo(bx + 5 + swW, yy); ctx.stroke();
      ctx.setLineDash([]);
      if (s.kind === 'bar') { ctx.fillStyle = s.color; ctx.fillRect(bx + 5, yy - 4, swW, 8); }
      ctx.fillStyle = THEME.text;
      ctx.textAlign = 'left';
      ctx.fillText(s.label, bx + 5 + swW + 5, yy);
      ctx.globalAlpha = 1;
    });
  }

  _drawSel(ctx) {
    const s = this._sel;
    ctx.save();
    ctx.fillStyle = THEME.sel;
    ctx.strokeStyle = THEME.selBorder; ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    const x = Math.min(s.x0, s.x1), y = Math.min(s.y0, s.y1);
    const w = Math.abs(s.x1 - s.x0), h = Math.abs(s.y1 - s.y0);
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x + .5, y + .5, w, h);
    ctx.restore();
  }

  /** 某时刻各曲线的取值。用阶梯保持（取该时刻之前最近的点），
   *  对状态量才是对的 —— 不能取到"之后"的点。 */
  readAt(a, xd) {
    const out = [];
    for (const s of a.series) {
      if (!s.visible || s.kind === 'bar' || !s.xs || !s.xs.length) continue;
      const xs = s.xs;
      let lo = 0, hi = xs.length - 1, idx = -1;
      if (xd >= xs[0]) {
        while (lo <= hi) {
          const m = (lo + hi) >> 1;
          if (xs[m] <= xd) { idx = m; lo = m + 1; } else hi = m - 1;
        }
      }
      if (idx < 0) continue;                 // 游标在所有点之前 -> 还没数据
      const v = s.ys[idx];
      if (v == null || !isFinite(v)) continue;
      out.push({ color: s.color, label: s.label, v });
    }
    return out;
  }

  /** 固定游标：红色实线（区别于 hover 的虚线）+ 该图各曲线的同时刻读数 */
  _drawCursor(ctx) {
    for (const a of this.axes) {
      if (!a._X || !a.xlim || a.noCursor) continue;
      const P = a._plot;
      if (!P) continue;
      const sx = a._X(this.cursorX);
      if (!(sx >= P.x - .5) || !(sx <= P.x + P.w + .5)) continue;
      ctx.save();
      ctx.strokeStyle = THEME.cursor || '#c62828';
      ctx.lineWidth = 1.3;
      ctx.beginPath(); ctx.moveTo(sx, P.y); ctx.lineTo(sx, P.y + P.h); ctx.stroke();
      // 顶部小三角，标记游标位置
      ctx.fillStyle = THEME.cursor || '#c62828';
      ctx.beginPath();
      ctx.moveTo(sx, P.y); ctx.lineTo(sx - 4, P.y - 5); ctx.lineTo(sx + 4, P.y - 5);
      ctx.closePath(); ctx.fill();
      if (this.showCursorReadout) {
        const items = this.readAt(a, this.cursorX);
        if (items.length) {
          ctx.font = '10px system-ui, sans-serif';
          let bw = 0;
          for (const it of items) bw = Math.max(bw, ctx.measureText(`${it.label} = ${fmtNum(it.v)}`).width);
          bw += 22;
          const bh = items.length * 13 + 8;
          let bx = sx + 8;
          if (bx + bw > P.x + P.w) bx = sx - bw - 8;
          if (bx < P.x) bx = P.x + 2;
          // 读数框放底部，避开顶部的图例
          let by = P.y + P.h - bh - 3;
          if (by < P.y + 2) by = P.y + 2;
          ctx.fillStyle = THEME.tipBg;
          ctx.strokeStyle = THEME.cursor || '#c62828'; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.rect(bx, by, bw, bh); ctx.fill(); ctx.stroke();
          items.forEach((it, i) => {
            const yy = by + 4 + i * 13 + 6;
            ctx.fillStyle = it.color;
            ctx.fillRect(bx + 5, yy - 4, 8, 3);
            ctx.fillStyle = THEME.text; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
            ctx.fillText(`${it.label} = ${fmtNum(it.v)}`, bx + 17, yy);
          });
        }
      }
      ctx.restore();
    }
  }

  _drawHover(ctx, hv) {
    const a = hv.axes;
    if (!a || !a._X) return;
    const P = a._plot;
    const sx = a._X(hv.xd);
    if (sx < P.x || sx > P.x + P.w) return;
    ctx.save();
    ctx.strokeStyle = THEME.hover; ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(sx, P.y); ctx.lineTo(sx, P.y + P.h); ctx.stroke();
    ctx.setLineDash([]);
    const lines = hv.lines || [];
    if (lines.length) {
      ctx.font = '10px system-ui, sans-serif';
      let bw = 0;
      for (const l of lines) bw = Math.max(bw, ctx.measureText(l.text).width);
      bw += 22;
      const bh = lines.length * 13 + 8;
      let bx = sx + 8;
      if (bx + bw > P.x + P.w) bx = sx - bw - 8;
      if (bx < P.x) bx = P.x + 2;
      const by = P.y + 3;
      ctx.fillStyle = THEME.tipBg;
      ctx.strokeStyle = THEME.tipBorder; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.rect(bx, by, bw, bh); ctx.fill(); ctx.stroke();
      lines.forEach((l, i) => {
        const yy = by + 4 + i * 13 + 6;
        ctx.fillStyle = l.color;
        ctx.fillRect(bx + 5, yy - 4, 8, 3);
        ctx.fillStyle = THEME.text; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        ctx.fillText(l.text, bx + 17, yy);
      });
    }
    ctx.restore();
  }

  // ───── 交互 ─────
  _axesAt(cssX, cssY) {
    for (const a of this.axes) {
      const R = a.rect;
      if (cssX >= R.x && cssX <= R.x + R.w && cssY >= R.y && cssY <= R.y + R.h) return a;
    }
    return null;
  }
  _bind() {
    const cv = this.cv;
    const pos = e => {
      const r = cv.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    const toData = (a, p) => {
      const P = a._plot;
      if (!P || P.w <= 0) return null;
      const xd = a.xlim[0] + (p.x - P.x) / P.w * (a.xlim[1] - a.xlim[0]);
      return xd;
    };

    cv.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      const p = pos(e);
      const a = this._axesAt(p.x, p.y);
      if (!a) return;
      // 图例命中 -> 切换显隐
      for (const h of (a._legendHits || [])) {
        if (p.x >= h.x && p.x <= h.x + h.w && p.y >= h.y && p.y <= h.y + h.h) {
          h.s.visible = !h.s.visible;
          a.autoScaleBar ? a.autoScaleBar(a.series.find(s => s._edges)?._edges) : null;
          this.draw();
          return;
        }
      }
      const P = a._plot;
      if (p.x < P.x || p.x > P.x + P.w || p.y < P.y || p.y > P.y + P.h) return;
      this._sel = { a, x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      cv.setPointerCapture(e.pointerId);
    });

    cv.addEventListener('pointermove', e => {
      const p = pos(e);
      if (this._sel) { this._sel.x1 = p.x; this._sel.y1 = p.y; this.draw(); return; }
      const a = this._axesAt(p.x, p.y);
      if (!a || !a.xlim) { if (this._hover) { this._hover = null; this.draw(); } return; }
      const P = a._plot;
      if (p.x < P.x || p.x > P.x + P.w) { if (this._hover) { this._hover = null; this.draw(); } return; }
      const xd = toData(a, p);
      this._hover = { axes: a, xd, lines: this._hoverLines(a, xd) };
      this.draw();
    });

    const endSel = e => {
      if (!this._sel) return;
      const s = this._sel; this._sel = null;
      const a = s.a, P = a._plot;
      const dx = Math.abs(s.x1 - s.x0), dy = Math.abs(s.y1 - s.y0);
      if (dx < 4 || dy < 4) {                          // 太小题 -> 当点击
        this.draw();
        if (this.onCursorPick) this.onCursorPick(toData(a, { x: s.x0, y: s.y0 }), a);
        return;
      }
      const xa = Math.min(s.x0, s.x1), xb = Math.max(s.x0, s.x1);
      const ya = Math.min(s.y0, s.y1), yb = Math.max(s.y0, s.y1);
      const nx0 = a.xlim[0] + (xa - P.x) / P.w * (a.xlim[1] - a.xlim[0]);
      const nx1 = a.xlim[0] + (xb - P.x) / P.w * (a.xlim[1] - a.xlim[0]);
      const ny1 = a.ylim[1] - (ya - P.y) / P.h * (a.ylim[1] - a.ylim[0]);
      const ny0 = a.ylim[1] - (yb - P.y) / P.h * (a.ylim[1] - a.ylim[0]);
      if (nx1 > nx0) a.xlim = [nx0, nx1];
      if (ny1 > ny0) a.ylim = [ny0, ny1];
      this.draw();
    };
    // ── 滚轮缩放 ──
    // 普通滚轮: X/Y 同时缩放；Shift: 只缩 X；Alt: 只缩 Y；Ctrl: 平移 Y
    cv.addEventListener('wheel', e => {
      const pp = pos(e);
      const ax = this._axesAt(pp.x, pp.y);
      if (!ax || !ax.xlim) return;
      const P = ax._plot;
      if (!P || pp.x < P.x || pp.x > P.x + P.w || pp.y < P.y || pp.y > P.y + P.h) return;
      e.preventDefault();
      const fx = (pp.x - P.x) / P.w, fy = (pp.y - P.y) / P.h;
      const k = e.deltaY < 0 ? 1 / 1.25 : 1.25;      // 向前滚 = 放大
      if (e.ctrlKey || e.metaKey) {
        this.panAxes(ax, 0, (e.deltaY < 0 ? 1 : -1) * (ax.ylim[1] - ax.ylim[0]) * 0.06);
      } else if (e.shiftKey) {
        this.zoomAt(ax, k, null, fx, fy);
      } else if (e.altKey) {
        this.zoomAt(ax, null, k, fx, fy);
      } else {
        this.zoomAt(ax, k, k, fx, fy);
      }
      this.draw();
    }, { passive: false });

    // ── 右键 / 中键拖动平移 ──
    let pan = null;
    cv.addEventListener('pointerdown', e => {
      if (e.button !== 2 && e.button !== 1) return;
      const pp = pos(e);
      const ax = this._axesAt(pp.x, pp.y);
      if (!ax) return;
      e.preventDefault();
      pan = { ax, px: pp.x, py: pp.y };
      cv.setPointerCapture(e.pointerId);
    });
    cv.addEventListener('pointermove', e => {
      if (!pan) return;
      const pp = pos(e);
      const P = pan.ax._plot;
      if (!P) return;
      const dx = -(pp.x - pan.px) / P.w * (pan.ax.xlim[1] - pan.ax.xlim[0]);
      const dy = (pp.y - pan.py) / P.h * (pan.ax.ylim[1] - pan.ax.ylim[0]);
      this.panAxes(pan.ax, dx, dy);
      pan.px = pp.x; pan.py = pp.y;
      this.draw();
    });
    const endPan = () => { pan = null; };
    cv.addEventListener('pointerup', endPan);
    cv.addEventListener('pointercancel', endPan);
    cv.addEventListener('contextmenu', e => e.preventDefault());

    cv.addEventListener('pointerup', endSel);
    cv.addEventListener('pointercancel', endSel);
    cv.addEventListener('pointerleave', () => { if (this._hover) { this._hover = null; this.draw(); } });
    cv.addEventListener('dblclick', () => { for (const a of this.axes) a.reset(); this.draw(); });
  }

  _hoverLines(a, xd) {
    const out = [];
    for (const s of a.series) {
      if (!s.visible || s.kind === 'bar' || !s.xs || !s.xs.length) continue;
      // 找最近的 x
      const xs = s.xs;
      let lo = 0, hi = xs.length - 1;
      if (xd <= xs[0]) lo = 0;
      else if (xd >= xs[hi]) lo = hi;
      else {
        while (lo < hi) { const m = (lo + hi) >> 1; if (xs[m] < xd) lo = m + 1; else hi = m; }
        if (lo > 0 && Math.abs(xs[lo - 1] - xd) < Math.abs(xs[lo] - xd)) lo--;
      }
      const v = s.ys[lo];
      if (v == null || !isFinite(v)) continue;
      out.push({ color: s.color, text: `${s.label} = ${fmtNum(v)}` });
    }
    return out;
  }
}

// ───────── 直方图分箱 ─────────

/**
 * 返回 { counts, edges }
 * 分箱数按 Python 版的经验公式：min(120, max(20, n//8))
 */
export function histogram(data, bins) {
  const v = Array.from(data).filter(x => isFinite(x));
  const n = v.length;
  if (!n) return { counts: [], edges: [0, 1] };
  let lo = Infinity, hi = -Infinity;
  for (const x of v) { if (x < lo) lo = x; if (x > hi) hi = x; }
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  const nb = bins || Math.min(120, Math.max(20, Math.floor(n / 8)));
  const step = (hi - lo) / nb;
  const counts = new Array(nb).fill(0);
  const edges = new Array(nb + 1);
  for (let i = 0; i <= nb; i++) edges[i] = lo + i * step;
  for (const x of v) {
    let k = Math.floor((x - lo) / step);
    if (k >= nb) k = nb - 1;
    if (k < 0) k = 0;
    counts[k]++;
  }
  return { counts, edges };
}
