/**
 * app.js —— IMU / Odometry / TrackedPose 对比 + 话题频率诊断（网页版）
 *
 * 是从同名 Python 脚本移植过来的，算法与图的定义一一对应:
 *   A1 角速度 z 直接测量   A2 三路 yaw 对比      A3 odom-imu yaw 漂移
 *   A4 帧间差分角速度      A5 话题频率诊断       A6 帧间 yaw 增量(度)
 *
 * 数据全程在本机解析，不上传。
 */

import { Bag, FileSource } from './bag.js';
import { Figure, Axes, histogram, fmtNum, setLineScale, getLineScale, niceStep } from './chart.js';

const APP_VERSION = '1.4';

const RAD_TO_DEG = 180 / Math.PI;
const WRAP_LIMIT = 180.0;      // wrap 到 (-180, 180]
const NEAR_LIMIT = 170.0;      // 接近极限 -> 可能超出可解析范围

// 浅底配色：比深色主题下压暗一档，保证在白底上仍然清楚
const SRC_COLORS = { imu: '#2e7d32', odom: '#e65100', tracked_pose: '#1565c0' };
const SRC_DASH = { imu: null, odom: [7, 4], tracked_pose: [3, 3] };
const SRC_MARKER = { imu: '.', odom: 'x', tracked_pose: 's' };

const FIG_META = {
  0: ['A0', 'Custom Fields - pick numeric fields from any topic'],
  1: ['A1', 'Angular Velocity (z) - direct measurement'],
  2: ['A2', 'Yaw Comparison - 3 sources'],
  3: ['A3', 'Yaw Difference - odom minus imu'],
  4: ['A4', 'Frame-to-Frame Yaw Step (dYaw/dt)'],
  5: ['A5', 'Topic Frequency Diagnostics'],
  6: ['A6', 'Frame-to-Frame Yaw Delta (deg, no dt)'],
};

// ═══════════════ 状态 ═══════════════
const S = {
  bag: null,
  fileName: '',
  dur: 0,                 // bag 总时长(秒)
  topics: [],             // 全部话题名
  types: new Map(),       // topic -> type
  imuTopic: '/imu',
  odomTopic: null,
  trackedTopic: null,
  range: [0, 0],          // 选中区间(秒, 相对 bag 开始)
  data: null,             // { imu:{t,wz,yaw}, odom:{...}, tracked:{...} }
  figs: [],               // Figure[]
  reports: {},
  cursorNs: null,         // 联动游标的绝对时间(ns)；null = 未设
  // A0 自定义曲线
  a0: {
    picks: [],            // [{topic, field}] 用户选的字段，可跨话题
    // 默认【关】：Y 轴直接画原始值。
    // 这里必须与 index.html 里 normChk 的 checked 一致 —— 之前 JS 写 true、
    // HTML 里没勾，结果按钮显示"未勾选"但画出来是 0~1，看着像数值被改过。
    // init() 里还会再以 checkbox 为准同步一次，保证单一事实来源。
    norm: false,
  },
  // A9 零漂分析（静止采样）
  drift: {
    sel: { x: null, y: null, z: null },   // {topic, field} —— 三轴可来自不同话题
    groups: [],        // 自动识别出的陀螺三轴组
    win: 1.0,          // 采样窗口（秒），对游标前后各 win/2 内的消息取平均
    picks: [],         // [{tNs, x,y,z (原始 rad/s), nx,ny,nz (样本数), sdx..}]
    closed: false,     // 是否已"结束选择"
  },
};

const $ = id => document.getElementById(id);

// ═══════════════ 统计小工具 ═══════════════

/** 线性插值分位数，与 numpy.percentile 默认行为一致 */
function percentile(arr, p) {
  const v = Array.from(arr).filter(isFinite).sort((a, b) => a - b);
  const n = v.length;
  if (!n) return NaN;
  if (n === 1) return v[0];
  const k = (n - 1) * (p / 100);
  const f = Math.floor(k), c = Math.ceil(k);
  return f === c ? v[f] : v[f] + (v[c] - v[f]) * (k - f);
}
const mean = a => { let s = 0; for (const v of a) s += v; return a.length ? s / a.length : NaN; };
const std = a => { if (a.length < 2) return 0; const m = mean(a); let s = 0; for (const v of a) s += (v - m) ** 2; return Math.sqrt(s / a.length); };
const median = a => percentile(a, 50);

/**
 * 四元数 -> yaw（与 Python quaternion_to_yaw 一致）
 */
function quatToYaw(q) {
  if (!q) return 0;
  const x = q.x || 0, y = q.y || 0, z = q.z || 0, w = q.w == null ? 1 : q.w;
  return Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z));
}

/**
 * 解缠绕：把 atan2 出来的 (-π,π] 接成连续曲线，并把起点平移到 0。
 * 与 Python unwrap_yaw 完全一致。
 */
function unwrapYaw(yaws) {
  const out = [];
  if (!yaws.length) return out;
  let offset = 0, prev = yaws[0];
  const start = yaws[0];
  for (const y of yaws) {
    const d = y - prev;
    if (d > Math.PI) offset -= 2 * Math.PI;
    else if (d < -Math.PI) offset += 2 * Math.PI;
    out.push(y + offset - start);
    prev = y;
  }
  return out;
}

/** 角度 wrap 到 (-limit, limit] */
function wrapDeg(d, limit = WRAP_LIMIT) {
  return ((d + limit) % (2 * limit) + 2 * limit) % (2 * limit) - limit;
}

/**
 * 话题频率诊断 —— 逐行对应 Python analyze_topic_frequency
 *
 * 为什么不能只看平均频率:
 *   平均频率 = 总数/总时长，会被丢包拉低。标称 10Hz 丢一半 -> 平均 5Hz，
 *   与"本来就是 5Hz"在平均值上完全一样。必须看 dt 的分布结构。
 *
 * 两个基准:
 *   median_dt -> 最常见的间隔，反映"实际拿到的频率"
 *   p5_dt     -> 最快的间隔，反映"驱动真实发布周期"的上界
 *
 * 判据: 看 dt 是否聚集在 p5 的整数倍上（离散多峰 = 均匀丢帧），
 *       是则用 1/p5 推断标称频率与丢包率；否则是连续抖动，诚实标注不推断。
 */
function analyzeTopicFrequency(times, name) {
  const r = { name, ok: false, count: times.length };
  const n = times.length;
  if (n < 3) return r;

  const dtAll = [];
  for (let i = 1; i < n; i++) dtAll.push(times[i] - times[i - 1]);
  const duration = times[n - 1] - times[0];

  // 时间戳重复或乱序 -> 本身就是严重异常，单独统计
  const dtGood = dtAll.filter(d => d > 0);
  const nBad = dtAll.length - dtGood.length;
  if (dtGood.length < 2) return r;

  const meanDt = mean(dtGood);
  const medianDt = median(dtGood);
  const stdDt = std(dtGood);
  const minDt = Math.min(...dtGood);
  const maxDt = Math.max(...dtGood);
  const p5 = percentile(dtGood, 5);
  const p95 = percentile(dtGood, 95);
  const p99 = percentile(dtGood, 99);

  const avgFreq = duration > 0 ? (n - 1) / duration : NaN;
  const medianFreq = medianDt > 0 ? 1 / medianDt : NaN;
  const fastFreq = p5 > 0 ? 1 / p5 : NaN;

  const cv = meanDt > 0 ? stdDt / meanDt : NaN;

  // 整数倍检测
  let multRatio = NaN;
  if (p5 > 0) {
    let hit = 0;
    for (const d of dtGood) {
      const ratio = d / p5;
      if (Math.abs(ratio - Math.round(ratio)) < 0.25) hit++;
    }
    multRatio = hit / dtGood.length;
  }

  const thr = 1.5 * medianDt;
  const nLate = dtGood.filter(d => d > thr).length;
  const lateRatio = nLate / dtGood.length;

  // 关键判据：离散多峰 vs 连续抖动（不能用 CV 判断，丢包本身会抬高 CV）
  const discrete = multRatio > 0.85;

  let loss = 0, nominalFreq = medianFreq, dropDetected = false;
  if (discrete && fastFreq > 0 && avgFreq > 0) {
    loss = Math.max(0, Math.min(1, 1 - avgFreq / fastFreq));
    nominalFreq = fastFreq;
    dropDetected = loss > 0.10;
  }

  let grade;
  if (cv < 0.05) grade = 'VERY STABLE';
  else if (cv < 0.15) grade = 'STABLE';
  else if (cv < 0.30) grade = 'MODERATE';
  else grade = 'UNSTABLE';

  let verdict;
  if (nBad > 0) {
    verdict = `timestamp anomaly: ${nBad} duplicated / out-of-order stamps`;
  } else if (!discrete) {
    verdict = `jittery: ${avgFreq.toFixed(2)} Hz, irregular spacing (CV=${cv.toFixed(3)}). `
      + 'Gaps form a continuous spread, not fixed-period drops, so driver rate cannot be inferred.';
  } else if (dropDetected) {
    verdict = `periodic packet drop: driver runs at ~${nominalFreq.toFixed(1)} Hz but ~${(loss * 100).toFixed(0)}% `
      + 'messages lost (gaps cluster at integer multiples of the period)';
  } else {
    verdict = `healthy: steady ${avgFreq.toFixed(2)} Hz, no significant drop (CV=${cv.toFixed(3)})`;
  }

  Object.assign(r, {
    ok: true, duration, avgFreq, medianFreq, nominalFreq, fastFreq, loss, dropDetected, discrete,
    meanDt, medianDt, stdDt, minDt, maxDt, p5, p95, p99, cv, grade,
    nLate, lateRatio, nBad, multRatio, verdict, dt: dtGood,
  });
  return r;
}

/**
 * 帧间 yaw 增量（度，不除 dt）
 * 与 A4 的区别: A4 = d_yaw/dt -> deg/s；A6 = d_yaw -> deg，与采样率无关
 */
function frameYawDelta(yawsDeg) {
  const d = [];
  for (let i = 1; i < yawsDeg.length; i++) d.push(wrapDeg(yawsDeg[i] - yawsDeg[i - 1]));
  return d;
}

function analyzeYawDelta(times, yawsDeg, name) {
  const d = frameYawDelta(yawsDeg);
  if (!d.length) return null;
  const absD = d.map(Math.abs);
  const sq = d.map(v => v * v);
  let net = 0; for (const v of d) net += v;
  let sumAbs = 0; for (const v of absD) sumAbs += v;
  const midT = [];
  for (let i = 1; i < times.length; i++) midT.push((times[i - 1] + times[i]) / 2);
  return {
    name, count: d.length,
    mean: mean(d), std: std(d), rms: Math.sqrt(mean(sq)),
    max: Math.max(...d), min: Math.min(...d),
    maxAbs: Math.max(...absD),
    p95Abs: percentile(absD, 95), p99Abs: percentile(absD, 99),
    sumAbs, net,
    nOver: absD.filter(v => v > NEAR_LIMIT).length,
    d, midT,
  };
}

/** 帧间角速度 deg/s（A4） */
function frameYawRate(times, yawsDeg) {
  const midT = [], rate = [];
  for (let i = 1; i < times.length; i++) {
    const dt = times[i] - times[i - 1];
    const dy = wrapDeg(yawsDeg[i] - yawsDeg[i - 1]);
    midT.push((times[i - 1] + times[i]) / 2);
    rate.push(dt > 0 ? dy / dt : NaN);
  }
  return { midT, rate };
}

/** 线性插值，与 np.interp 一致（xp 需递增） */
function interp(x, xp, fp) {
  const n = xp.length;
  if (!n) return NaN;
  if (x <= xp[0]) return fp[0];
  if (x >= xp[n - 1]) return fp[n - 1];
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xp[m] <= x) lo = m; else hi = m; }
  const span = xp[hi] - xp[lo];
  if (span <= 0) return fp[lo];
  return fp[lo] + (fp[hi] - fp[lo]) * (x - xp[lo]) / span;
}

// ═══════════════ 加载 bag ═══════════════

async function openFile(file) {
  setStatus('读取中…', '');
  try {
    const bag = new Bag(new FileSource(file));
    await bag.open();
    await bag.loadAll();
    if (!bag.entries.length) throw new Error('bag 里没有消息');

    S.bag = bag;
    S.fileName = file.name;
    S.dur = (bag.endTime - bag.startTime) / 1e9;
    S.topics = bag.topics();
    S.types = new Map();
    for (const c of bag.connections.values()) S.types.set(c.topic, c.type || '');

    detectTopics();
    S.range = [0, S.dur];
  S.a0.picks = [];
  S.a0Wrap = null;
  clearA0Cache();
  S.drift.picks = [];
  S.drift.closed = false;
  S.drift.groups = [];
  S.drift.sel = { x: null, y: null, z: null };
    buildTopicSelectors();
    buildRawTopicSelector();
    buildDriftGroups();
    $('startIn').value = '0';
    $('endIn').value = S.dur.toFixed(1);
    drawRangeBar();
    setStatus(`${file.name} · ${bag.entries.length} 条消息 · ${S.topics.length} 个话题 · 压缩:${bag.compression}`, '');
    // 载入成功后才放开交互控件（初始是 disabled 的）
    for (const id of ['imuSel', 'odomSel', 'trackedSel', 'startIn', 'endIn',
                      'btnApply', 'btnAll', 'btnResetAll', 'lwSel',
                      'btnFields', 'normChk',
                      'rawTopicSel', 'rawPrev', 'rawNext', 'rawCopy', 'cmpCopy',
                      'driftGroup', 'driftWin', 'driftSelX', 'driftSelY', 'driftSelZ',
                      'driftPick', 'driftUndo', 'driftClear', 'driftDone', 'driftCsv']) $(id).disabled = false;
    runAnalysis();
  } catch (e) {
    setStatus('失败: ' + e.message, 'err');
    console.error(e);
  }
}

/** 自动挑话题：imu 固定 /imu；odom 在 /odom、/fused_odom 里挑；tracked 选带 tracked_pose 的 */
function detectTopics() {
  const has = t => S.topics.includes(t);
  S.imuTopic = has('/imu') ? '/imu' : (S.topics.find(t => (S.types.get(t) || '').includes('Imu')) || null);
  const odomCands = ['/odom', '/fused_odom'];
  S.odomTopic = odomCands.find(has)
    || S.topics.find(t => (S.types.get(t) || '') === 'nav_msgs/msg/Odometry'
                       || (S.types.get(t) || '') === 'nav_msgs/Odometry')
    || null;
  S.trackedTopic = S.topics.find(t => t.includes('tracked_pose'))
    || S.topics.find(t => (S.types.get(t) || '').includes('PoseWithCovarianceStamped'))
    || null;
}

function buildTopicSelectors() {
  const mk = (sel, cur, filter, onPick) => {
    sel.innerHTML = '';
    const list = S.topics.filter(filter);
    if (!list.includes(cur) && cur) list.unshift(cur);
    const off = document.createElement('option');
    off.value = ''; off.textContent = '（不使用）';
    sel.appendChild(off);
    for (const t of list) {
      const o = document.createElement('option');
      o.value = t;
      const c = S.bag.byTopic.get(t);
      o.textContent = `${t}  (${c ? c.length : 0})`;
      sel.appendChild(o);
    }
    sel.value = cur || '';
    sel.onchange = () => onPick(sel.value || null);
  };
  mk($('odomSel'), S.odomTopic, t => (S.types.get(t) || '').includes('Odometry'), v => { S.odomTopic = v; runAnalysis(); });
  mk($('trackedSel'), S.trackedTopic, t => t.includes('tracked_pose') || (S.types.get(t) || '').includes('PoseWithCovariance'), v => { S.trackedTopic = v; runAnalysis(); });
  mk($('imuSel'), S.imuTopic, t => (S.types.get(t) || '').includes('Imu'), v => { S.imuTopic = v; runAnalysis(); });
}

// ═══════════════ 提取数据 ═══════════════

/**
 * 从 bag 里取三个话题在 [t0,t1] 内的消息，时间统一为"相对区间起点的秒"。
 * 用消息自带的 header.stamp（与 Python 脚本一致），缺失时退回 bag 记录时间。
 */
// ═══════════════ 联动游标 ═══════════════
//
// 点任意一张图 -> 记下该时刻的绝对时间(ns) -> 其余所有图换算到自己的坐标后画同一条竖线。
// 各图时间基准不同（A0 用区间起点，A1~A6 用最早消息），所以存绝对 ns 而不是本地 x。

/** 给一张图挂上游标能力 */
function attachCursor(fig, xBaseNs) {
  fig.xBaseNs = xBaseNs;
  fig.onCursorPick = (xd) => setCursorNs(xBaseNs + xd * 1e9);
  applyCursorTo(fig);
}

/** 把全局游标应用到某张图 */
function applyCursorTo(fig) {
  if (S.cursorNs == null) { fig.cursorX = null; return; }
  fig.cursorX = (S.cursorNs - fig.xBaseNs) / 1e9;
}

/** 设置游标（点了某张图）*/
function setCursorNs(ns) {
  if (!isFinite(ns)) return;
  S.cursorNs = ns;
  for (const f of S.figs) { applyCursorTo(f); f.draw(); }
  renderCursorBar();
}

function clearCursor() {
  S.cursorNs = null;
  for (const f of S.figs) { f.cursorX = null; f.draw(); }
  renderCursorBar();
}

/** 顶部固定条：滚动页面时也能看到游标时刻与各图读数 */
function renderCursorBar() {
  const bar = $('cursorBar');
  if (!bar) return;
  if (S.cursorNs == null || !S.data) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';

  // 主时间轴用 A1~A6 的基准（D.baseNs），A0 可能基准不同
  const base = S.data.baseNs != null ? S.data.baseNs : 0;
  const tRel = (S.cursorNs - base) / 1e9;
  $('cbTime').textContent = tRel.toFixed(3);

  // 逐图读数
  const box = $('cbRead');
  box.innerHTML = '';
  let visible = 0;
  S.figs.forEach((f, i) => {
    if (f.cursorX == null) return;
    const name = (FIG_META[i] && FIG_META[i][0]) || `F${i}`;
    const parts = [];
    for (const a of f.axes) {
      if (a.noCursor) continue;
      for (const it of f.readAt(a, f.cursorX)) {
        parts.push(`<span class="cbi"><i style="background:${it.color}"></i>${fmtNum(it.v)}</span>`);
      }
    }
    if (!parts.length) return;
    visible++;
    const row = document.createElement('div');
    row.className = 'cbRow';
    row.innerHTML = `<span class="cbName">${name}</span>${parts.join('')}`;
    box.appendChild(row);
  });
  $('cbVis').textContent = visible ? `${visible} 张图在视野内` : '游标不在任何图的当前视野内';

  // 游标一动，A7/A8 跟着刷新
  renderRawMsg();
  renderCompare();
}

// ═══════════════ A7 / A8：原始消息 ═══════════════

/** 纳秒 -> "MM-DD HH:MM:SS.mmm"（本地时区，与 rosbag info 一致） */
function fmtRosWall(ns) {
  if (ns == null || !isFinite(ns)) return '--';
  const d = new Date(ns / 1e6);
  const p = (n, w = 2) => String(n).padStart(w, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

/** 复制文本。file:// 下 clipboard API 常被拒，退回 execCommand */
function copyText(t) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(t).then(() => true, () => legacy());
    }
  } catch (e) { /* 落到 legacy */ }
  return Promise.resolve(legacy());
  function legacy() {
    try {
      const ta = document.createElement('textarea');
      ta.value = t;
      ta.style.position = 'fixed';
      ta.style.top = '-1000px';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (e) { return false; }
  }
}

/** JSON 语法高亮（与 webbag 同一套配色） */
function escapeHtml(s) { return String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
function hlJson(json) {
  return escapeHtml(json).replace(
    /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*")(\s*:)?|\b(?:true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g,
    (mm, str, _2, colon) => {
      let cls = 'jn';
      if (str) cls = colon ? 'jk' : 'jv';
      else if (/^(true|false)$/.test(mm)) cls = 'jb';
      else if (/^null$/.test(mm)) cls = 'jp';
      return `<span class="${cls}">${mm}</span>`;
    });
}

/** 消息 -> 可复制的 JSON 文本。TypedArray 折叠成 Array(n)，数字 6 位小数 */
function msgToJson(m) {
  return JSON.stringify(m, (k, v) => {
    if (v && v.BYTES_PER_ELEMENT !== undefined) return `Array(${v.length})`;
    if (typeof v === 'number') return +v.toFixed(6);
    return v;
  }, 2);
}

/** 话题下拉：全部话题（带消息数），默认选 imu */
function buildRawTopicSelector() {
  const sel = $('rawTopicSel');
  if (!sel || !S.bag) return;
  const prev = sel.value;
  sel.innerHTML = '';
  for (const t of S.topics) {
    const c = S.bag.byTopic.get(t);
    const o = document.createElement('option');
    o.value = t;
    o.textContent = `${t}  (${c ? c.length : 0})`;
    sel.appendChild(o);
  }
  // 优先保持上次选择，否则落在 imu（分析时最常看它）
  if (prev && S.topics.includes(prev)) sel.value = prev;
  else if (S.imuTopic && S.topics.includes(S.imuTopic)) sel.value = S.imuTopic;
  sel.onchange = () => renderRawMsg();
}

/**
 * 该话题在当前游标位置的上一条 / 下一条。
 * 为什么不用 latestBefore 再 ±1 秒：消息间隔不均匀，按索引走才不会漏帧或停在同一条。
 */
function rawNeighborEntry(topic, dir) {
  const idx = S.bag && S.bag.byTopic.get(topic);
  if (!idx || !idx.length || S.cursorNs == null) return null;
  let lo = 0, hi = idx.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (S.bag.entries[idx[mid]].stamp <= S.cursorNs) lo = mid + 1; else hi = mid;
  }
  let i = lo - 1 + dir;
  if (i < 0) i = 0;
  if (i >= idx.length) i = idx.length - 1;
  return S.bag.entries[idx[i]];
}

/** A7：显示游标时刻、选中话题最近一帧的完整内容 */
function renderRawMsg() {
  const info = $('rawInfo'), pre = $('rawJson');
  if (!info || !pre) return;
  if (!S.bag) { info.textContent = '未加载 bag'; return; }
  const topic = $('rawTopicSel') ? $('rawTopicSel').value : '';
  if (!topic) { info.textContent = '请选择话题'; pre.textContent = ''; return; }
  if (S.cursorNs == null) {
    info.textContent = '在图上单击任意位置以定位';
    pre.textContent = '（在图上单击任意位置后，这里显示该时刻最近一帧的原始内容 —— 未做任何单位换算）';
    return;
  }
  const e = S.bag.latestBefore(topic, S.cursorNs);
  if (!e) {
    info.textContent = '该时刻之前没有这个话题的消息';
    pre.textContent = '';
    return;
  }
  const base = (S.data && S.data.baseNs != null) ? S.data.baseNs : S.bag.startTime;
  const rel = (e.stamp - base) / 1e9;
  const m = S.bag.decode(e);
  const conn = S.bag.connections.get(e.connId);
  info.innerHTML = `${topic} @ <b>${rel.toFixed(3)}</b>s · 墙上 ${fmtRosWall(e.stamp)}` +
    (conn ? ` · ${conn.type}` : '');
  pre.innerHTML = m ? hlJson(msgToJson(m)) : '（这条消息解析失败）';
}

/** 上一条 / 下一条：把游标移到该话题相邻消息的时刻（其余图会同步） */
function stepRaw(dir) {
  if (!S.bag || S.cursorNs == null) return null;
  const topic = $('rawTopicSel') ? $('rawTopicSel').value : '';
  if (!topic) return null;
  const e = rawNeighborEntry(topic, dir);
  if (e) setCursorNs(e.stamp);
  return e;
}

/** 按钮反馈：临时占位，不覆盖渲染逻辑写的内容 */
function flash(el, msg) {
  if (!el) return;
  const old = el.textContent;
  el.textContent = msg;
  setTimeout(() => { if (el.textContent === msg) el.textContent = old; }, 1800);
}

/** A8：游标时刻，各话题最近一帧的数值（过滤 covariance 等矩阵字段） */
function renderCompare() {
  const pre = $('cmpTable'), info = $('cmpInfo');
  if (!pre || !info) return;
  if (!S.bag) return;
  if (S.cursorNs == null) {
    info.textContent = '在图上单击任意位置以定位';
    pre.textContent = '（在图上单击任意位置后，这里列出各话题在该时刻最近一帧的数值）';
    return;
  }
  const base = (S.data && S.data.baseNs != null) ? S.data.baseNs : S.bag.startTime;
  const hideCov = $('hideCov') ? $('hideCov').checked : true;
  const isNoise = n => hideCov && /covariance|\.\[?\d+\]?$/.test(n);
  const csv = ['topic,t_rel_s,wall_clock,field,value'];
  const lines = [];
  let nTopic = 0, nField = 0;
  for (const topic of S.topics) {
    const e = S.bag.latestBefore(topic, S.cursorNs);
    if (!e) continue;
    const m = S.bag.decode(e);
    if (!m) continue;
    const f = flatNums(m, '', Object.create(null), 0);
    const names = Object.keys(f).filter(n => !isNoise(n)).sort();
    if (!names.length) continue;
    const rel = ((e.stamp - base) / 1e9).toFixed(3);
    const wall = fmtRosWall(e.stamp);
    nTopic++;
    lines.push(`── ${topic}  @ ${rel}s  ${wall}`);
    for (const n of names) {
      const v = f[n];
      const vs = Number.isInteger(v) ? String(v) : String(+v.toFixed(6));
      lines.push(`   ${n.padEnd(38)} ${vs}`);
      csv.push(`${topic},${rel},${wall},${n},${vs}`);
      nField++;
    }
  }
  pre.textContent = lines.length ? lines.join('\n') : '该时刻之前没有任何话题的消息';
  info.textContent = `${nTopic} 个话题 · ${nField} 个数值字段`;
  pre._csv = csv.join('\n');
}

// ═══════════════ A0 自定义曲线 ═══════════════

const A0_COLORS = ['#1565c0', '#e65100', '#2e7d32', '#c62828', '#6a1b9a',
                   '#00838f', '#ef6c00', '#4527a0', '#00897b', '#ad1457',
                   '#558b2f', '#283593'];
const A0_MAX = 12;

// 图高（与 index.html 里 canvas.figcv 的 height 保持一致）
const FIG_H = 300;      // A0 与 A1~A4 单图
const FIG_H2 = 450;     // A5/A6 双子图（高度减半后每格约 180px）

/**
 * 摊平成 { 字段路径: 数值 }。
 * ROS 的 time/duration（sec+nsec 或 sec+nanosec）折成秒；
 * 布尔转成 0/1，这样才能画开关量。
 */
function flatNums(obj, prefix, out, depth) {
  if (depth > 4) return out;
  for (const k in obj) {
    const v = obj[k];
    if (v === null || v === undefined) continue;
    const p = prefix ? prefix + '.' + k : k;
    if (typeof v === 'number') { if (isFinite(v)) out[p] = v; }
    else if (typeof v === 'boolean') out[p] = v ? 1 : 0;
    else if (typeof v === 'object') {
      if (typeof v.sec === 'number' && (typeof v.nanosec === 'number' || typeof v.nsec === 'number')) {
        out[p] = v.sec + (v.nanosec != null ? v.nanosec : v.nsec) * 1e-9;
      } else if (!Array.isArray(v)) {
        flatNums(v, p, out, depth + 1);
      }
    }
  }
  return out;
}

/**
 * 枚举一个话题里可画的数值字段。
 * 采样若干条消息（不是全量，几万条的话题全解一遍会卡），
 * 顺带标出哪些字段是常量（全程一个值，画出来是条直线，通常没意义）。
 */
const _fieldCache = new Map();
function topicFields(topic) {
  const key = topic + '\u0000' + (S.bag ? S.bag.entries.length : 0);
  if (_fieldCache.has(key)) return _fieldCache.get(key);
  const idx = (S.bag && S.bag.byTopic.get(topic)) || [];
  const SAMPLE = 24;
  const step = Math.max(1, Math.floor(idx.length / SAMPLE));
  const names = new Set();
  const vals = new Map();      // 字段 -> 见过的不同值集合
  let n = 0;
  for (let k = 0; k < idx.length && n < SAMPLE; k += step, n++) {
    const m = S.bag.decode(S.bag.entries[idx[k]]);
    if (!m) continue;
    const f = flatNums(m, '', Object.create(null), 0);
    for (const p in f) {
      names.add(p);
      if (!vals.has(p)) vals.set(p, new Set());
      const st = vals.get(p);
      if (st.size < 4) st.add(f[p]);      // 只需知道"是否变化"，不必存全部
    }
  }
  const fields = [...names].sort().map(p => ({
    name: p,
    const: (vals.get(p) || new Set()).size <= 1,
  }));
  const res = { fields, count: idx.length };
  _fieldCache.set(key, res);
  return res;
}

/** 清掉 A0 缓存（换 bag 或改区间时调用） */
function clearA0Cache() { _fieldCache.clear(); _a0SeriesCache.clear(); }

/**
 * 抽取一条序列（带区间过滤）。
 * 缓存 key 含区间，所以改 start/end 后会自动重取。
 */
const _a0SeriesCache = new Map();
function getA0Series(topic, field, t0, t1) {
  const key = `${topic}\u0000${field}\u0000${t0}\u0000${t1}`;
  if (_a0SeriesCache.has(key)) return _a0SeriesCache.get(key);
  const idx = (S.bag && S.bag.byTopic.get(topic)) || [];
  const lo = S.bag.startTime + t0 * 1e9;
  const hi = S.bag.startTime + t1 * 1e9;
  const xs = [], ys = [];
  const MAX = 40000;
  const step = Math.max(1, Math.ceil(idx.length / MAX));
  for (let k = 0; k < idx.length; k += step) {
    const e = S.bag.entries[idx[k]];
    if (e.stamp < lo || e.stamp > hi) continue;
    const m = S.bag.decode(e);
    if (!m) continue;
    const v = flatNums(m, '', Object.create(null), 0)[field];
    if (v === undefined) continue;
    // 与 A1~A6 统一时间源：优先消息自带的 header.stamp，没有才退回 bag 记录时间。
    // 否则同一时刻在 A0 和 A1 上会差出 header 与 record 的偏移。
    const st = stampNs(m, e);
    if (st == null) continue;
    xs.push((st - lo) / 1e9);
    ys.push(v);
  }
  let mn = Infinity, mx = -Infinity;
  for (const v of ys) { if (v < mn) mn = v; if (v > mx) mx = v; }
  const r = { topic, field, xs, ys, min: mn, max: mx, changed: mx > mn, n: xs.length };
  _a0SeriesCache.set(key, r);
  return r;
}

function a0Key(topic, field) { return topic + '\u0000' + field; }
function a0Label(topic, field) {
  const seg = topic.split('/').filter(Boolean);
  const t = seg.length > 1 ? seg.slice(-2).join('/') : topic;
  return `${t} · ${field.split('.').pop()}`;
}

/** 加入 / 移除一条 A0 曲线 */
function toggleA0(topic, field) {
  const k = a0Key(topic, field);
  const i = S.a0.picks.findIndex(p => a0Key(p.topic, p.field) === k);
  if (i >= 0) { S.a0.picks.splice(i, 1); }
  else {
    if (S.a0.picks.length >= A0_MAX) return 'reject';
    S.a0.picks.push({ topic, field });
  }
  rebuildA0();
  return 'ok';
}

/**
 * 只重画 A0（不重算 A1~A6）。
 * A0 是交互式的，选个字段就等几秒重算全部图太浪费。
 */
function rebuildA0() {
  if (!S.bag || !S.data) return;
  const [t0, t1] = S.range;
  const prev = S.a0Wrap;
  const host = document.getElementById('figs');
  // 记下旧 A0 停在哪：用户可能已经把它拖到中间了，重建后必须放回原位，
  // 不能无条件塞到最前面 —— 否则每改一次字段顺序就被打回原形。
  const hadPrev = !!(prev && prev.parentNode === host);
  const anchor = hadPrev ? prev.nextSibling : null;
  const r = buildA0Figure(t0, t1);
  if (!r) return;
  if (prev && prev.parentNode) prev.parentNode.removeChild(prev);
  if (r.wrap.parentNode === host) {
    // insertBefore(node, null) 等价 appendChild，所以 anchor=null 正好表示"放到末尾"
    if (hadPrev) host.insertBefore(r.wrap, anchor);
    else if (host.firstChild !== r.wrap) host.insertBefore(r.wrap, host.firstChild);
  }
  attachCursor(r.fig, S.bag.startTime + t0 * 1e9);   // 重建后要重新挂游标
  S.a0Wrap = r.wrap;
  S.figs[0] = r.fig;
  renderFieldMenu();
  updateA0Info();
  renderCursorBar();
}

/**
 * 构建 A0 图。
 * 归一化模式下每条线独立映射到 0~1（跨话题量纲不同时必需），
 * 图例里标注各自的实际范围；关掉则共享 Y 轴，适合量纲相同的几个字段。
 */
function buildA0Figure(t0, t1) {
  const r = newFig(0, null, FIG_H, true, true);
  const a = r.fig.add(new Axes('', 'Time in range (s)',
    S.a0.norm ? 'Normalized (0~1 per curve)' : 'Value', { yZeroLine: true }));
  const picks = S.a0.picks;
  let any = false;
  picks.forEach((pk, i) => {
    const se = getA0Series(pk.topic, pk.field, t0, t1);
    if (!se.n) return;
    any = true;
    const color = A0_COLORS[i % A0_COLORS.length];
    const range = se.changed ? ` [${fmtNum(se.min)}~${fmtNum(se.max)}]` : ' [常量]';
    let ys = se.ys;
    if (S.a0.norm) {
      if (se.changed) {
        const d = se.max - se.min || 1;
        ys = se.ys.map(v => (v - se.min) / d);
      } else {
        ys = se.ys.map(() => 0.5);
      }
    }
    a.add(se.xs, ys, {
      color, lw: 1.3, marker: se.n <= 600 ? '.' : 'none',
      label: a0Label(pk.topic, pk.field) + (S.a0.norm ? range : ''),
    });
  });
  a.autoScale();
  if (!any) {
    // 一条都没选时给个提示，而不是画个空框
    a.xlim = [0, Math.max(1, t1 - t0)];
    a.ylim = [0, 1];
    a.home = a.home || { x: a.xlim.slice(), y: a.ylim.slice() };
  }
  r.fig.draw();
  attachCursor(r.fig, S.bag.startTime + t0 * 1e9);
  return r;
}

/** 顶栏/图头的状态文字 */
function updateA0Info() {
  const el = $('a0Info');
  if (!el) return;
  const n = S.a0.picks.length;
  el.textContent = n ? `A0: ${n} 条曲线` : (S.bag ? 'A0: 未选字段' : '');
}

/** 点击面板外部关闭（绑在 document 上，面板内的点击要放行） */
function bindFieldMenuOutside() {
  document.addEventListener('pointerdown', e => {
    const m = $('fieldMenu');
    if (!m || m.style.display === 'none') return;
    if (m.contains(e.target)) return;
    if (e.target && (e.target.id === 'btnFields' || e.target.closest('.figbtns'))) return;
    m.style.display = 'none';
  });
}

/** 字段选择面板显隐 */
function toggleFieldMenu(force) {
  const m = $('fieldMenu');
  if (!m) return;
  const show = force != null ? force : m.style.display === 'none';
  m.style.display = show ? 'block' : 'none';
  if (show) renderFieldMenu();
}

/**
 * 渲染字段列表：按话题分组，只列数值字段，
 * 常量字段灰掉（画出来是一条直线，通常没意义，但保留可点）。
 */
function renderFieldMenu() {
  const box = $('fieldMenu');
  if (!box || !S.bag) return;
  const kw = ($('fieldSearch') && $('fieldSearch').value || '').trim().toLowerCase();
  box.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'fmHead';
  head.innerHTML = `<b>选择数值字段</b><span class="fmHint">最多 ${A0_MAX} 条 · 可跨话题</span>`;
  box.appendChild(head);
  const inp = document.createElement('input');
  inp.id = 'fieldSearch'; inp.className = 'fmSearch'; inp.placeholder = '搜索话题或字段…';
  inp.value = kw;
  inp.oninput = () => renderFieldMenu();
  box.appendChild(inp);

  // covariance 这类矩阵字段一个消息能出几十个，默认藏起来，不然真正的信号被淹没
  const hideCov = $('hideCov') ? $('hideCov').checked : true;
  const covRow = document.createElement('label');
  covRow.className = 'fmOpt';
  covRow.innerHTML = '<input type="checkbox" id="hideCov"' + (hideCov ? ' checked' : '') +
    '> 隐藏 covariance 等矩阵字段';
  box.appendChild(covRow);
  $('hideCov').onchange = () => renderFieldMenu();
  const isNoise = n => hideCov && /covariance|\.\[?\d+\]?$/.test(n);

  const picked = new Set(S.a0.picks.map(p => a0Key(p.topic, p.field)));
  let shown = 0;
  for (const topic of S.topics) {
    const info = topicFields(topic);
    if (!info.fields.length) continue;
    const matched = info.fields.filter(f =>
      (!kw || topic.toLowerCase().includes(kw) || f.name.toLowerCase().includes(kw)) &&
      !isNoise(f.name));
    if (!matched.length) continue;
    shown++;
    const grp = document.createElement('div');
    grp.className = 'fmGroup';
    const th = document.createElement('div');
    th.className = 'fmTopic';
    th.innerHTML = `<span>${topic}</span><span class="fmCnt">${info.count} msgs</span>`;
    grp.appendChild(th);
    for (const f of matched) {
      const on = picked.has(a0Key(topic, f.name));
      const row = document.createElement('div');
      row.className = 'fmRow' + (on ? ' on' : '') + (f.const ? ' const' : '');
      row.innerHTML = `<span class="fmChk">${on ? '\u2713' : ''}</span><span class="fmName">${f.name}</span>` +
                      (f.const ? '<span class="fmTag">常量</span>' : '');
      row.onclick = () => toggleA0(topic, f.name);
      grp.appendChild(row);
    }
    box.appendChild(grp);
  }
  if (!shown) {
    const d = document.createElement('div');
    d.className = 'fmEmpty';
    d.textContent = kw ? `没有匹配「${kw}」的数值字段` : '这个 bag 里没有可画的数值字段';
    box.appendChild(d);
  }
  // 搜索框重绘后要保住焦点和光标位置
  const ni = $('fieldSearch');
  if (ni && document.activeElement !== ni) { /* 初次渲染不抢焦点 */ }
}


function extract(t0, t1) {
  const bag = S.bag;
  const bagT0 = bag.startTime;
  const lo = bagT0 + t0 * 1e9;
  const hi = bagT0 + t1 * 1e9;

  const res = {};
  const grab = (topic, pick) => {
    const out = { tNs: [], wz: [], yaw: [] };
    if (!topic) return out;
    const idx = bag.byTopic.get(topic);
    if (!idx) return out;
    for (const i of idx) {
      const e = bag.entries[i];
      if (e.stamp < lo || e.stamp > hi) continue;
      const m = bag.decode(e);
      if (!m) continue;
      const st = stampNs(m, e);
      if (st == null) continue;
      const v = pick(m);
      if (!v) continue;
      out.tNs.push(st);
      out.wz.push(v.wz);
      out.yaw.push(v.yaw);
    }
    return out;
  };

  res.imu = grab(S.imuTopic, m => {
    if (m.angular_velocity == null || m.orientation == null) return null;
    return { wz: m.angular_velocity.z || 0, yaw: quatToYaw(m.orientation) };
  });
  res.odom = grab(S.odomTopic, m => {
    const tw = m.twist && m.twist.twist;
    const po = m.pose && m.pose.pose && m.pose.pose.orientation;
    if (po == null) return null;
    return { wz: tw && tw.angular ? (tw.angular.z || 0) : 0, yaw: quatToYaw(po) };
  });
  res.tracked = grab(S.trackedTopic, m => {
    const po = m.pose && m.pose.pose && m.pose.pose.orientation;
    if (po == null) return null;
    return { wz: 0, yaw: quatToYaw(po) };
  });

  // 相对时间：Python 用 min(all_times) 归零。
  // 先在纳秒域相减再除 1e9，避免 1.78e9 秒量级上做减法丢失亚毫秒精度。
  const all = [...res.imu.tNs, ...res.odom.tNs, ...res.tracked.tNs];
  // 三个话题都不存在时（比如 bag 里只有 /imu_state），base 不能退化成 0 ——
  // 否则联动游标 / A9 算出来的"相对时刻"会变成 1e9 秒这种天文数字。
  const base = all.length ? Math.min(...all) : lo;
  res.baseNs = base;          // 供联动游标做时间基准换算
  for (const k of ['imu', 'odom', 'tracked']) res[k].t = res[k].tNs.map(v => (v - base) / 1e9);

  // 连续 yaw（度，起点 0）
  res.imu.yawDeg = unwrapYaw(res.imu.yaw).map(v => v * RAD_TO_DEG);
  res.odom.yawDeg = unwrapYaw(res.odom.yaw).map(v => v * RAD_TO_DEG);
  res.tracked.yawDeg = unwrapYaw(res.tracked.yaw).map(v => v * RAD_TO_DEG);
  return res;
}

/**
 * 取消息的纳秒时间戳。
 * 优先用消息自带的 header.stamp（与 Python 脚本一致），为 0 或缺失时退回 bag 记录时间。
 *
 * 注意字段名的坑: ROS1 的 time 类型字段叫 sec/nsec，ROS2 叫 sec/nanosec。
 * 只读 nanosec 的话 ROS1 bag 的亚秒部分会被当 0，时间直接退化到整秒精度。
 */
function stampNs(m, e) {
  const h = m && m.header && m.header.stamp;
  if (h) {
    const sec = h.sec;
    const ns = h.nanosec != null ? h.nanosec : h.nsec;
    if (sec > 1e6) return sec * 1e9 + (ns || 0);
  }
  return e.stamp;
}

/**
 * 给所有图绑定游标。
 * A0 的基准是区间起点，A1~A6 是最早消息时间 —— 两者不同，必须分别指定，
 * 否则点 A1 的某个时刻，A0 上的竖线会偏。
 * A5/A6 的下半部分（dt 直方图 / delta 直方图）x 轴不是时间，标 noCursor 跳过。
 */
function bindCursors(D) {
  const BASE = (D && D.baseNs != null) ? D.baseNs : 0;
  const F = { A0: 0, A1: 1, A2: 2, A3: 3, A4: 4, A5: 5, A6: 6 };
  const [t0] = S.range;
  const bases = {
    [F.A0]: S.bag.startTime + t0 * 1e9,
    [F.A1]: BASE, [F.A2]: BASE, [F.A3]: BASE, [F.A4]: BASE,
    [F.A5]: BASE, [F.A6]: BASE,
  };
  S.figs.forEach((f, i) => {
    attachCursor(f, bases[i] != null ? bases[i] : BASE);
    if (i === F.A5 || i === F.A6) {
      f.axes.forEach((a, k) => { if (k === 1) a.noCursor = true; });
    }
  });
}

// ═══════════════ 主流程 ═══════════════

function runAnalysis() {
  if (!S.bag) return;
  const [t0, t1] = S.range;
  const D = extract(t0, t1);
  S.data = D;

  const d = document.getElementById('hintBox');
  if (d) d.style.display = 'none';

  buildAllFigures(D);
  bindCursors(D);
  buildReports(D);
  renderCursorBar();
  renderDriftAll();     // 采样点的 t_rel 跟着 baseNs 走，改区间后要重算
}

// ═══════════════ 六张图 ═══════════════

function newFig(n, extraTitle, hpx, full, atTop) {
  const [code, title] = FIG_META[n];
  const cv = document.createElement('canvas');
  cv.className = 'figcv';
  if (hpx) cv.style.height = hpx + 'px';   // 双子图的 A5/A6 要更高才看得清（每个子图约 450px）
  const wrap = document.createElement('div');
  wrap.className = 'fig';
  wrap.dataset.fid = code;              // 拖动排序用，重建后顺序才不会丢
  const head = document.createElement('div');
  head.className = 'fighead';
  head.innerHTML = `<span class="dragh" title="按住拖动可调整显示顺序">⠿</span><span class="code">${code}</span><span class="ttl">${title}${extraTitle ? ' — ' + extraTitle : ''}</span>`;
  // 拖动在部分机器上不灵敏（触控板、canvas 抢事件），所以再给一对 ↑↓ 按钮兜底
  appendMoveBtns(head, code, true);
  const btns = document.createElement('span');
  btns.className = 'figbtns';
  const mk2 = (txt, title, fn) => {
    const b = document.createElement('button');
    b.className = 'mini'; b.textContent = txt; b.title = title;
    b.onclick = fn; btns.appendChild(b); return b;
  };
  head.appendChild(btns);
  wrap.appendChild(head);
  wrap.appendChild(cv);
  // full = 跨整行（A0 用）。其余图两两并排，由 CSS grid 自动排。
  // grid 里视觉顺序 = DOM 顺序，所以 A0 必须是第一个子节点，不能 append 到末尾。
  if (full) wrap.classList.add('full');
  const host = document.getElementById('figs');
  // insertBefore(node, null) 等价于 appendChild，所以这一行同时覆盖两种情况
  if (atTop) host.insertBefore(wrap, host.firstChild);
  else host.appendChild(wrap);
  const fig = new Figure(cv, { suptitle: '' });
  const r = { fig, cv, wrap, btns, head, code, title };
  if (n === 0) {
    // A0 专属按钮：选字段 / 清空
    mk2('选字段', '挑要看的数值字段（可跨话题）', () => toggleFieldMenu());
    mk2('清空', '移除全部曲线', () => { S.a0.picks = []; rebuildA0(); });
  }
  mk2('+', '放大（以中心为锚点）', () => fig.zoomAll(1 / 1.4));
  mk2('\u2212', '缩小', () => fig.zoomAll(1.4));
  mk2('复位', '恢复原始范围', () => { for (const a of fig.axes) a.reset(); fig.draw(); });
  return r;
}

function buildAllFigures(D) {
  const host = document.getElementById('figs');
  // 只清 .fig，不能 innerHTML='' —— A7/A8/A9 等报告块也挂在这个容器里（要一起拖动排序）
  [...host.children].forEach(e => { if (e.classList.contains('fig')) e.remove(); });
  S.figs = [];

  // ── A0 自定义曲线（用户选字段，可跨话题）──
  {
    const [t0, t1] = S.range;
    const r = buildA0Figure(t0, t1, true);
    S.a0Wrap = r.wrap;
    S.figs.push(r.fig);
  }

  // ── A1 角速度 z ──
  {
    const { fig } = newFig(1, null, FIG_H);
    const a = fig.add(new Axes('', 'Time (s)', 'Angular Velocity (deg/s)', { yZeroLine: true }));
    if (D.imu.t.length) {
      a.add(D.imu.t, D.imu.wz.map(v => v * RAD_TO_DEG),
        { color: SRC_COLORS.imu, marker: '.', label: 'imu_z (deg/s)' });
    }
    if (D.odom.t.length) {
      a.add(D.odom.t, D.odom.wz.map(v => v * RAD_TO_DEG),
        { color: SRC_COLORS.odom, dash: SRC_DASH.odom, marker: 'x', label: 'odom_z (deg/s)' });
    }
    a.autoScale(); fig.draw(); S.figs.push(fig);
  }

  // ── A2 三路 yaw 对比 ──
  {
    const { fig } = newFig(2, null, FIG_H);
    const a = fig.add(new Axes('', 'Time (s)', 'Yaw (degrees)', { yZeroLine: true }));
    addYaw(a, 'imu', D.imu, 'imu_yaw (deg)');
    addYaw(a, 'odom', D.odom, 'odom_yaw (deg)');
    addYaw(a, 'tracked_pose', D.tracked, 'tracked_pose_yaw (deg)');
    a.autoScale(); fig.draw(); S.figs.push(fig);
  }

  // ── A3 odom - imu 漂移 ──
  {
    const { fig } = newFig(3, null, FIG_H);
    const a = fig.add(new Axes('', 'Time (s)', 'Yaw diff (deg)', { yZeroLine: true }));
    if (D.odom.t.length && D.imu.t.length) {
      const ys = D.odom.t.map((t, i) => D.odom.yawDeg[i] - interp(t, D.imu.t, D.imu.yawDeg));
      a.add(D.odom.t, ys, { color: '#c62828', lw: 0.9, label: 'odom-imu yaw diff (deg)' });
    }
    a.autoScale(); fig.draw(); S.figs.push(fig);
  }

  // ── A4 帧间角速度 ──
  {
    const { fig } = newFig(4, null, FIG_H);
    const a = fig.add(new Axes('', 'Time (s)', 'Yaw step (deg/s)', { yZeroLine: true }));
    for (const [k, d] of [['imu', D.imu], ['odom', D.odom], ['tracked_pose', D.tracked]]) {
      if (!d.t || d.t.length < 2) continue;
      const { midT, rate } = frameYawRate(d.t, d.yawDeg);
      a.add(midT, rate, {
        color: SRC_COLORS[k], dash: SRC_DASH[k], marker: '.',
        label: `${k} yaw step (deg/s)`,
      });
    }
    a.autoScale(); fig.draw(); S.figs.push(fig);
  }

  // ── A5 频率诊断（上：瞬时频率；下：dt 直方图）──
  {
    const { fig } = newFig(5, null, FIG_H2);
    const a1 = fig.add(new Axes('Instantaneous frequency (1/dt) - flat line means stable publishing',
      'Time (s)', 'Instantaneous freq (Hz)'));
    const a2 = fig.add(new Axes('Interval (dt) histogram - integer-multiple peaks = periodic packet drop',
      'dt (ms)', 'Count'));

    const inputs = [];
    if (D.imu.t.length >= 3) inputs.push(['imu', D.imu.t]);
    if (D.odom.t.length >= 3) inputs.push(['odom', D.odom.t]);
    if (D.tracked.t.length >= 3) inputs.push(['tracked_pose', D.tracked.t]);
    const stats = inputs.map(([n, t]) => analyzeTopicFrequency(t, n)).filter(s => s.ok);

    let ymax = 0;
    for (const [name, times] of inputs) {
      const st = stats.find(s => s.name === name);
      if (!st) continue;
      const midT = [], inst = [];
      for (let i = 1; i < times.length; i++) {
        const dt = times[i] - times[i - 1];
        if (dt <= 0) continue;
        midT.push((times[i - 1] + times[i]) / 2);
        inst.push(1 / dt);
      }
      a1.add(midT, inst, {
        color: SRC_COLORS[name], marker: '.', alpha: 0.85,
        label: `${name} (${st.avgFreq.toFixed(2)} Hz avg, nominal ${st.nominalFreq.toFixed(1)} Hz)`,
      });
      if (name === 'imu') {
        // 标称频率参考线：单独一条水平序列
        a1.add([a1.xRangeAll()[0], a1.xRangeAll()[1]], [st.nominalFreq, st.nominalFreq], {
          color: '#c62828', dash: [5, 4], lw: 1, label: `imu nominal ${st.nominalFreq.toFixed(1)} Hz`,
        });
        ymax = st.nominalFreq * 2.2;
      }
    }
    a1.autoScale();
    if (ymax > 0) a1.ylim = [0, ymax];

    // 直方图：优先 imu，没有就用第一条
    const pick = stats.find(s => s.name === 'imu') || stats[0];
    if (pick) {
      const dtMs = pick.dt.map(v => v * 1000);
      const h = histogram(dtMs);
      const bar = a2.bar(h.counts, {
        color: pick.name === 'imu' ? '#43a047' : '#8d6e63',
        alpha: 0.78, label: `${pick.name} dt distribution`,
      });
      bar._edges = h.edges;
      bar.edgeColor = 'rgba(0,0,0,.35)';
      a2.autoScaleBar(h.edges);
      // 参考线：P5 及其整数倍（蓝）+ 中位数（红）
      const p5ms = pick.p5 * 1000, medMs = pick.medianDt * 1000;
      a2.vlines = [
        { v: p5ms, color: '#1565c0', dash: null, w: 1.6, label: `P5 ${p5ms.toFixed(1)}ms` },
        { v: medMs, color: '#c62828', dash: [5, 4], w: 1.4, label: `median ${medMs.toFixed(1)}ms` },
      ];
      for (const k of [2, 3, 4, 5]) a2.vlines.push({ v: k * p5ms, color: '#1565c0', dash: [2, 3], w: 0.9, alpha: 0.55 });
    } else {
      a2.autoScaleBar([0, 1]);
    }

    fig.draw(); S.figs.push(fig);
  }

  // ── A6 帧间 yaw 增量（上：每帧转角；下：直方图）──
  {
    const { fig } = newFig(6, null, FIG_H2);
    const a1 = fig.add(new Axes('Rotation per frame (deg) - NOT divided by dt. Large steps mean sampling is too sparse',
      'Time (s)', 'Yaw delta per frame (deg)', { yZeroLine: true }));
    const a2 = fig.add(new Axes('Delta histogram - static periods pile up at 0 deg',
      'Yaw delta per frame (deg)', 'Count'));

    const deltaStats = [];
    for (const [k, d] of [['imu', D.imu], ['odom', D.odom], ['tracked_pose', D.tracked]]) {
      if (!d.t || d.t.length < 2) continue;
      const st = analyzeYawDelta(d.t, d.yawDeg, k);
      if (!st) continue;
      deltaStats.push(st);
      a1.add(st.midT, st.d, {
        color: SRC_COLORS[k], marker: '.', alpha: k === 'imu' ? 0.9 : 0.6,
        label: `${k} (RMS ${st.rms.toFixed(4)} deg/frame, max |${st.maxAbs.toFixed(3)}|)`,
      });
    }
    a1.autoScale();
    if (deltaStats.length) {
      const m = Math.max(...deltaStats.map(s => s.maxAbs));
      if (m > 0) a1.ylim = [-m * 1.15, m * 1.15];
    }

    const imuD = deltaStats.find(s => s.name === 'imu');
    if (imuD) {
      const h = histogram(imuD.d, Math.min(160, Math.max(20, Math.floor(imuD.d.length / 8))));
      const bar = a2.bar(h.counts, { color: '#43a047', alpha: 0.8, label: 'imu per-frame delta' });
      bar._edges = h.edges;
      bar.edgeColor = 'rgba(0,0,0,.35)';
      a2.autoScaleBar(h.edges);
      a2.vlines = [
        { v: 0, color: '#57606a', dash: null, w: 0.8 },
        { v: imuD.mean, color: '#c62828', dash: [5, 4], w: 1.4, label: `mean ${imuD.mean >= 0 ? '+' : ''}${imuD.mean.toFixed(5)} deg` },
        { v: imuD.rms, color: '#ef6c00', dash: [2, 2], w: 1.2, label: `RMS ${imuD.rms.toFixed(4)} deg` },
        { v: -imuD.rms, color: '#ef6c00', dash: [2, 2], w: 1.2 },
      ];
    } else {
      a2.autoScaleBar([0, 1]);
    }

    fig.draw(); S.figs.push(fig);
  }

  applyFigOrder();     // 图重建后按保存的顺序恢复（默认 A0→A6 在前，报告块在后）
  S.reports.deltaStats = null;
}

function addYaw(a, key, d, label) {
  if (!d.t || !d.t.length) return;
  a.add(d.t, d.yawDeg, { color: SRC_COLORS[key], dash: SRC_DASH[key], marker: SRC_MARKER[key], label });
}


// ═══════════════ 文字报告 ═══════════════

function buildReports(D) {
  const inputs = [];
  if (D.imu.t.length >= 3) inputs.push(['imu', D.imu.t]);
  if (D.odom.t.length >= 3) inputs.push(['odom', D.odom.t]);
  if (D.tracked.t.length >= 3) inputs.push(['tracked_pose', D.tracked.t]);
  const stats = inputs.map(([n, t]) => analyzeTopicFrequency(t, n));

  let s = '========== Topic Frequency Diagnostics ==========\n';
  for (const st of stats) {
    s += `\n[${st.name}]\n`;
    if (!st.ok) { s += `  not enough messages to analyze (count=${st.count})\n`; continue; }
    s += `  Messages          : ${st.count}\n`;
    s += `  Duration          : ${st.duration.toFixed(3)} s\n`;
    s += `  Average freq      : ${st.avgFreq.toFixed(3)} Hz   <-- count/duration\n`;
    s += `  Median freq       : ${st.medianFreq.toFixed(3)} Hz   <-- actual rate you get\n`;
    s += `  Fastest (P5) freq : ${st.fastFreq.toFixed(3)} Hz   <-- hints at driver's true rate\n`;
    if (st.discrete) {
      s += `  Est. packet loss  : ${(st.loss * 100).toFixed(1)} %     (nominal ~${st.nominalFreq.toFixed(1)} Hz)\n`;
    } else {
      s += '  Est. packet loss  : n/a  (gaps are continuous jitter, driver rate not inferable)\n';
    }
    s += `  dt mean / median  : ${st.meanDt.toFixed(4)} / ${st.medianDt.toFixed(4)} s\n`;
    s += `  dt std            : ${st.stdDt.toFixed(4)} s\n`;
    s += `  dt min / P5       : ${st.minDt.toFixed(4)} / ${st.p5.toFixed(4)} s\n`;
    s += `  dt max / P95 /P99 : ${st.maxDt.toFixed(4)} / ${st.p95.toFixed(4)} / ${st.p99.toFixed(4)} s\n`;
    s += `  Jitter (CV)       : ${st.cv.toFixed(4)}   -> ${st.grade}\n`;
    s += `  Late intervals    : ${st.nLate} / ${st.dt.length} (${(st.lateRatio * 100).toFixed(1)}%)  [dt > 1.5x median]\n`;
    s += `  Bad / reverse ts  : ${st.nBad}\n`;
    s += `  Periodic-drop idx : ${(st.multRatio * 100).toFixed(1)}% of dt are integer multiples of P5\n`;
    s += `  VERDICT           : ${st.verdict}\n`;
  }
  s += '\n=================================================\n';
  $('reportFreq').textContent = s;

  const ds = [];
  for (const [k, d] of [['imu', D.imu], ['odom', D.odom], ['tracked_pose', D.tracked]]) {
    if (!d.t || d.t.length < 2) continue;
    const st = analyzeYawDelta(d.t, d.yawDeg, k);
    if (st) ds.push(st);
  }
  let s2 = '========== Frame-to-Frame Yaw Delta (deg) ==========\n(per-frame rotation in degrees, NOT divided by dt)\n';
  for (const st of ds) {
    s2 += `\n[${st.name}]\n`;
    s2 += `  Frames (deltas)   : ${st.count}\n`;
    s2 += `  mean / std        : ${st.mean >= 0 ? '+' : ''}${st.mean.toFixed(5)} / ${st.std.toFixed(5)} deg\n`;
    s2 += `  RMS per frame     : ${st.rms.toFixed(5)} deg   <-- typical per-frame step\n`;
    s2 += `  min / max         : ${st.min >= 0 ? '+' : ''}${st.min.toFixed(4)} / ${st.max >= 0 ? '+' : ''}${st.max.toFixed(4)} deg\n`;
    s2 += `  max |delta|       : ${st.maxAbs.toFixed(4)} deg\n`;
    s2 += `  |delta| P95 / P99 : ${st.p95Abs.toFixed(4)} / ${st.p99Abs.toFixed(4)} deg\n`;
    s2 += `  Total |rotation|  : ${st.sumAbs.toFixed(3)} deg  <-- denominator for scale-factor error\n`;
    s2 += `  Net rotation      : ${st.net >= 0 ? '+' : ''}${st.net.toFixed(3)} deg  (last - first)\n`;
    if (st.nOver > 0) {
      s2 += `  !! WARNING        : ${st.nOver} frames exceed ${NEAR_LIMIT} deg - rotation too fast for `
        + 'this sample rate, wrap-around may corrupt these values\n';
    } else {
      s2 += '  Wrap safety       : OK (no frame near the +-180 deg limit)\n';
    }
  }
  s2 += '\n====================================================\n';
  $('reportDelta').textContent = s2;
}

// ═══════════════ 区间条 ═══════════════

const rb = { cv: null, ctx: null, drag: null };

function drawRangeBar() {
  if (!rb.cv) { rb.cv = $('rangeBar'); rb.ctx = rb.cv.getContext('2d'); }
  const cv = rb.cv, ctx = rb.ctx;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(200, cv.clientWidth || 600), h = 34;
  cv.width = w * dpr; cv.height = h * dpr;
  cv.style.height = h + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, w, h);

  if (!S.bag) return;
  const X = t => t / S.dur * w;

  // 消息密度
  if (S.imuTopic && S.bag.byTopic.get(S.imuTopic)) {
    ctx.fillStyle = 'rgba(46,125,50,.45)';
    for (const i of S.bag.byTopic.get(S.imuTopic)) {
      const x = X((S.bag.entries[i].stamp - S.bag.startTime) / 1e9);
      ctx.fillRect(x, 4, 1, h - 8);
    }
  }
  // 选区
  const [a, b] = S.range;
  ctx.fillStyle = 'rgba(0,137,123,.15)';
  ctx.fillRect(X(a), 0, X(b) - X(a), h);
  ctx.strokeStyle = '#00897b'; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(X(a), 0); ctx.lineTo(X(a), h);
  ctx.moveTo(X(b), 0); ctx.lineTo(X(b), h); ctx.stroke();

  ctx.fillStyle = '#57606a'; ctx.font = '10px system-ui, sans-serif';
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText(`${a.toFixed(2)}s`, Math.min(X(a) + 4, w - 40), h / 2);
  ctx.textAlign = 'right';
  ctx.fillText(`${b.toFixed(2)}s`, Math.max(X(b) - 4, 40), h / 2);
}

// ═══════════════ A9 · IMU 零漂分析（静止采样） ═══════════════

/**
 * 零偏（bias）：车辆静止时陀螺理论上该输出 0，实际输出的就是零偏。
 * 在不同时刻多次采样，看零偏怎么漂 —— 这就是零偏稳定性。
 *
 * 【为什么默认取窗口均值而不是单帧】
 *   单帧陀螺读数噪声很大（MEMS 尤其明显）。直接读某一帧，看到的
 *   mostly 是噪声而不是零偏。默认对游标前后各 0.5s 的消息取平均，
 *   噪声被压掉约 √n，零偏才显出来。窗口设 0 则退回单帧取值。
 *
 * 【单位】
 *   bag 里存的是 rad/s（原始值，全程不做任何换算，只在显示时转换）：
 *     deg/s = rad/s × 180/π
 *     °/h   = rad/s × 180/π × 3600   ← 陀螺零偏的行业惯用单位
 */

const RAD_TO_DEG_PER_H = RAD_TO_DEG * 3600;

// 陀螺三轴字段识别：angular_velocity / raw_angular_velocity_avg_10s / gyro / omega ... 的 .x/.y/.z
const GYRO_FIELD_RE = /(^|\.)([A-Za-z0-9_]*(?:angular_velocity|gyro|omega)[A-Za-z0-9_]*)\.([xyz])$/i;

const fin0 = v => (isFinite(v) ? v : 0);
const fmtSigned = (v, d = 6) => (isFinite(v) ? (v >= 0 ? '+' : '') + v.toFixed(d) : '—');
const fmtSci = v => (!isFinite(v) ? '—' : (v === 0 ? '0' : v.toExponential(3)));

/** 列对齐：数字右对齐，其余左对齐 */
function padRows(rows, sep = '  ') {
  if (!rows.length) return [];
  const ncol = Math.max(...rows.map(r => r.length));
  const w = [];
  for (let c = 0; c < ncol; c++) w[c] = Math.max(...rows.map(r => String(r[c] == null ? '' : r[c]).length));
  return rows.map(r => r.map((v, i) => {
    const s = String(v == null ? '' : v);
    return /^[+\-]?[\d.]/.test(s) ? s.padStart(w[i]) : s.padEnd(w[i]);
  }).join(sep).replace(/\s+$/, ''));
}

/** 自动识别 bag 里的陀螺三轴字段组（同一话题、同一前缀，x/y/z 齐全） */
function detectGyroGroups() {
  const groups = new Map();
  for (const topic of (S.topics || [])) {
    const info = topicFields(topic);
    for (const f of info.fields) {
      const m = GYRO_FIELD_RE.exec(f.name);
      if (!m) continue;
      const k = topic + '\u0000' + m[2];
      if (!groups.has(k)) groups.set(k, { topic, prefix: m[2], count: info.count, axes: {} });
      groups.get(k).axes[m[3].toLowerCase()] = f.name;
    }
  }
  const out = [...groups.values()].filter(g => g.axes.x && g.axes.y && g.axes.z);
  const score = g => (g.topic === S.imuTopic ? 100 : 0)
    + (/imu/i.test(g.topic) ? 30 : 0)
    + (/avg|mean|filt/i.test(g.prefix) ? 5 : 0)
    + Math.min(g.count, 200000) / 1e6;
  out.sort((a, b) => score(b) - score(a));
  return out;
}

function driftLabel(sel) {
  if (!sel) return '—';
  const seg = sel.topic.split('/').filter(Boolean);
  const t = seg.length ? '/' + seg.slice(-1).join('/') : sel.topic;
  return `${t} · ${sel.field}`;
}

/** 零漂分析的时间基准：跟着 A1~A6 走，改区间后采样点的 t_rel 自动跟着变 */
function driftBase() {
  const b = (S.data && S.data.baseNs != null) ? S.data.baseNs : 0;
  return b > 0 ? b : (S.bag ? S.bag.startTime : 0);
}

/** 构造三轴组下拉（并尽量保持上次选择） */
function buildDriftGroups() {
  const sel = $('driftGroup');
  if (!sel) return;
  const prev = sel.value;
  S.drift.groups = S.bag ? detectGyroGroups() : [];
  sel.innerHTML = '';
  const add = (v, t) => { const o = document.createElement('option'); o.value = v; o.textContent = t; sel.appendChild(o); };
  add('', '（未选择）');
  S.drift.groups.forEach((g, i) => add(String(i), `${g.topic} · ${g.prefix}   (${g.count} msgs)`));
  add('custom', '自定义…（从全部字段里逐轴挑）');
  if (S.drift.groups.length) {
    const keep = prev !== '' && prev !== 'custom' && S.drift.groups[+prev];
    sel.value = keep ? prev : '0';
    applyDriftGroup();
  } else {
    sel.value = '';
    S.drift.sel = { x: null, y: null, z: null };
  }
  renderDriftAll();
}

function applyDriftGroup() {
  const v = $('driftGroup') ? $('driftGroup').value : '';
  const g = S.drift.groups && S.drift.groups[+v];
  if (g) {
    S.drift.sel = {
      x: { topic: g.topic, field: g.axes.x },
      y: { topic: g.topic, field: g.axes.y },
      z: { topic: g.topic, field: g.axes.z },
    };
  } else if (v !== 'custom') {
    S.drift.sel = { x: null, y: null, z: null };
  }
  renderDriftAll();
}

// ── 逐轴自定义字段面板 ──
let _driftAxis = null;

function openDriftPicker(axis) {
  _driftAxis = axis;
  const m = $('driftMenu');
  if (!m) return;
  m.style.display = 'block';
  renderDriftMenu();
}
function closeDriftPicker() { const m = $('driftMenu'); if (m) m.style.display = 'none'; }

function renderDriftMenu() {
  const box = $('driftMenu');
  if (!box || !S.bag || !_driftAxis) return;
  const kw = (($('driftSearch') && $('driftSearch').value) || '').trim().toLowerCase();
  box.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'fmHead';
  head.innerHTML = `<b>选择 ${_driftAxis.toUpperCase()} 轴字段</b><span class="fmHint">单击确定</span>`;
  box.appendChild(head);
  const inp = document.createElement('input');
  inp.id = 'driftSearch'; inp.className = 'fmSearch'; inp.placeholder = '搜索话题或字段…'; inp.value = kw;
  inp.oninput = () => renderDriftMenu();
  box.appendChild(inp);

  const cur = S.drift.sel[_driftAxis];
  const curKey = cur ? a0Key(cur.topic, cur.field) : '';
  const isNoise = n => /covariance|\.\[?\d+\]?$/.test(n);
  let shown = 0;
  for (const topic of S.topics) {
    const info = topicFields(topic);
    const matched = info.fields.filter(f =>
      (!kw || topic.toLowerCase().includes(kw) || f.name.toLowerCase().includes(kw)) && !isNoise(f.name));
    if (!matched.length) continue;
    shown++;
    const grp = document.createElement('div'); grp.className = 'fmGroup';
    const th = document.createElement('div'); th.className = 'fmTopic';
    th.innerHTML = `<span>${topic}</span><span class="fmCnt">${info.count} msgs</span>`;
    grp.appendChild(th);
    for (const f of matched) {
      const on = a0Key(topic, f.name) === curKey;
      const row = document.createElement('div');
      row.className = 'fmRow' + (on ? ' on' : '') + (f.const ? ' const' : '');
      row.innerHTML = `<span class="fmChk">${on ? '\u2713' : ''}</span><span class="fmName">${f.name}</span>` +
        (f.const ? '<span class="fmTag">常量</span>' : '');
      row.onclick = () => {
        S.drift.sel[_driftAxis] = { topic, field: f.name };
        if ($('driftGroup')) $('driftGroup').value = 'custom';
        closeDriftPicker();
        renderDriftAll();
      };
      grp.appendChild(row);
    }
    box.appendChild(grp);
  }
  if (!shown) {
    const d = document.createElement('div');
    d.className = 'fmEmpty';
    d.textContent = kw ? `没有匹配「${kw}」的字段` : '这个 bag 里没有可用字段';
    box.appendChild(d);
  }
}

/** idx 里第一个 stamp >= ns 的位置（二分，避免大话题全扫） */
function idxLowerBound(idx, ns) {
  let lo = 0, hi = idx.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (S.bag.entries[idx[m]].stamp < ns) lo = m + 1; else hi = m;
  }
  return lo;
}

/**
 * 取某个字段在 tNs 附近 win 秒窗口内的均值。
 * 窗口内没有消息时退回"游标前最近一帧"（n=1），这样低频话题也能用。
 */
function driftSampleAt(sel, tNs, winSec) {
  if (!sel || !S.bag) return null;
  const idx = S.bag.byTopic.get(sel.topic);
  if (!idx || !idx.length) return null;
  const half = Math.max(0, +winSec || 0) / 2 * 1e9;
  const vals = [];
  if (half > 0) {
    const lo = tNs - half, hi = tNs + half;
    for (let k = idxLowerBound(idx, lo); k < idx.length; k++) {
      const e = S.bag.entries[idx[k]];
      if (e.stamp > hi) break;
      const m = S.bag.decode(e);
      if (!m) continue;
      const v = flatNums(m, '', Object.create(null), 0)[sel.field];
      if (v !== undefined && isFinite(v)) vals.push(v);
    }
  }
  if (!vals.length) {
    const e = S.bag.latestBefore(sel.topic, tNs);
    if (!e) return null;
    const m = S.bag.decode(e);
    if (!m) return null;
    const v = flatNums(m, '', Object.create(null), 0)[sel.field];
    if (v === undefined || !isFinite(v)) return null;
    vals.push(v);
  }
  return { v: mean(vals), n: vals.length, sd: std(vals) };
}

/** 记录当前游标处的采样点。返回错误文案，成功返回 null */
function driftAddPick() {
  if (!S.bag) return '未加载 bag';
  if (S.cursorNs == null) return '请先在图上单击，定位游标';
  const sel = S.drift.sel;
  if (!sel.x && !sel.y && !sel.z) return '请先选择 X / Y / Z 字段';
  const w = parseFloat($('driftWin') ? $('driftWin').value : '1');
  S.drift.win = isFinite(w) && w >= 0 ? w : 1.0;
  const rec = { tNs: S.cursorNs };
  let any = false;
  for (const ax of ['x', 'y', 'z']) {
    if (!sel[ax]) { rec[ax] = NaN; rec['n' + ax] = 0; rec['sd' + ax] = NaN; continue; }
    const s = driftSampleAt(sel[ax], S.cursorNs, S.drift.win);
    if (!s) { rec[ax] = NaN; rec['n' + ax] = 0; rec['sd' + ax] = NaN; continue; }
    rec[ax] = s.v; rec['n' + ax] = s.n; rec['sd' + ax] = s.sd;
    any = true;
  }
  if (!any) return '该时刻三轴都没取到值（字段在这个话题里不存在？）';
  S.drift.picks.push(rec);
  S.drift.picks.sort((a, b) => a.tNs - b.tNs);
  renderDriftAll();
  return null;
}

function driftRemoveLast() { S.drift.picks.pop(); renderDriftAll(); }

function driftClearPicks() {
  S.drift.picks = [];
  S.drift.closed = false;
  renderDriftAll();
}

function driftToggleDone() {
  S.drift.closed = !S.drift.closed;
  renderDriftAll();
}

function renderDriftState() {
  const info = $('driftInfo');
  if (info) {
    const n = S.drift.picks.length;
    info.textContent = `已记录 ${n} 个采样点` + (S.drift.closed ? ' · 已结束' : '');
  }
  const bx = $('driftSelX'), by = $('driftSelY'), bz = $('driftSelZ');
  if (bx) bx.textContent = 'X: ' + driftLabel(S.drift.sel.x);
  if (by) by.textContent = 'Y: ' + driftLabel(S.drift.sel.y);
  if (bz) bz.textContent = 'Z: ' + driftLabel(S.drift.sel.z);
  const d = $('driftDone');
  if (d) {
    d.textContent = S.drift.closed ? '继续添加采样点' : '结束选择并分析';
    d.classList.toggle('primary', !S.drift.closed);
  }
  for (const id of ['driftPick', 'driftUndo', 'driftClear']) {
    const el = $(id);
    if (el) el.disabled = (S.drift.closed && id !== 'driftClear') || !S.bag;
  }
}

function renderDriftTable() {
  const pre = $('driftTable');
  if (!pre) return;
  const P = S.drift.picks;
  if (!P.length) {
    pre.textContent = '（在图上单击定位游标 → 点「⦿ 记录当前游标」。车辆静止的时刻最适合采样；建议采 3 个以上、间隔拉开）';
    return;
  }
  const base = driftBase();
  const rows = [['#', 't_rel(s)', '墙上时间', 'X(deg/s)', 'Y(deg/s)', 'Z(deg/s)', '|b|(deg/s)', '样本']];
  P.forEach((p, i) => {
    const g = a => (isFinite(p[a]) ? fmtSigned(p[a] * RAD_TO_DEG, 7) : '—');
    const mag = Math.hypot(fin0(p.x), fin0(p.y), fin0(p.z));
    rows.push([String(i + 1), ((p.tNs - base) / 1e9).toFixed(3), fmtRosWall(p.tNs),
      g('x'), g('y'), g('z'), fmtSigned(mag * RAD_TO_DEG, 7),
      String(Math.max(p.nx || 0, p.ny || 0, p.nz || 0))]);
  });
  pre.textContent = padRows(rows).join('\n');
}

function renderDriftFigure() {
  const cv = $('driftCv');
  if (!cv) return;
  const P = S.drift.picks;
  if (!S._driftFig) S._driftFig = new Figure(cv, { suptitle: '' });
  const fig = S._driftFig;
  fig.axes.length = 0;
  const a = fig.add(new Axes('', 'Time (s)', 'Zero bias (deg/s)', { yZeroLine: true }));
  const C = { x: '#c62828', y: '#2e7d32', z: '#1565c0' };
  const base = driftBase();
  let any = false;
  for (const ax of ['x', 'y', 'z']) {
    if (!S.drift.sel[ax]) continue;
    const xs = [], ys = [];
    for (const p of P) {
      if (!isFinite(p[ax])) continue;
      xs.push((p.tNs - base) / 1e9);
      ys.push(p[ax] * RAD_TO_DEG);
    }
    if (!xs.length) continue;
    any = true;
    a.add(xs, ys, { color: C[ax], marker: 'o', lw: 1.4, label: `${ax.toUpperCase()} · ${driftLabel(S.drift.sel[ax])}` });
  }
  if (!any) {
    a.xlim = [0, 1]; a.ylim = [0, 1];
    a.home = { x: [0, 1], y: [0, 1] };
  } else {
    a.autoScale();
  }
  fig.draw();
}

/** 最小二乘：返回 {slope, intercept, r2}，slope 单位 = (deg/s) per s */
function linfit(ts, ys) {
  const n = ts.length;
  if (n < 2) return null;
  const mt = mean(ts), my = mean(ys);
  let sxy = 0, sxx = 0, syy = 0;
  for (let i = 0; i < n; i++) { const dt = ts[i] - mt, dy = ys[i] - my; sxy += dt * dy; sxx += dt * dt; syy += dy * dy; }
  if (sxx <= 0) return null;
  const slope = sxy / sxx;
  return { slope, intercept: my - slope * mt, r2: syy > 0 ? (sxy * sxy) / (sxx * syy) : 1 };
}

/** 零偏量级参考（粗略分级，非严格标准） */
function biasGrade(degPerHour) {
  const v = Math.abs(degPerHour);
  if (v < 1) return '< 1 °/h：战术级（很好）';
  if (v < 10) return '1~10 °/h：导航 / 工业级 MEMS（良好）';
  if (v < 100) return '10~100 °/h：消费级 MEMS（常见）';
  return '> 100 °/h：偏大，建议标定或查温漂 / 安装';
}

/** 零偏稳定性参考：看 1σ 的量级（不是看均值）—— 1σ 越大说明每次静止读到的值越不稳 */
function biasStabilityGrade(sdDegPerHour) {
  const v = Math.abs(sdDegPerHour);
  if (v < 5) return '很稳定';
  if (v < 20) return '较稳定';
  if (v < 100) return '一般，有明显波动';
  return '不稳定，波动量级已接近甚至超过零偏本身';
}

function renderDriftReport() {
  const pre = $('driftReport');
  if (!pre) return;
  const P = S.drift.picks;
  if (!S.drift.closed) {
    pre.textContent = P.length
      ? `已记录 ${P.length} 个采样点，点「结束选择并分析」生成零漂报告。`
      : '（先在图上单击定位游标 → 「⦿ 记录当前游标」；采完点「结束选择并分析」）';
    return;
  }
  if (P.length < 2) { pre.textContent = `至少需要 2 个采样点才能分析变化（当前 ${P.length} 个）`; return; }

  const base = driftBase();
  const T = P.map(p => (p.tNs - base) / 1e9);
  const span = T[T.length - 1] - T[0];
  const L = [];

  L.push(`采样点 ${P.length} 个 · 跨度 ${span.toFixed(1)}s (${(span / 60).toFixed(2)} min) · 采样窗口 ±${(S.drift.win / 2).toFixed(2)}s`);
  for (const ax of ['x', 'y', 'z']) {
    if (S.drift.sel[ax]) L.push(`  ${ax.toUpperCase()} = ${driftLabel(S.drift.sel[ax])}`);
  }
  const minN = Math.min(...P.map(p => Math.max(p.nx || 0, p.ny || 0, p.nz || 0)));
  if (minN <= 1) L.push('  ⚠ 有采样点只取到 1 帧（该话题频率偏低或窗口太小），噪声较大，建议加大「采样窗口」');

  // ── 每轴统计 ──
  L.push('');
  L.push('【一、每轴零偏统计】原始 rad/s，按度显示；°/h 是陀螺零偏的行业惯用单位（1 deg/s = 3600 °/h）');
  const srows = [['轴', 'n', '均值(deg/s)', '均值(°/h)', '1σ(deg/s)', '1σ(°/h)', '极差(deg/s)', '极差(°/h)']];
  const stat = {};
  for (const ax of ['x', 'y', 'z']) {
    const v = P.map(p => p[ax]).filter(isFinite);
    if (!v.length) continue;
    const m = mean(v), sd = v.length > 1 ? std(v) : 0, mn = Math.min(...v), mx = Math.max(...v);
    stat[ax] = { m, sd, mn, mx, ptp: mx - mn, n: v.length };
    srows.push([ax.toUpperCase(), String(v.length),
      fmtSigned(m * RAD_TO_DEG, 8), fmtSigned(m * RAD_TO_DEG_PER_H, 3),
      // sd 是【原始 rad/s】的标准差，所以 deg/s 要 ×RAD_TO_DEG，°/h 要 ×RAD_TO_DEG_PER_H。
      // 之前 deg/s 那一列直接拿 sd 当度用，等于小了 57.3 倍。
      sd > 0 ? (sd * RAD_TO_DEG).toExponential(3) : '—', fmtSigned(sd * RAD_TO_DEG_PER_H, 3),
      // 极差恒 ≥ 0，不该带正号（fmtSigned 会给正数加 +）
      ((mx - mn) * RAD_TO_DEG).toExponential(3), ((mx - mn) * RAD_TO_DEG_PER_H).toFixed(3)]);
  }
  L.push(...padRows(srows));
  // 1σ 到底是什么意思 —— 这是最容易被误读成"噪声"的一列，必须讲清楚
  L.push('  1σ 是什么：各采样点零偏的【标准差】，衡量"这几次静止采到的值彼此差多少"。');
  L.push('    · 它【不等于】纯噪声 —— 里面混了两次采样之间真实漂掉的量，采样间隔越长混得越多；');
  L.push('    · 想单独测噪声，应在【同一段静止时间内连续采 10 个以上点】（间隔几秒）；');
  L.push('    · 想看器件长期稳定性，才用现在这种跨几分钟的采法。');
  L.push(`  当前 n=${P.length}：` + (P.length < 4
    ? '点太少，1σ 几乎没有统计意义（2 点必然只有一种差值），请至少采 5 个'
    : (P.length < 6 ? '偏少，1σ 仅作粗略参考' : '数量够，1σ 可作参考')) + '。');

  // ── 相邻变化 ──
  L.push('');
  L.push('【二、相邻采样点的零偏变化】Δ = 后一点 − 前一点；每个量同时给出 deg/s 与 °/h');
  const crows = [['区间', 'Δt(s)',
    'ΔX(°/h)', 'ΔX(deg/s)', 'ΔY(°/h)', 'ΔY(deg/s)', 'ΔZ(°/h)', 'ΔZ(deg/s)',
    '|Δ|(°/h)', '|Δ|(deg/s)', '变化率(deg/s per s)', '等效(°/h per h)']];
  const deltas = [];
  for (let i = 0; i + 1 < P.length; i++) {
    const dt = T[i + 1] - T[i];
    const d = {};
    for (const ax of ['x', 'y', 'z']) d[ax] = (isFinite(P[i + 1][ax]) && isFinite(P[i][ax])) ? P[i + 1][ax] - P[i][ax] : NaN;
    const dvec = Math.hypot(fin0(d.x), fin0(d.y), fin0(d.z));
    const rateD = dt > 0 ? (dvec * RAD_TO_DEG) / dt : NaN;
    const rateH = dt > 0 ? (dvec * RAD_TO_DEG_PER_H) * 3600 / dt : NaN;
    deltas.push({ i, dt, d, dvec, rateD, rateH, label: `#${i + 1}→#${i + 2}` });
    crows.push([`#${i + 1}→#${i + 2}`, dt.toFixed(2),
      fmtSigned(d.x * RAD_TO_DEG_PER_H, 3), fmtSci(d.x * RAD_TO_DEG),
      fmtSigned(d.y * RAD_TO_DEG_PER_H, 3), fmtSci(d.y * RAD_TO_DEG),
      fmtSigned(d.z * RAD_TO_DEG_PER_H, 3), fmtSci(d.z * RAD_TO_DEG),
      (dvec * RAD_TO_DEG_PER_H).toFixed(3), (dvec * RAD_TO_DEG).toExponential(3),
      fmtSci(rateD), isFinite(rateH) ? rateH.toFixed(1) : '—']);
  }
  L.push(...padRows(crows));
  // 间隔越短，外推到"每小时"的倍数越大，噪声被同步放大。
  // 不加这句的话，两帧间隔 0.2s 时任何微小抖动都会被算成几百 °/h per h，
  // 看着像器件炸了，其实只是外推倍数（这里 3600/0.2 = 18000 倍）在起作用。
  L.push('  注：间隔越短，外推到「每小时」的倍数越大（噪声同步放大）；');
  L.push('      判断稳定性请优先看【一】的 1σ 与【四】的 R²，这两个不受间隔影响。');
  L.push('  关于「变化率(deg/s per s)」= Δ零偏(deg/s) ÷ Δt(s)：');
  L.push('      意思是"零偏每秒又变了多少 deg/s"，量纲上记作 deg/s² —— 它不是"秒的平方"这种');
  L.push('      独立物理量，就是"每秒的变化量"，跟加速度那个 m/s² 只是写法习惯相同而已。');

  // ── 最大变化 ──
  L.push('');
  L.push('【三、最大变化发生在哪两帧（逐轴）】');
  const mrows = [['轴', '区间', 'Δ(deg/s)', 'Δ(°/h)', 'Δt(s)', '变化率(deg/s per s)']];
  let gmax = null;
  for (const ax of ['x', 'y', 'z']) {
    const cand = deltas.filter(d => isFinite(d.d[ax]));
    if (!cand.length) continue;
    let best = cand[0];
    for (const d of cand) if (Math.abs(d.d[ax]) > Math.abs(best.d[ax])) best = d;
    const dv = best.d[ax];
    mrows.push([ax.toUpperCase(), best.label, fmtSigned(dv * RAD_TO_DEG, 8), fmtSigned(dv * RAD_TO_DEG_PER_H, 3),
      best.dt.toFixed(2), fmtSci(best.dt > 0 ? (dv * RAD_TO_DEG) / best.dt : NaN)]);
    if (!gmax || Math.abs(dv) > Math.abs(gmax.dv)) gmax = { ax, label: best.label, dv, dt: best.dt };
  }
  L.push(...padRows(mrows));
  if (gmax) L.push(`  → 单轴最大：${gmax.ax.toUpperCase()} 轴在 ${gmax.label} 之间，Δ = ${fmtSigned(gmax.dv * RAD_TO_DEG_PER_H, 3)} °/h`);
  if (deltas.length) {
    let bd = deltas[0];
    for (const d of deltas) if (d.dvec > bd.dvec) bd = d;
    L.push(`  → 三轴合成最大：${bd.label} 之间，|Δ| = ${(bd.dvec * RAD_TO_DEG_PER_H).toFixed(4)} °/h（Δt=${bd.dt.toFixed(2)}s）`);
  }

  // ── 漂移趋势 ──
  L.push('');
  L.push('【四、零漂趋势（最小二乘拟合零偏随时间的变化）】');
  const trows = [['轴', '斜率(deg/s per h)', '斜率(°/h per h)', '起始零偏(deg/s)', '起始零偏(°/h)', 'R²']];
  const fits = {};                       // 结论段要用，这里存下来
  for (const ax of ['x', 'y', 'z']) {
    const ts = [], ys = [];
    P.forEach((p, i) => { if (isFinite(p[ax])) { ts.push(T[i]); ys.push(p[ax] * RAD_TO_DEG); } });
    const f = linfit(ts, ys);
    if (!f) continue;
    fits[ax] = f;
    // 注意：linfit 喂进去的 ys 已经是【deg/s】（不是 rad/s），所以这里只剩纯时间换算：
    //   deg/s → °/h 乘 3600；per s → per h 再乘 3600。
    //   之前写成 slope*RAD_TO_DEG_PER_H*3600，等于又多乘了一个 180/π（57.3 倍）。
    trows.push([ax.toUpperCase(), fmtSci(f.slope * 3600), fmtSigned(f.slope * 3600 * 3600, 1),
      fmtSci(f.intercept), fmtSigned(f.intercept * 3600, 3), f.r2.toFixed(3)]);
  }
  L.push(...padRows(trows));
  L.push('  （R² 越接近 1，说明零偏随时间线性漂移越明显；接近 0 则基本是随机抖动）');

  // ── 量级评估 ──
  const biases = Object.values(stat).map(s => Math.abs(s.m * RAD_TO_DEG_PER_H));
  if (biases.length) {
    const mx = Math.max(...biases);
    L.push('');
    L.push('【五、量级参考】');
    L.push(`  当前最大零偏 ${(mx / 3600).toExponential(3)} deg/s = ${mx.toFixed(2)} °/h`);
    L.push(`  ${biasGrade(mx)}`);
    L.push('  注：以上只是粗略量级，不是严格的器件等级判定（严格判定需 Allan 方差 + 温控）。');
    if (mx < 30) {
      // 只有量级小到一定程度，地球自转才不会被噪声淹没，这时才值得提
      L.push('  提示：这个量级已接近地球自转（15.04 °/h × sin(纬度)），高精度陀螺静止时读到的');
      L.push('        "零偏"里本来就含这一项，属正常物理现象，不是器件坏了。');
    }
  }

  // ── 六、结论 ──
  if (Object.keys(stat).length) {
    let worstAx = null, worstV = -1;
    for (const ax of ['x', 'y', 'z']) {
      const s = stat[ax]; if (!s) continue;
      const v = Math.abs(s.m * RAD_TO_DEG_PER_H);
      if (v > worstV) { worstV = v; worstAx = ax; }
    }
    const mx = worstV;
    // 移动机器人最关心的其实是"航向会漂多少"，把 °/h 换算成更直观的 °/min
    const degPerMin = mx / 60;
    const sdMax = Math.max(...Object.values(stat).map(s => Math.abs(s.sd * RAD_TO_DEG_PER_H)));

    L.push('');
    L.push('【六、结论】');
    L.push(`  1) 零偏量级：最大 ${mx.toFixed(1)} °/h（${worstAx.toUpperCase()} 轴 = ${(mx / 3600).toExponential(3)} deg/s）→ ${biasGrade(mx)}`);
    L.push(`  2) 实际影响：若不补偿，航向约以 ${degPerMin.toFixed(2)} °/min 漂移 ——` +
           ` 1 分钟偏 ${degPerMin.toFixed(1)}°，10 分钟偏 ${(degPerMin * 10).toFixed(1)}°`);
    L.push(`  3) 稳定性：1σ 最大 ${sdMax.toFixed(1)} °/h → ${biasStabilityGrade(sdMax)}`);

    const r2s = Object.keys(fits).map(ax => ({ ax, r2: fits[ax].r2 }));
    if (r2s.length) {
      const top = r2s.reduce((a, b) => (b.r2 > a.r2 ? b : a));
      if (P.length < 4) {
        L.push(`  4) 漂移趋势：只有 ${P.length} 个采样点，R² 不可靠（两点拟合必然 R²=1），暂不判断线性漂移`);
      } else if (top.r2 > 0.8) {
        L.push(`  4) 漂移趋势：${top.ax.toUpperCase()} 轴 R²=${top.r2.toFixed(3)}，随时间线性漂移明显` +
               ' —— 典型温漂特征，建议查温度');
      } else {
        L.push(`  4) 漂移趋势：最高 R² 仅 ${top.r2.toFixed(3)}（${top.ax.toUpperCase()} 轴），` +
               '未见明显线性漂移，更像随机抖动');
      }
    }

    let verdict, advice;
    if (mx < 10) {
      verdict = '零偏很小，日常使用无需特别处理';
      advice = '保持定期标定即可。';
    } else if (mx < 50) {
      verdict = '零偏处于常见 MEMS 水平，可接受';
      advice = '若要做较长时间的纯惯导推算，建议启动时静止 5~10 秒取均值做零偏扣除。';
    } else if (mx < 200) {
      verdict = '零偏偏大';
      advice = '建议：① 上电静止时做零偏标定并扣除；② 冷机与运行 30 分钟后各采一组对比，' +
               '判断是不是温漂；③ 检查 IMU 安装是否牢固、附近有无热源或持续震动。';
    } else {
      verdict = '零偏过大，不建议用于长时间航向推算';
      advice = '优先排查：温漂、安装松动 / 结构应力、电源噪声；必要时更换或返厂标定。';
    }
    L.push(`  → 总体：${verdict}`);
    L.push(`  → 建议：${advice}`);
    L.push('  前提：以上基于「打点处车辆确实静止、且所选字段就是角速度」。若那几段其实在动，');
    L.push('        或选的是未做零偏标定的原始量，结论会偏大 —— 先看 A1 曲线确认打点段是平的。');
  }

  pre.textContent = L.join('\n');
  // CSV
  const csv = ['idx,t_rel_s,wall_clock,x_rad_s,y_rad_s,z_rad_s,x_deg_s,y_deg_s,z_deg_s,x_deg_per_h,y_deg_per_h,z_deg_per_h,n_samples'];
  P.forEach((p, i) => {
    csv.push([i + 1, T[i].toFixed(3), fmtRosWall(p.tNs),
      isFinite(p.x) ? p.x : '', isFinite(p.y) ? p.y : '', isFinite(p.z) ? p.z : '',
      isFinite(p.x) ? (p.x * RAD_TO_DEG).toFixed(9) : '', isFinite(p.y) ? (p.y * RAD_TO_DEG).toFixed(9) : '', isFinite(p.z) ? (p.z * RAD_TO_DEG).toFixed(9) : '',
      isFinite(p.x) ? (p.x * RAD_TO_DEG_PER_H).toFixed(4) : '', isFinite(p.y) ? (p.y * RAD_TO_DEG_PER_H).toFixed(4) : '', isFinite(p.z) ? (p.z * RAD_TO_DEG_PER_H).toFixed(4) : '',
      Math.max(p.nx || 0, p.ny || 0, p.nz || 0)].join(','));
  });
  pre._csv = csv.join('\n');
}

function renderDriftAll() {
  renderDriftState();
  renderDriftTable();
  renderDriftFigure();
  renderDriftReport();
}

// ═══════════════ 区块拖动排序 ═══════════════
/**
 * 图和报告块都挂在 #figs 下（CSS grid），DOM 顺序 = 视觉顺序，
 * 所以拖动只要改 DOM 顺序就行，不用管布局。
 *
 * 为什么靠「手柄」而不是整块可拖：canvas 上本来就有框选放大 / 右键平移，
 * 整块 draggable=true 会把"拖框放大"变成"把整张图拖走"。
 * 所以默认不可拖，只有按住标题栏左侧的 ⠿ 时才临时打开。
 */
const FIG_ORDER_KEY = 'imuweb.figOrder.v1';
const FIG_ORDER_DEF = ['A0', 'A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'A9', 'R5', 'R6'];
let _dragEl = null;

function figKeyOf(el) {
  if (!el || el.nodeType !== 1) return null;
  if (el.classList && el.classList.contains('fig')) return el.dataset.fid || null;
  if (el.tagName === 'DETAILS') return el.dataset.fid || null;
  return null;
}

function figHost() { return document.getElementById('figs'); }

function figBlocks() {
  const host = figHost();
  return host ? [...host.children].filter(e => figKeyOf(e)) : [];
}

/** 按给定 key 顺序重排；没出现在 key 里的块按原有相对顺序补到末尾 */
function reorderBlocks(keys) {
  const host = figHost();
  if (!host) return;
  const map = new Map();
  for (const el of [...host.children]) { const k = figKeyOf(el); if (k) map.set(k, el); }
  for (const k of keys) { const el = map.get(k); if (el) { host.appendChild(el); map.delete(k); } }
  for (const el of map.values()) host.appendChild(el);
}

function applyFigOrder() {
  let keys = FIG_ORDER_DEF;
  try {
    const s = JSON.parse(localStorage.getItem(FIG_ORDER_KEY) || '[]');
    if (Array.isArray(s) && s.length) keys = s;
  } catch (e) { /* 读不到就用默认 */ }
  reorderBlocks(keys);
}

function saveFigOrder() {
  try { localStorage.setItem(FIG_ORDER_KEY, JSON.stringify(figBlocks().map(figKeyOf))); } catch (e) {}
}

function resetFigOrder() {
  try { localStorage.removeItem(FIG_ORDER_KEY); } catch (e) {}
  reorderBlocks(FIG_ORDER_DEF);
}

/**
 * 把 key 对应的块上移(-1) / 下移(+1) 一格，返回是否真的动了。
 * 拖动之外的兜底：触控板拖 HTML5 drag 很容易失败，按钮点一下必然生效。
 */
function moveBlock(key, dir) {
  const host = figHost();
  if (!host) return false;
  const blocks = figBlocks();
  const i = blocks.findIndex(e => figKeyOf(e) === key);
  if (i < 0) return false;
  const j = i + (dir < 0 ? -1 : 1);
  if (j < 0 || j >= blocks.length) return false;      // 已在两端
  const el = blocks[i], ref = blocks[j];
  // 下移：把 ref 挪到 el 前面（等价于 el 落到 ref 后面）
  // 上移：把 el 挪到 ref 前面
  if (dir < 0) host.insertBefore(el, ref); else host.insertBefore(ref, el);
  saveFigOrder();
  redrawAfterReorder();
  return true;
}

/** 顺序变了宽度可能变（A0 占整行，其余半行），图要按新宽度重画 */
function redrawAfterReorder() {
  for (const f of (S.figs || [])) { try { f.draw(); } catch (e) {} }
  if (S._driftFig) { try { S._driftFig.draw(); } catch (e) {} }
}

/**
 * 在图头 / summary 里插一对 ↑↓ 按钮。
 * 为什么不放 figbtns（右侧那组）：那里是"+/−/复位"，跟顺序无关，混在一起不好找；
 * 放标题左边紧贴 ⠿，语义上是"这一块的移动控件"。
 */
function appendMoveBtns(box, key, afterHandle) {
  if (!box || !document.createElement) return;
  // 插在手柄之后：每次都以"上一个插好的按钮"为锚点，否则第二个会插到第一个前面（↓↑ 反了）
  let anchor = afterHandle && box.querySelector ? box.querySelector('.dragh') : null;
  for (const t of [['↑', '上移一位', -1], ['↓', '下移一位', 1]]) {
    const b = document.createElement('button');
    b.className = 'mvbtn';
    b.textContent = t[0];
    b.title = t[1];
    b.dataset.mv = String(t[2]);
    // pointerdown 冒泡到 host 会把块设成 draggable，点按钮不该触发拖动
    b.addEventListener('pointerdown', e => { if (e.stopPropagation) e.stopPropagation(); });
    // 在 <summary> 里点按钮会顺带把 details 折叠/展开，必须拦掉
    b.onclick = e => {
      if (e && e.preventDefault) e.preventDefault();
      if (e && e.stopPropagation) e.stopPropagation();
      moveBlock(key, t[2]);
    };
    if (anchor && anchor.parentNode === box) box.insertBefore(b, anchor.nextSibling);
    else box.appendChild(b);
    anchor = b;
  }
}

/** 找落点：先看指针压在哪个块上，按左右半边决定插前面还是后面；都不在就取中心最近的 */
function nearestSlot(x, y) {
  const blocks = figBlocks().filter(e => e !== _dragEl);
  if (!blocks.length) return null;
  for (const el of blocks) {
    const r = el.getBoundingClientRect();
    if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
      return { el, after: x > r.left + r.width / 2 };
    }
  }
  let best = blocks[0], bestD = Infinity;
  for (const el of blocks) {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const d = (x - cx) ** 2 + (y - cy) ** 2;
    if (d < bestD) { bestD = d; best = el; }
  }
  const r = best.getBoundingClientRect();
  return { el: best, after: x > r.left + r.width / 2 };
}

function endFigDrag() {
  const host = figHost();
  if (_dragEl) { _dragEl.classList.remove('dragging'); _dragEl.draggable = false; }
  _dragEl = null;
  if (host) host.querySelectorAll('.dropHint').forEach(x => x.classList.remove('dropHint'));
  saveFigOrder();
}

function initSortable() {
  const host = figHost();
  if (!host) return;
  // details 没有 .fighead，给 summary 前面插一个同样的手柄
  [...host.children].forEach(d => {
    if (d.tagName !== 'DETAILS') return;
    const sm = d.querySelector('summary');
    if (!sm || sm.querySelector('.dragh')) return;
    const h = document.createElement('span');
    h.className = 'dragh'; h.textContent = '⠿'; h.title = '按住拖动可调整显示顺序';
    sm.insertBefore(h, sm.firstChild);
    appendMoveBtns(sm, d.dataset.fid, true);
  });

  // 按住手柄才允许拖动（pointerdown 在 dragstart 之前触发）
  host.addEventListener('pointerdown', e => {
    const h = e.target && e.target.closest ? e.target.closest('.dragh') : null;
    if (!h) return;
    const blk = h.closest('.fig, details');
    if (blk) blk.draggable = true;
  });
  host.addEventListener('pointerup', () => {
    [...host.children].forEach(b => { if (b.draggable && b !== _dragEl) b.draggable = false; });
  });

  host.addEventListener('dragstart', e => {
    const blk = e.target && e.target.closest ? e.target.closest('.fig, details') : null;
    if (!blk || !figKeyOf(blk)) { e.preventDefault(); return; }
    _dragEl = blk;
    blk.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', figKeyOf(blk)); } catch (err) {}
  });

  // 实时移动（grid 里比"放手才跳"直观得多），落点变化时只做一次 insertBefore
  host.addEventListener('dragover', e => {
    if (!_dragEl) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const slot = nearestSlot(e.clientX, e.clientY);
    if (!slot) return;
    if (slot.after) {
      const nx = slot.el.nextSibling;
      if (nx !== _dragEl) host.insertBefore(_dragEl, nx);
    } else if (slot.el !== _dragEl) {
      host.insertBefore(_dragEl, slot.el);
    }
  });

  host.addEventListener('drop', e => { if (_dragEl) { e.preventDefault(); endFigDrag(); } });
  host.addEventListener('dragend', endFigDrag);

  const btn = document.getElementById('btnSortReset');
  if (btn) btn.onclick = () => {
    resetFigOrder();
    for (const f of S.figs) f.draw();
  };
}

// ═══════════════ UI ═══════════════

function setStatus(msg, cls) {
  const el = $('stat');
  el.textContent = msg;
  el.style.color = cls === 'err' ? 'var(--err)' : 'var(--text)';
}

function applyRange() {
  _a0SeriesCache.clear();      // 区间变了，A0 的序列要重取
  let a = parseFloat($('startIn').value);
  let b = parseFloat($('endIn').value);
  if (!isFinite(a)) a = 0;
  if (!isFinite(b)) b = S.dur;
  a = Math.max(0, Math.min(S.dur, a));
  b = Math.max(0, Math.min(S.dur, b));
  if (b - a < 0.01) b = Math.min(S.dur, a + 0.01);
  S.range = [a, b];
  $('startIn').value = a.toFixed(2);
  $('endIn').value = b.toFixed(2);
  drawRangeBar();
  runAnalysis();
}

function init() {
  $('ver').textContent = 'v' + APP_VERSION;
  initSortable();          // 给报告块装手柄 + 绑定拖动事件
  applyFigOrder();         // 恢复上次保存的顺序

  $('btnOpen').onclick = () => $('fileInput').click();
  $('fileInput').onchange = e => { if (e.target.files[0]) openFile(e.target.files[0]); e.target.value = ''; };
  ['dragover', 'dragenter'].forEach(ev => window.addEventListener(ev, e => { e.preventDefault(); document.body.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach(ev => window.addEventListener(ev, e => { e.preventDefault(); document.body.classList.remove('dragover'); }));
  window.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f) openFile(f); });

  // 线宽：整体缩放所有曲线的粗细（各条线之间的相对关系保留）
  $('lwSel').onchange = () => {
    setLineScale(parseFloat($('lwSel').value) || 1);
    for (const f of S.figs) f.draw();
  };
  setLineScale(parseFloat($('lwSel').value) || 1);

  $('btnApply').onclick = applyRange;

  // A0 归一化开关（以 checkbox 为唯一事实来源，避免与 S.a0.norm 初值不一致）
  S.a0.norm = $('normChk').checked;
  $('normChk').onchange = () => { S.a0.norm = $('normChk').checked; rebuildA0(); };
  $('btnFields').onclick = () => toggleFieldMenu();
  bindFieldMenuOutside();
  $('cbClear').onclick = () => clearCursor();
  document.addEventListener('keydown', e => { if (e.key === 'Escape') clearCursor(); });

  // A7 / A8
  $('rawPrev').onclick = () => stepRaw(-1);
  $('rawNext').onclick = () => stepRaw(1);
  $('rawCopy').onclick = async () => {
    const pre = $('rawJson');
    const topic = $('rawTopicSel').value;
    const e = (S.bag && topic && S.cursorNs != null) ? S.bag.latestBefore(topic, S.cursorNs) : null;
    const m = e ? S.bag.decode(e) : null;
    const txt = m ? msgToJson(m) : (pre ? pre.textContent : '');
    const ok = await copyText(txt);
    flash($('rawInfo'), ok ? '已复制到剪贴板' : '复制失败，请手动选中下方内容复制');
  };
  $('cmpCopy').onclick = async () => {
    const ok = await copyText($('cmpTable')._csv || $('cmpTable').textContent);
    flash($('cmpInfo'), ok ? '已复制 CSV' : '复制失败，请手动选中下方内容复制');
  };
  $('btnAll').onclick = () => { S.range = [0, S.dur]; $('startIn').value = '0'; $('endIn').value = S.dur.toFixed(2); drawRangeBar(); runAnalysis(); };
  $('btnResetAll').onclick = () => { for (const f of S.figs) { for (const a of f.axes) a.reset(); f.draw(); } };

  // ── A9 零漂分析 ──
  if ($('driftGroup')) $('driftGroup').onchange = applyDriftGroup;
  if ($('driftSelX')) $('driftSelX').onclick = () => openDriftPicker('x');
  if ($('driftSelY')) $('driftSelY').onclick = () => openDriftPicker('y');
  if ($('driftSelZ')) $('driftSelZ').onclick = () => openDriftPicker('z');
  if ($('driftPick')) $('driftPick').onclick = () => {
    const err = driftAddPick();
    if (err) flash($('driftInfo'), err);
  };
  if ($('driftUndo')) $('driftUndo').onclick = driftRemoveLast;
  if ($('driftClear')) $('driftClear').onclick = driftClearPicks;
  if ($('driftDone')) $('driftDone').onclick = driftToggleDone;
  if ($('driftCsv')) $('driftCsv').onclick = async () => {
    const ok = await copyText($('driftReport')._csv || $('driftReport').textContent);
    flash($('driftInfo'), ok ? '已复制 CSV' : '复制失败，请手动选中下方内容复制');
  };
  if ($('driftWin')) $('driftWin').onchange = () => { renderDriftAll(); };
  // 点面板外关闭字段选择器
  document.addEventListener('pointerdown', e => {
    const m = $('driftMenu');
    if (!m || m.style.display === 'none') return;
    if (m.contains(e.target)) return;
    if (e.target && e.target.closest && e.target.closest('#driftBar')) return;
    m.style.display = 'none';
  });
  renderDriftAll();

  // 区间条拖选
  const cv = $('rangeBar');
  const toT = e => {
    const r = cv.getBoundingClientRect();
    return Math.max(0, Math.min(S.dur, (e.clientX - r.left) / r.width * S.dur));
  };
  cv.addEventListener('pointerdown', e => {
    if (!S.bag) return;
    const t = toT(e);
    rb.drag = { a: t, b: t };
    cv.setPointerCapture(e.pointerId);
  });
  cv.addEventListener('pointermove', e => {
    if (!rb.drag || !S.bag) return;
    rb.drag.b = toT(e);
    const a = Math.min(rb.drag.a, rb.drag.b), b = Math.max(rb.drag.a, rb.drag.b);
    S.range = [a, b];
    $('startIn').value = a.toFixed(2);
    $('endIn').value = b.toFixed(2);
    drawRangeBar();
  });
  cv.addEventListener('pointerup', () => {
    if (!rb.drag) return;
    const a = Math.min(rb.drag.a, rb.drag.b), b = Math.max(rb.drag.a, rb.drag.b);
    rb.drag = null;
    if (b - a < S.dur * 0.005) return;    // 误点，忽略
    S.range = [a, b];
    runAnalysis();
  });

  window.addEventListener('resize', () => {
    drawRangeBar();
    for (const f of S.figs) f.draw();
    if (S._driftFig) S._driftFig.draw();
  });
}

document.addEventListener('DOMContentLoaded', init);
window.__imu = { S, getLineScale, niceStep, setCursorNs, clearCursor, attachCursor, bindCursors, renderCursorBar, flatNums, topicFields, getA0Series,
  toggleA0, buildA0Figure, rebuildA0, a0Key, renderFieldMenu, analyzeTopicFrequency, unwrapYaw, percentile, frameYawDelta, analyzeYawDelta, frameYawRate, interp, histogram, wrapDeg, quatToYaw, extract, runAnalysis, openFile,
  fmtRosWall, msgToJson, hlJson, buildRawTopicSelector, rawNeighborEntry, renderRawMsg, renderCompare, stepRaw,
  // A9 零漂
  detectGyroGroups, driftSampleAt, driftAddPick, driftRemoveLast, driftClearPicks, driftToggleDone,
  renderDriftAll, renderDriftTable, renderDriftReport, renderDriftFigure, renderDriftState,
  driftBase, linfit, biasGrade, biasStabilityGrade, padRows, fmtSigned, fmtSci, buildDriftGroups, applyDriftGroup, openDriftPicker,
  RAD_TO_DEG_PER_H, GYRO_FIELD_RE,
  // 拖动排序
  figKeyOf, figBlocks, reorderBlocks, applyFigOrder, saveFigOrder, resetFigOrder, nearestSlot,
  moveBlock, appendMoveBtns, redrawAfterReorder,
  FIG_ORDER_DEF, FIG_ORDER_KEY };
