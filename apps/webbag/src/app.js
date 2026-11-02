/**
 * app.js —— 感知导航bag分析工具主逻辑
 * 依赖: bag.js（零依赖解析器）
 * 渲染全部用 Canvas 2D，不需要 three.js
 */
import { Bag, FileSource, TFTree, compose2D, apply2D, yawOf } from './bag.js';

const $ = id => document.getElementById(id);
const cv = $('cv'), ctx = cv.getContext('2d');
const tlcv = $('tlcv'), tlctx = tlcv.getContext('2d');
const view = $('view');

// ═══════════════ 全局状态 ═══════════════
const S = {
  bag: null,
  fileName: '',
  roles: new Map(),      // topic -> 角色
  types: new Map(),      // topic -> 消息类型
  topics: [],            // 排序后的话题列表
  visible: new Set(),    // 勾选显示的话题
  playing: false,
  t: 0,                  // 当前时刻(ns)
  speed: 1,
  map: null,             // 地图位图
  traj: null,            // 轨迹 [{x,y,th,t}]（已换算到地图坐标系）
  poseTopic: null,
  selected: null,        // 选中的消息条目
  selTopic: null,
  lastT: 0,
  // ── 地图要素（v3 map_info）──
  mf: null,               // 解析结果缓存 {virtual,carpet,slope,lowth,marker}
  mfEntry: null,          // 这份结果对应的消息，换了才重新解析
  mfStat: null,           // 各类数量统计
  mfLayer: {              // 各层显示开关
    virtual: true, carpet: true, slope: true, lowth: true,
    marker: true, name: true,
  },
  // ── 坐标系 ──
  tf: null,              // TFTree
  mapFrame: 'map',       // 地图所在坐标系名
  tfOn: true,            // 是否启用 TF 换算
  tfNote: '',            // 叠加信息里显示的 TF 状态
  tfCache: new Map(),    // 查询缓存
  tfWarned: new Set(),   // 已经警告过的帧，避免刷屏
  trajFrame: null,       // 位姿话题原始所在坐标系
  poseTopics: [],        // 所有可选位姿话题
  yawOffset: 0,          // 车辆图标朝向的额外偏移（弧度）
  srcMode: 'auto',       // auto | tf | odom | raw
  offX: 0, offY: 0, offT: 0,   // 手动微调（米 / 米 / 弧度）
  diag: '',              // 位姿诊断信息
  cams: [],              // 相机面板 [{topic, el, cv, ctx, lbl, err, entry}]
  lastCamT: 0,           // 相机上次刷新时刻（播放时节流用）
  chart: null,           // 曲线数据 {topic, rows, fields}
  chartMsg: '',         // 曲线面板为空时显示的原因
  series: [],           // 曲线序列 [{topic, field, pts, min, max, changed, on}]
  status: [],           // 状态条条目 [{topic, path, label, el, chip}]
  color: new Map(),     // 话题 -> 绘制颜色
  userColor: new Map(), // 用户手动改过颜色的话题（重新配色时不覆盖）
  dash: new Map(),      // 路径话题 -> 线型
  showLegend: true,     // 地图上是否画图层图例
  tlZoom: 1,            // 时间轴横向放大倍数（1=看全程）
  tlView0: 0,           // 时间轴可视窗口起点（秒，相对 bag 开头）
  chZoom: 1,            // 曲线横向放大倍数
  chView0: 0,           // 曲线可视窗口起点（秒）
  hoverT: null,         // 曲线上鼠标悬停的时间
  followTl: true,       // 放大后播放时是否自动跟随播放头
};

/** 界面上显示的版本号（标题旁边） */
const APP_VERSION = '1.5';

// ═══════════════ 类型归一化 & 角色识别 ═══════════════
// rosbags 读出的是 ROS2 风格 pkg/msg/Type，bag 文件里存 ROS1 风格 pkg/Type，两种都认
function norm(t) { return (t || '').replace('/msg/', '/'); }

function detectRole(type, topic) {
  const T = norm(type);
  if (T === 'nav_msgs/OccupancyGrid') return 'map';
  // 地图要素话题（v3_msgs/MapInfoV3 这类自定义类型）：虚拟墙/禁行区/地毯/标记点等
  if (/MapInfo/i.test(T) || /map_info/i.test(topic || '')) return 'mapinfo';
  if (T === 'sensor_msgs/LaserScan') return 'scan';
  if (T === 'nav_msgs/Odometry' || T === 'geometry_msgs/PoseStamped'
      || T === 'geometry_msgs/PoseWithCovarianceStamped') return 'pose';
  if (T === 'sensor_msgs/PointCloud2') return 'cloud';
  if (T === 'nav_msgs/Path') return 'path';
  if (T === 'tf2_msgs/TFMessage') return 'tf';
  if (T === 'visualization_msgs/MarkerArray') return 'marker';
  if (T === 'sensor_msgs/Image' || T === 'sensor_msgs/CompressedImage') return 'image';
  return 'data';
}
const ROLE_LABEL = { map: '地图', mapinfo: '地图要素', path: '路径', scan: '激光', pose: '位姿', cloud: '点云', tf: 'TF', marker: '标记', image: '图像', data: '数据' };

/** 打开 bag 后默认勾选的话题。字符串=精确匹配，正则=模糊 */
const DEFAULT_TOPICS = [
  '/scan_raw',
  '/map',
  '/odom_origin',
  '/targets_source',
  '/debug_cleaning_path',
  '/debug_replaned_path',
  '/debug_transform_path',
  '/debug_footprint',
];

/**
 * 指定话题的固定颜色（覆盖自动分配），字符串=精确匹配，正则=模糊。
 * 有些图层看惯了某个颜色，写死比每次变更好认。
 */
const TOPIC_COLOR_FIX = [
  { test: /^\/scan_raw$/, color: '#ffb300' },      // 激光点云：橙黄
  { test: /^\/debug_footprint$/, color: '#ff5252' }, // 车辆边框：红
];
function fixedColor(topic) {
  for (const f of TOPIC_COLOR_FIX) {
    const hit = typeof f.test === 'string' ? f.test === topic : f.test.test(topic);
    if (hit) return f.color;
  }
  return null;
}
/** 重新算出某个话题的默认颜色（右键"恢复默认"时用） */
function defaultColorFor(topic) {
  const roles = ['map', 'mapinfo', 'path', 'scan', 'cloud', 'pose', 'marker', 'image', 'tf', 'data'];
  const role = S.roles.get(topic) || 'data';
  let pi = 0;
  for (const r of roles) {
    for (const t of S.topics) {
      if (S.roles.get(t) !== r) continue;
      if (!fixedColor(t)) pi++;
      if (t === topic) return fixedColor(t) || (fixedColor(t) ? TOPIC_PALETTE[0] : TOPIC_PALETTE[(pi - 1) % TOPIC_PALETTE.length]);
    }
  }
  return ROLE_COLOR[role] || '#8d6e63';
}
const ROLE_COLOR = { map: '#00bfa5', mapinfo: '#ff7043', path: '#ffb300', scan: '#ffb300', pose: '#42a5f5', cloud: '#ab47bc', tf: '#78909c', marker: '#ef5350', image: '#66bb6a', data: '#8d6e63' };

/**
 * 每个话题一个颜色，避免"点云和路径全是一个色看不清"。
 * 按角色分组依次取色，同类话题之间保证不同。
 */
const TOPIC_PALETTE = [
  '#ffb300', '#42a5f5', '#ab47bc', '#26c6da', '#ef5350', '#9ccc65',
  '#ffa726', '#ec407a', '#5c6bc0', '#26a69a', '#d4e157', '#8d6e63',
  '#78909c', '#7e57c2', '#66bb6a', '#29b6f6',
];
/** 路径用不同线型再区分一层（颜色之外多一个维度） */
const DASH_STYLES = [[], [7, 4], [2, 3], [10, 3, 2, 3]];

// ═══════════════ 打开文件 ═══════════════
async function openFile(file) {
  $('stat').textContent = '读取中…';
  $('stat').style.color = 'var(--warn)';
  try {
    const bag = new Bag(new FileSource(file));
    await bag.open();
    const n = await bag.loadAll(c => { $('stat').textContent = `读取中… ${c} 条`; });
    if (!n) throw new Error('bag 里没有消息');

    S.bag = bag;
    S.fileName = file.name;

    // 建立 topic -> 角色 / 类型
    S.roles.clear(); S.types.clear(); S.visible.clear();
    for (const c of bag.connections.values()) {
      S.types.set(c.topic, c.type);
      const r = detectRole(c.type, c.topic);
      // 同一 topic 可能有多个 connection，保留"最具体"的角色
      if (!S.roles.has(c.topic) || S.roles.get(c.topic) === 'data') S.roles.set(c.topic, r);
    }
    S.topics = bag.topics();
    // 给每个话题分配一个颜色：按角色分组依次取，同类之间一定不同
    S.color.clear();
    {
      let pi = 0;
      for (const role of ['map', 'mapinfo', 'path', 'scan', 'cloud', 'pose', 'marker', 'image', 'tf', 'data']) {
        for (const t of S.topics) {
          if (S.roles.get(t) !== role) continue;
          // 固定颜色优先，其余从调色板依次取
          const fx = fixedColor(t);
          if (!fx) pi++;                       // 占住序号，避免固定色打乱其余配色
          S.color.set(t, fx || TOPIC_PALETTE[(pi - 1) % TOPIC_PALETTE.length]);
        }
      }
      // 用户手动改过的优先级最高（同一个 bag 重新渲染时保留）
      for (const [t, c] of S.userColor) if (S.topics.includes(t)) S.color.set(t, c);
      // 路径再记一个线型序号，实线/虚线/点线轮换
      let di = 0;
      for (const t of S.topics) if (S.roles.get(t) === 'path') S.dash.set(t, DASH_STYLES[di++ % DASH_STYLES.length]);
    }
    // 默认勾选：优先用白名单（常用的一套），其余按需手动开。
    // 白名单一个都没命中时（换了别的机器人）退回按角色勾选。
    const hit = (p, t) => (typeof p === 'string' ? p === t : p.test(t));
    const picked = DEFAULT_TOPICS.filter(p => S.topics.some(t => hit(p, t)));
    if (picked.length) {
      for (const t of S.topics) {
        if (picked.some(p => hit(p, t))) S.visible.add(t);
      }
    } else {
      for (const t of S.topics) {
        const r = S.roles.get(t);
        if (r === 'map' || r === 'pose' || r === 'scan') S.visible.add(t);
      }
    }
    // 地图要素话题默认勾上：它跟 /map 一样属于地图本体，
    // 不勾的话用户打开 bag 看不到虚拟墙/标记点，还以为工具没做这个功能。
    for (const t of S.topics) if (S.roles.get(t) === 'mapinfo') S.visible.add(t);

    // 位姿候选：Odometry / PoseStamped / PoseWithCovarianceStamped
    S.poseTopics = S.topics.filter(t => S.roles.get(t) === 'pose');
    // 优先 nav_msgs/Odometry
    S.poseTopic = S.poseTopics.find(t => norm(S.types.get(t)) === 'nav_msgs/Odometry')
               || S.poseTopics[0] || null;
    // 位姿话题自动选（界面上的下拉已移除）：优先 nav_msgs/Odometry

    // 换 bag 了，清掉上一个 bag 的地图要素缓存，避免残留
    S.mf = null; S.mfEntry = null; S.mfStat = null; S.mfSig = ''; S.mfWarned = false;

    await buildMap();
    buildTF();           // 必须在 buildTraj 之前：轨迹要换算到地图坐标系
    buildTraj();
    buildCams();

    // 状态条 + 曲线：自动挑几条，省得面板空着
    _seriesCache.clear();
    buildStatusDefaults();
    renderStatus();
    autoPickSeries();

    S.t = bag.startTime;
    S.selected = null;
    // 自动选一个"关注话题"，右下角内容面板跟着当前时刻走。
    // 优先挑状态/数据类的（能看出变化的），没有就用第一个。
    S.selTopic = S.topics.find(t => S.roles.get(t) === 'data')
              || S.topics.find(t => S.roles.get(t) === 'scan')
              || S.topics[0] || null;

    renderTopicMenu();
    renderStatMenu();
    renderChartMenu();
    renderMfMenu();
    renderTimeline();
    fitView();
    draw();
    updateStatusValues();

    $('hint').style.display = 'none';
    $('stat').textContent = `${file.name} · ${n} 条消息 · ${S.topics.length} 个话题 · 压缩:${bag.compression}`;
    $('stat').style.color = 'var(--text)';
    ['btnPlay', 'btnFit', 'speed', 'btnTf', 'btnLegend',
     'btnTopics', 'btnAddStat', 'btnAddChart', 'btnClearChart', 'btnRec', 'recMode', 'btnMf',
     'btnTlReset', 'btnChReset']
      .forEach(id => $(id).disabled = false);
  } catch (e) {
    $('stat').textContent = '失败: ' + e.message;
    $('stat').style.color = 'var(--err)';
    console.error(e);
  }
}

// ═══════════════ 地图 ═══════════════
async function buildMap() {
  S.map = null;
  const mt = S.topics.find(t => S.roles.get(t) === 'map');
  if (!mt) return;
  const e = S.bag.latestBefore(mt, S.bag.endTime) || S.bag.entries.find(x => x.topic === mt);
  if (!e) return;
  const m = S.bag.decode(e);
  if (!m || !m.info || !m.data) return;
  const w = m.info.width, h = m.info.height, data = m.data;
  if (!w || !h || data.length < w * h) return;

  const img = new ImageData(w, h);
  for (let r = 0; r < h; r++) {
    const src = (h - 1 - r) * w;                 // ROS 原点在左下，画布原点在左上 -> 翻转
    for (let c = 0; c < w; c++) {
      const v = data[src + c];
      const g = v < 0 ? 128 : (v === 0 ? 255 : Math.max(0, 255 - v * 2.55));
      const o = (r * w + c) * 4;
      img.data[o] = img.data[o + 1] = img.data[o + 2] = g;
      img.data[o + 3] = 255;
    }
  }
  const c2 = document.createElement('canvas');
  c2.width = w; c2.height = h;
  c2.getContext('2d').putImageData(img, 0, 0);
  S.map = { canvas: c2, w, h, res: m.info.resolution,
            ox: m.info.origin.position.x, oy: m.info.origin.position.y };
  // 地图自身所在的坐标系名 —— 所有数据最终都要换算到这个系
  if (m.header && m.header.frame_id) S.mapFrame = m.header.frame_id;
}

// ═══════════════ 地图要素（v3_msgs/MapInfoV3）═══════════════
/**
 * 从 map_info_v3 里读出虚拟墙/禁行区/地毯区/斜坡区/低门槛/标记点，画到地图上。
 *
 * 为什么类型要写成常量表：bag 里这些字段只是 int16 数字，没有名字，
 * 不对照 .msg 定义根本看不出 10 到底是虚拟墙还是禁行区。
 *
 * 为什么几何要做"字段名探测"：LineData / CircleData / RectangleData /
 * PolygonData 的具体字段没给，不同版本写法可能不一样（points 数组、
 * start+end、center+radius…）。这里按优先级试探，取不到就跳过，
 * 保证"认得出的都能画出来"，而不是整块不显示。
 */

/** VirtualInfo.virtual_data_type —— 数据的语义（决定颜色） */
const VTYPE = {
  0:  { name: '未知',     color: '#78909c', fill: 'rgba(120,144,156,.18)' },
  10: { name: '虚拟墙',   color: '#ff5252', fill: null,                   line: true, dash: [9, 5], lw: 2.6 },
  11: { name: '禁行区',   color: '#ff5252', fill: 'rgba(255,82,82,.20)' },
  12: { name: '自由区',   color: '#66bb6a', fill: 'rgba(102,187,106,.16)' },
  13: { name: '障碍区',   color: '#8d6e63', fill: 'rgba(141,110,99,.26)' },
  14: { name: '地毯区',   color: '#ffa726', fill: 'rgba(255,167,38,.20)' },
  15: { name: '斜坡区',   color: '#ab47bc', fill: 'rgba(171,71,188,.18)' },
  16: { name: '低门槛',   color: '#26c6da', fill: 'rgba(38,198,218,.18)' },
  17: { name: '无效区',   color: '#546e7a', fill: 'rgba(84,110,122,.24)' },
  18: { name: '低置信',   color: '#d4e157', fill: 'rgba(212,225,87,.16)' },
};
/** VirtualInfo.points_type —— 点集形状 */
const PT_LINE = 21, PT_CIRCLE = 22, PT_RECT = 23, PT_POLY = 24;
const PTYPE_NAME = { 21: '直线', 22: '圆形', 23: '矩形', 24: '多边形' };

/** MarkerPoint.type —— 标记点种类（决定图标与颜色） */
const MTYPE = {
  0:   { name: '充电桩',   color: '#4caf50', glyph: '⚡' },
  1:   { name: '停靠点',   color: '#42a5f5', glyph: 'P' },
  2:   { name: '反光板',   color: '#ffd54f', glyph: '◇' },
  3:   { name: '梯控桩',   color: '#26a69a', glyph: '⇅' },
  5:   { name: '巡检点',   color: '#ba68c8', glyph: '◎' },
  6:   { name: '二维码',   color: '#ff8a65', glyph: '▣' },
  100: { name: '其他',     color: '#90a4ae', glyph: '•' },
  999: { name: '未知',     color: '#78909c', glyph: '?' },
};

/** 三个来源都归到"虚拟数据"这一类（语义不同，但都是 VirtualInfo） */
const MF_GROUPS = [
  { key: 'virtual', label: '虚拟数据', field: 'virtual_info_list' },
  { key: 'carpet',  label: '地毯区',   field: 'carpetzone_info_list' },
  { key: 'slope',   label: '斜坡区',   field: 'slopezone_info_list' },
  { key: 'lowth',   label: '低门槛',   field: 'low_threshold_info_list' },
];

/**
 * 单个点 -> [x,y]。
 * 真实 msg 里点是 geometry_msgs/Pose：坐标在 pose.position 下面，
 * 不是 pose.x。两种都认，老的裸 {x,y} 写法才不会失效。
 */
function poseXY(p) {
  if (!p || typeof p !== 'object') return null;
  let x = p.x, y = p.y;
  if (typeof x !== 'number' && p.position && typeof p.position === 'object') {
    x = p.position.x; y = p.position.y;
  }
  if (typeof x !== 'number' || typeof y !== 'number') return null;
  if (!isFinite(x) || !isFinite(y)) return null;
  return [x, y];
}

/**
 * 取点数组。poses 排在第一个 —— 它是 LineData/CircleData/RectangleData/
 * PolygonData 共用的真实字段名（geometry_msgs/Pose[]），写死在最前面，
 * 别的文件名只是历史兼容。
 */
function pickPts(o) {
  if (!o || typeof o !== 'object') return null;
  for (const k of ['poses', 'points', 'pts', 'point', 'vertices', 'vertex',
                   'polygon_points', 'pose_list']) {
    const v = o[k];
    if (Array.isArray(v) && v.length) {
      const p = pts2xy(v);
      if (p.length) return p;
    }
  }
  // 兜底：找唯一一个「元素能取出 x/y」的数组（字段名完全没见过的版本）
  for (const k of Object.keys(o)) {
    const v = o[k];
    if (!Array.isArray(v) || !v.length) continue;
    if (v[0] && typeof v[0] === 'object') {
      const p = pts2xy(v);
      if (p.length) return p;
    }
  }
  return null;
}

/** 两点式线段：真实字段名是 start_pose/end_pose，其余为历史兼容 */
function pickPair(o) {
  if (!o || typeof o !== 'object') return null;
  for (const [a, b] of [['start_pose', 'end_pose'], ['pose_start', 'pose_end'],
                        ['start', 'end'], ['begin', 'end'], ['p1', 'p2'],
                        ['from', 'to'], ['point1', 'point2'], ['a', 'b'],
                        ['pose1', 'pose2']]) {
    const pa = poseXY(o[a]), pb = poseXY(o[b]);
    if (pa && pb) return [pa, pb];
  }
  return null;
}

/** 圆：真实字段名是 center_pose + radius */
function circleOf(o) {
  if (!o || typeof o !== 'object') return null;
  const ct = poseXY(o.center_pose) || poseXY(o.center) ||
             poseXY(o.pose) || poseXY(o.origin);
  const r = o.radius ?? o.r ?? o.radius_m;
  if (ct && typeof r === 'number' && isFinite(r) && r > 0) {
    return { kind: 'circle', center: ct, radius: r };
  }
  return null;
}

/**
 * 去掉首尾重复点。
 * 矩形/多边形常把起点在末尾再存一次用于闭合（示例里矩形就是 5 点），
 * 留着会画出一条零长度的边，去重后才是干净的 4 角。
 */
function dropDupTail(pts) {
  if (!pts || pts.length < 2) return pts;
  const a = pts[0], b = pts[pts.length - 1];
  if (Math.abs(a[0] - b[0]) < 1e-9 && Math.abs(a[1] - b[1]) < 1e-9) {
    return pts.slice(0, -1);
  }
  return pts;
}

/**
 * 退化判定：所有点重合。
 * 真实数据里没用到的几何字段不是缺省，而是填了全零 pose
 * （示例里一条 RECT 的 start_pose/end_pose 就是 0,0）。
 * 不判掉的话会画出一条零长度的线，还会挡住后面的兜底探测。
 */
function degenerate(pts) {
  if (!pts || pts.length < 2) return false;
  const [x0, y0] = pts[0];
  for (let i = 1; i < pts.length; i++) {
    if (Math.abs(pts[i][0] - x0) > 1e-9 || Math.abs(pts[i][1] - y0) > 1e-9) return false;
  }
  return true;
}

/** 任意含 x/y 的东西转成 [x,y] 数组，过滤掉 NaN（Pose 与裸点都支持） */
function pts2xy(arr) {
  const out = [];
  for (const p of arr) {
    const xy = poseXY(p);
    if (xy) out.push(xy);
  }
  return out;
}

/** 矩形：2 个对角点补全成 4 角；4 点直接用 */
function rectCorners(pts) {
  if (!pts || !pts.length) return pts;
  if (pts.length >= 4) return pts.slice(0, 4);
  if (pts.length === 2) {
    const [x1, y1] = pts[0], [x2, y2] = pts[1];
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]];
  }
  return pts;
}

/** 中心 + 宽高 -> 4 角（有些版本矩形这么存） */
function rectFromCenter(o) {
  const c = o.center || o.pose;
  const w = o.width ?? o.w ?? o.size_x;
  const h = o.height ?? o.h ?? o.size_y;
  const cx = c && (c.x ?? (c.position && c.position.x));
  const cy = c && (c.y ?? (c.position && c.position.y));
  if (typeof cx !== 'number' || typeof w !== 'number' || typeof h !== 'number') return null;
  return [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2],
          [cx + w / 2, cy + h / 2], [cx - w / 2, cy + h / 2]];
}

/**
 * 从一条 VirtualInfo 里取出几何。
 * 返回 {kind, pts, center, radius}，认不出来返回 null（由调用方跳过）。
 */
function geomOf(vi) {
  if (!vi || typeof vi !== 'object') return null;
  const pt = vi.points_type | 0;

  // 1) 先按 points_type 指定的字段取（真实数据就是这样：
  //    LINE 用 line.poses / start_pose+end_pose，RECT 用 rectangle.poses…）
  let g = null;
  if (pt === PT_LINE) {
    const L = vi.line || {};
    // 先去重再判长度：全零的 start/end_pose 去重后只剩 1 点，自然被淘汰
    const q = dropDupTail(pickPts(L) || pickPair(L) || []);
    if (q.length >= 2 && !degenerate(q)) g = { kind: 'line', pts: q };
  } else if (pt === PT_CIRCLE) {
    g = circleOf(vi.circle);
    // 少数版本圆没有 radius，而是把边界点存在 poses 里
    if (!g) {
      const q = pickPts(vi.circle);
      if (q && q.length >= 3 && !degenerate(q)) g = { kind: 'poly', pts: dropDupTail(q) };
    }
  } else if (pt === PT_RECT) {
    const rc = vi.rectangle || {};
    const q = pickPts(rc);
    if (q && q.length >= 2 && !degenerate(q)) g = { kind: 'poly', pts: rectCorners(dropDupTail(q)) };
    else {
      const p4 = rectFromCenter(rc);
      if (p4) g = { kind: 'poly', pts: p4 };
    }
  } else if (pt === PT_POLY) {
    const q = pickPts(vi.polygon);
    if (q && q.length >= 3 && !degenerate(q)) g = { kind: 'poly', pts: dropDupTail(q) };
  }
  if (g) return g;

  // 2) 兜底：points_type 标错/指定字段为空时，换个字段再试。
  //    区域类（polygon/rectangle）只当区域，不降级成线段 ——
  //    否则一个只有 2 个有效点的残缺多边形会被画成一条莫名的线。
  for (const k of ['polygon', 'rectangle']) {
    if (!vi[k]) continue;
    const q = dropDupTail(pickPts(vi[k]) || []);
    if (degenerate(q)) continue;
    if (q.length >= 3) return { kind: 'poly', pts: q };
    if (k === 'rectangle' && q.length === 2) return { kind: 'poly', pts: rectCorners(q) };
  }
  if (vi.line) {
    const q = dropDupTail(pickPts(vi.line) || pickPair(vi.line) || []);
    if (q.length >= 2 && !degenerate(q)) return { kind: 'line', pts: q };
  }
  const c = circleOf(vi.circle);
  if (c) return c;
  return null;
}

/** 把一条消息的 5 类要素抽成统一结构，供绘制用 */
function extractFeatures(msg) {
  const out = { virtual: [], carpet: [], slope: [], lowth: [], marker: [] };
  if (!msg) return out;
  for (const g of MF_GROUPS) {
    const list = msg[g.field];
    if (!Array.isArray(list)) continue;
    for (const vi of list) {
      if (!vi) continue;
      const geo = geomOf(vi);
      if (!geo) continue;
      out[g.key].push({ vtype: vi.virtual_data_type | 0, geo, group: g.key });
    }
  }
  const mk = msg.marker_point_list;
  if (Array.isArray(mk)) {
    for (const m of mk) {
      if (!m) continue;
      const pos = m.pose && m.pose.position;
      if (!pos || typeof pos.x !== 'number') continue;
      out.marker.push({
        name: m.name || '', type: m.type | 0,
        x: pos.x, y: pos.y,
        th: yawOf(m.pose && m.pose.orientation),
        barcode: m.barcode_id || '',
        isDefault: !!m.is_default,
      });
    }
  }
  return out;
}

/** 地图要素话题（一个 bag 一般只有一个） */
function mapInfoTopic() {
  return S.topics.find(t => S.roles.get(t) === 'mapinfo') || null;
}

/**
 * 取出当前时刻的要素并缓存。
 * 缓存原因：draw 每帧都调，而 map_info_v3 是低频话题，
 * 每帧重新解析大数组会拖慢播放。只在消息换了时才重解析。
 */
function currentFeatures() {
  const tp = mapInfoTopic();
  if (!tp || !S.bag) { S.mf = null; S.mfEntry = null; return null; }
  const e = S.bag.latestBefore(tp, S.t) || S.bag.latestBefore(tp, S.bag.endTime);
  if (!e) { S.mf = null; return null; }
  if (S.mfEntry !== e) {
    const m = S.bag.decode(e);
    S.mf = extractFeatures(m);
    S.mfEntry = e;
    S.mfStat = summarizeFeatures(S.mf);
    // 一个要素都没解析出来时，把原始结构打出来。
    // 子消息字段名（LineData/CircleData…）各版本可能不同，
    // 有这段输出用户就能直接把真实结构发过来，不用猜。
    if (!S.mfStat.total && !S.mfWarned) {
      S.mfWarned = true;
      const raw = {};
      for (const g of MF_GROUPS) {
        const l = m[g.field];
        if (Array.isArray(l) && l.length) raw[g.field] = l[0];
      }
      if (Array.isArray(m.marker_point_list) && m.marker_point_list.length) {
        raw.marker_point_list = m.marker_point_list[0];
      }
      if (Object.keys(raw).length) {
        console.warn('[地图要素] 话题里有数据但没解析出可绘制的几何，原始结构如下，' +
                     '把它发给开发者即可适配：', raw);
      }
    }
    // 要素数量变了就刷新面板上的计数（只在面板打开时动 DOM）
    const sig = JSON.stringify(S.mfStat);
    if (sig !== S.mfSig) {
      S.mfSig = sig;
      const dd = $('mfDD');
      if (dd && dd.classList.contains('open')) renderMfMenu();
    }
  }
  return S.mf;
}

/** 统计各类数量，用于面板上显示"（3）" */
function summarizeFeatures(f) {
  if (!f) return null;
  const out = {};
  let n = 0;
  for (const g of MF_GROUPS) { out[g.key] = f[g.key].length; n += f[g.key].length; }
  out.marker = f.marker.length; n += f.marker.length;
  out.total = n;
  return out;
}

/** 画一个多边形/线段路径（世界坐标 -> 屏幕） */
function tracePoly(pts, close) {
  ctx.beginPath();
  for (let i = 0; i < pts.length; i++) {
    const [sx, sy] = w2s(pts[i][0] + S.offX, pts[i][1] + S.offY);
    if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
  }
  if (close) ctx.closePath();
}

/** 绘制所有地图要素。在地图之后、轨迹之前画，避免盖住车 */
function drawMapFeatures() {
  const f = currentFeatures();
  if (!f) return;
  const tp = mapInfoTopic();
  if (tp && !S.visible.has(tp)) return;      // 话题总开关（时间轴旁边的勾选）
  const L = S.mfLayer;

  ctx.save();
  ctx.lineJoin = 'round';

  // 1) 区域类（先填充，后描边，避免后画的盖住前一个的边框）
  for (const g of MF_GROUPS) {
    if (!L[g.key]) continue;
    for (const it of f[g.key]) {
      const meta = VTYPE[it.vtype] || VTYPE[0];
      const geo = it.geo;
      if (geo.kind === 'poly') {
        if (meta.fill) { tracePoly(geo.pts, true); ctx.fillStyle = meta.fill; ctx.fill(); }
        tracePoly(geo.pts, true);
        ctx.strokeStyle = meta.color; ctx.lineWidth = 1.6; ctx.setLineDash([]);
        ctx.stroke();
      } else if (geo.kind === 'circle') {
        const [sx, sy] = w2s(geo.center[0] + S.offX, geo.center[1] + S.offY);
        const r = (geo.radius / (S.map ? S.map.res : 0.05)) * V.s;
        if (meta.fill) { ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2); ctx.fillStyle = meta.fill; ctx.fill(); }
        ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2);
        ctx.strokeStyle = meta.color; ctx.lineWidth = 1.6; ctx.setLineDash([]); ctx.stroke();
      }
    }
  }

  // 2) 线类（虚拟墙）—— 画在区域之上，才不会被填充色糊住
  for (const g of MF_GROUPS) {
    if (!L[g.key]) continue;
    for (const it of f[g.key]) {
      if (it.geo.kind !== 'line') continue;
      const meta = VTYPE[it.vtype] || VTYPE[0];
      tracePoly(it.geo.pts, false);
      ctx.strokeStyle = meta.color;
      ctx.lineWidth = meta.lw || 2.2;
      ctx.setLineDash(meta.dash || []);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // 3) 标记点（最后画，保证在最上层）
  if (L.marker) {
    for (const m of f.marker) {
      const meta = MTYPE[m.type] || MTYPE[999];
      const [sx, sy] = w2s(m.x + S.offX, m.y + S.offY);
      const r = Math.max(4, Math.min(9, V.s * 0.5));
      // 底盘：深色描边 + 类型色填充，深色浅色地图上都能看清
      ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2);
      ctx.fillStyle = meta.color; ctx.fill();
      ctx.lineWidth = 1.6; ctx.strokeStyle = 'rgba(13,17,23,.9)'; ctx.stroke();
      // 内部字形
      ctx.fillStyle = '#0d1117';
      ctx.font = `bold ${Math.round(r * 1.25)}px sans-serif`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(meta.glyph, sx, sy + 0.5);

      // 名字：缩放太小时不画，否则挤成一团
      if (L.name && m.name && V.s > 0.35) {
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        const tw = ctx.measureText(m.name).width;
        const bx = sx + r + 4, by = sy - 7;
        ctx.fillStyle = 'rgba(13,17,23,.72)';
        ctx.fillRect(bx - 2, by, tw + 4, 14);
        ctx.fillStyle = '#e6edf3';
        ctx.fillText(m.name, bx, by + 7);
      }
    }
  }
  ctx.restore();
}

/**
 * 地图要素图例：单独画在左下角，说明当前画了哪些类型。
 * 放在左下而不是右上（右上已被图层图例占用），避免打架。
 */
function drawFeatureLegend(H) {
  const f = S.mf;
  if (!f || !S.mfStat || !S.mfStat.total) return;
  const tp = mapInfoTopic();
  if (tp && !S.visible.has(tp)) return;

  // 只列出当前实际画出来的类型（按开关过滤）
  const rows = [];
  const seen = new Set();
  const L = S.mfLayer;
  for (const g of MF_GROUPS) {
    if (!L[g.key]) continue;
    for (const it of f[g.key]) {
      const meta = VTYPE[it.vtype] || VTYPE[0];
      if (seen.has(meta.name)) continue;
      seen.add(meta.name);
      rows.push({ c: meta.color, t: meta.name, fill: !!meta.fill });
    }
  }
  if (L.marker) {
    const ms = new Set(f.marker.map(m => (MTYPE[m.type] || MTYPE[999]).name));
    for (const n of ms) {
      const meta = Object.values(MTYPE).find(v => v.name === n);
      rows.push({ c: meta.color, t: '标记点·' + n, fill: false });
    }
  }
  if (!rows.length) return;

  ctx.save();
  ctx.font = '11px sans-serif';
  const pad = 6, lh = 16;
  let maxW = 0;
  for (const r of rows) maxW = Math.max(maxW, ctx.measureText(r.t).width);
  const bw = pad * 2 + 14 + 6 + maxW;
  const bh = pad * 2 + rows.length * lh;
  const bx = 8, by = H - bh - 8;

  ctx.fillStyle = 'rgba(18,22,28,.78)';
  ctx.strokeStyle = 'rgba(127,127,127,.35)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.rect(bx, by, bw, bh); ctx.fill(); ctx.stroke();

  rows.forEach((r, i) => {
    const y = by + pad + i * lh + lh / 2;
    ctx.strokeStyle = r.c; ctx.fillStyle = r.c;
    if (r.fill) { ctx.fillRect(bx + pad, y - 5, 14, 10); ctx.strokeRect(bx + pad, y - 5, 14, 10); }
    else { ctx.lineWidth = 2.5; ctx.beginPath(); ctx.moveTo(bx + pad, y); ctx.lineTo(bx + pad + 14, y); ctx.stroke(); }
    ctx.fillStyle = '#c9d1d9';
    ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    ctx.fillText(r.t, bx + pad + 14 + 6, y);
  });
  ctx.restore();
}

// ═══════════════ TF 树 ═══════════════
function buildTF() {
  S.tf = null;
  S.tfNote = '';
  S.tfCache.clear();

  const tfTopics = S.topics.filter(t => S.roles.get(t) === 'tf');
  if (!tfTopics.length) {
    S.tfNote = '无 /tf 话题，按原始坐标画（可能偏移）';
    return;
  }

  const tree = new TFTree();
  let statN = 0, dynN = 0, bad = 0;

  for (const tp of tfTopics) {
    const isStatic = /static/i.test(tp);
    const idx = S.bag.byTopic.get(tp) || [];
    for (const i of idx) {
      const e = S.bag.entries[i];
      const m = S.bag.decode(e);
      if (!m || !m.transforms) continue;
      for (const tr of m.transforms) {
        if (!tr || !tr.transform) continue;
        const parent = tr.header && tr.header.frame_id;
        const child = tr.child_frame_id;
        if (!parent || !child) { bad++; continue; }
        const tn = tr.transform.translation, rt = tr.transform.rotation;
        if (!tn || !rt) { bad++; continue; }
        if (isStatic) {
          tree.addStatic(parent, child, tn.x, tn.y, yawOf(rt));
          statN++;
        } else {
          tree.addDynamic(parent, child, tn.x, tn.y, yawOf(rt), e.stamp);
          dynN++;
        }
      }
    }
  }
  tree.finalize();
  S.tf = tree;
  S.tfNote = `TF: ${statN} 静态 + ${dynN} 动态${bad ? ` (${bad} 条异常)` : ''}`;
  // 诊断：列出哪些帧能连到地图帧。查不到的帧会按原始坐标画，容易对不上
  const ok = [], ng = [];
  for (const f of tree.frames) {
    if (f === S.mapFrame) continue;
    (tree.lookup(S.mapFrame, f, S.bag ? S.bag.startTime : 0) ? ok : ng).push(f);
  }
  console.log('[TF] 地图帧 =', S.mapFrame);
  console.log('[TF] 可换算到地图的帧:', ok.join(', ') || '(无)');
  if (ng.length) console.warn('[TF] 连不到地图的帧（会按原样画）:', ng.join(', '));
}

/** 查 source 帧 -> 地图帧 的变换（带缓存 + 失败统计） */
function toMap(source, t) {
  if (!S.tfOn || !S.tf) return null;
  if (!source || source === S.mapFrame) return { x: 0, y: 0, theta: 0 };
  const key = source + '@' + Math.floor(t / 1e8);   // 0.1 秒一格，够细也够快
  if (S.tfCache.has(key)) return S.tfCache.get(key);
  const T = S.tf.lookup(S.mapFrame, source, t);
  S.tfCache.set(key, T);
  if (!T && !S.tfWarned.has(source)) {
    S.tfWarned.add(source);
    console.warn(`[TF] 找不到 ${S.mapFrame} ← ${source} 的变换，该话题按原始坐标画`);
  }
  return T;
}

// ═══════════════ 轨迹 ═══════════════
/**
 * 车辆位置有四种算法，结果可能不一致，所以做成可选：
 *   tf   —— 纯粹用 TF: map ← child_frame_id（最权威，前提是 TF 里有完整链路）
 *   odom —— 里程计位姿 ⊗ (map ← odom)，即 Odometry 消息自带的位置
 *   raw  —— 不做任何变换（最早的行为，通常偏）
 *   auto —— 有 TF 用 tf，否则退回 odom，再不行 raw
 */
function buildTraj() {
  S.traj = null;
  S.trajFrame = null;
  S.diag = '';
  if (!S.poseTopic) return;
  const idx = S.bag.byTopic.get(S.poseTopic);
  if (!idx || !idx.length) return;

  const pts = [];
  let frame = null, child = null;
  let nTf = 0, nOdom = 0;
  let gapSum = 0, gapMax = 0, gapN = 0;

  for (const i of idx) {
    const e = S.bag.entries[i];
    const m = S.bag.decode(e);
    if (!m) continue;
    let x, y, th = 0;
    if (m.pose && m.pose.pose) { x = m.pose.pose.position.x; y = m.pose.pose.position.y; th = quatYaw(m.pose.pose.orientation); }
    else if (m.pose && m.pose.position) { x = m.pose.position.x; y = m.pose.position.y; th = quatYaw(m.pose.orientation); }
    else continue;

    if (!frame && m.header) frame = m.header.frame_id;
    if (!child) child = m.child_frame_id;          // Odometry 才有

    // 两种算法都算出来，便于对比
    const Ttf = child ? toMap(child, e.stamp) : null;              // map ← base_link
    const Tmap = toMap(frame, e.stamp);                            // map ← odom
    const pTf = Ttf ? { x: Ttf.x, y: Ttf.y, theta: Ttf.theta } : null;
    const pOdom = Tmap ? compose2D(Tmap, { x, y, theta: th }) : null;

    if (pTf) nTf++;
    if (pOdom) nOdom++;
    if (pTf && pOdom) {
      const g = Math.hypot(pTf.x - pOdom.x, pTf.y - pOdom.y);
      gapSum += g; gapMax = Math.max(gapMax, g); gapN++;
    }

    let p;
    const mode = S.srcMode === 'auto'
      ? (pTf ? 'tf' : (pOdom ? 'odom' : 'raw'))
      : S.srcMode;
    if (mode === 'tf') p = pTf || pOdom || { x, y, theta: th };
    else if (mode === 'odom') p = pOdom || pTf || { x, y, theta: th };
    else if (mode === 'raw') p = { x, y, theta: th };
    else p = pTf || pOdom || { x, y, theta: th };

    pts.push({ x: p.x, y: p.y, th: p.theta, t: e.stamp });
  }

  S.traj = pts;
  S.trajFrame = frame;
  // 诊断：两种算法差多少。差得多说明 odom 和 TF 不同源，需要人工判断哪个对
  const src = S.srcMode === 'auto'
    ? (nTf ? 'TF' : (nOdom ? '里程计' : '原始')) : S.srcMode;
  S.diag = `车辆来源 ${src}`
    + (child ? ` (${frame}→${S.mapFrame}, 车体=${child})` : ` (${frame}→${S.mapFrame})`)
    + (gapN ? ` | TF与里程计平均差 ${(gapSum / gapN).toFixed(2)}m 最大 ${gapMax.toFixed(2)}m` : '');
}
function quatYaw(q) {
  if (!q) return 0;
  const { x = 0, y = 0, z = 0, w = 1 } = q;
  return Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z));
}

// ═══════════════ 视图变换 ═══════════════
const V = { s: 1, tx: 0, ty: 0 };
function fitView() {
  const W = cv.width / DPR, H = cv.height / DPR;
  if (S.map) {
    V.s = Math.min(W / S.map.w, H / S.map.h) * 0.94;
    V.tx = (W - S.map.w * V.s) / 2;
    V.ty = (H - S.map.h * V.s) / 2;
  } else {
    V.s = 1; V.tx = W / 2; V.ty = H / 2;
  }
}
function w2s(x, y) {
  const m = S.map;
  if (!m) return [(x - V.tx) / V.s, (y - V.ty) / V.s];
  const px = (x - m.ox) / m.res, py = (y - m.oy) / m.res;
  return [px * V.s + V.tx, (m.h - py) * V.s + V.ty];
}
function s2w(sx, sy) {
  const m = S.map;
  if (!m) return [sx, sy];
  const px = (sx - V.tx) / V.s, py = m.h - (sy - V.ty) / V.s;
  return [m.ox + px * m.res, m.oy + py * m.res];
}

// ═══════════════ 主绘制 ═══════════════
function draw() {
  const W = cv.width / DPR, H = cv.height / DPR;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, W, H);

  // 地图
  if (S.map && S.visible.has(S.topics.find(t => S.roles.get(t) === 'map'))) {
    ctx.drawImage(S.map.canvas, V.tx, V.ty, S.map.w * V.s, S.map.h * V.s);
  }

  // 地图要素（虚拟墙 / 禁行区 / 地毯 / 斜坡 / 低门槛 / 标记点）
  drawMapFeatures();

  // 轨迹（叠加手动微调）
  if (S.traj && S.visible.has(S.poseTopic)) {
    ctx.strokeStyle = 'rgba(66,165,245,.55)'; ctx.lineWidth = 1.5;
    ctx.beginPath();
    let started = false;
    for (const p of S.traj) {
      const [sx, sy] = w2s(p.x + S.offX, p.y + S.offY);
      if (!started) { ctx.moveTo(sx, sy); started = true; } else ctx.lineTo(sx, sy);
    }
    ctx.stroke();
  }

  // 当前位姿 + 激光
  let pose = null;
  if (S.traj) {
    let lo = 0, hi = S.traj.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (S.traj[mid].t <= S.t) lo = mid + 1; else hi = mid; }
    if (lo > 0) { const p = S.traj[lo - 1]; pose = { x: p.x + S.offX, y: p.y + S.offY, th: p.th + S.offT }; }
  }

  if (pose && S.visible.has(S.poseTopic)) drawRobot(pose);

  // 路径（nav_msgs/Path）：每条一个颜色 + 一种线型
  for (const t of S.topics) {
    if (S.roles.get(t) !== 'path' || !S.visible.has(t)) continue;
    const e = S.bag.latestBefore(t, S.t + 0.3e9);
    if (!e) continue;
    const m = S.bag.decode(e);
    if (m) drawPath(m, e, S.color.get(t) || ROLE_COLOR.path, S.dash.get(t) || []);
  }

  for (const t of S.topics) {
    if (S.roles.get(t) !== 'scan' || !S.visible.has(t)) continue;
    const e = S.bag.latestBefore(t, S.t + 0.3e9);
    if (!e) continue;
    const m = S.bag.decode(e);
    if (m && m.ranges) drawScan(m, e, pose, S.color.get(t) || ROLE_COLOR.scan);
  }

  // 点云(简单画点)
  for (const t of S.topics) {
    if (S.roles.get(t) !== 'cloud' || !S.visible.has(t)) continue;
    const e = S.bag.latestBefore(t, S.t + 0.3e9);
    if (!e) continue;
    const m = S.bag.decode(e);
    if (m) drawCloud(m, e, S.color.get(t) || ROLE_COLOR.cloud);
  }

  // MarkerArray（车辆边框 / 障碍物框等）
  for (const t of S.topics) {
    if (S.roles.get(t) !== 'marker' || !S.visible.has(t)) continue;
    const e = S.bag.latestBefore(t, S.t + 0.3e9);
    if (!e) continue;
    const m = S.bag.decode(e);
    if (m) drawMarkers(m, e, S.color.get(t) || ROLE_COLOR.marker);
  }

  if (S.showLegend) drawLegend(W);
  // 地图要素图例放左下角（右上角已被图层图例占用）
  if (S.showLegend) drawFeatureLegend(H);
}

// visualization_msgs/Marker 的 type 常量
const MK = { ARROW: 0, CUBE: 1, SPHERE: 2, CYLINDER: 3, LINE_STRIP: 4,
             LINE_LIST: 5, CUBE_LIST: 6, SPHERE_LIST: 7, POINTS: 8,
             TEXT: 9, MESH: 10, TRIANGLE_LIST: 11 };

/** 优先用消息自带的颜色，没给（全 0）才用话题分配色 */
function markerColor(mk, fallback) {
  const c = mk.color;
  if (c && (c.r || c.g || c.b)) {
    const a = c.a === undefined ? 1 : c.a;
    if (a > 0.05) {
      return `rgba(${Math.round((c.r || 0) * 255)},${Math.round((c.g || 0) * 255)},`
           + `${Math.round((c.b || 0) * 255)},${Math.min(1, a).toFixed(2)})`;
    }
  }
  return fallback;
}

/**
 * 绘制 visualization_msgs/MarkerArray。
 * 常见的是车辆边框（LINE_STRIP，在 base_link 坐标系下的一圈轮廓），
 * 也支持 LINE_LIST / POINTS / CUBE / TRIANGLE_LIST。
 * 文本(9)和网格(10)画不了，跳过。
 */
function drawMarkers(m, entry, color) {
  const arr = m.markers;
  if (!arr || !arr.length) return;
  const mPerCell = S.map ? S.map.res : 0.05;

  for (const mk of arr) {
    // action: 0=ADD 1=MODIFY 2=DELETE 3=DELETEALL
    if (mk.action === 2 || mk.action === 3) continue;

    const frame = mk.header && mk.header.frame_id;
    const T = toMap(frame, entry.stamp) || { x: 0, y: 0, theta: 0 };
    const col = markerColor(mk, color);
    const type = mk.type | 0;
    const pts = (mk.points || []).filter(p => p && isFinite(p.x) && isFinite(p.y));
    // 世界点 -> 屏幕点
    const P = p => { const w = apply2D(T, { x: p.x, y: p.y }); return w2s(w.x, w.y); };

    ctx.save();
    ctx.strokeStyle = col;
    ctx.fillStyle = col;

    if (type === MK.LINE_STRIP || type === MK.LINE_LIST) {
      const lw = Math.max(1, (mk.scale && mk.scale.x ? mk.scale.x : 0.03) / mPerCell * V.s);
      ctx.lineWidth = Math.min(Math.max(1, lw), 8);
      ctx.beginPath();
      if (type === MK.LINE_LIST) {
        for (let i = 0; i + 1 < pts.length; i += 2) {
          const [x1, y1] = P(pts[i]), [x2, y2] = P(pts[i + 1]);
          ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
        }
      } else {
        for (let i = 0; i < pts.length; i++) {
          const [x, y] = P(pts[i]);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        // 车辆边框首尾常常没重复写，这里自动闭合。
        // 判据：首尾间距远小于整条折线的总长 → 说明它是个闭合多边形
        //      （开放路径的首尾间距会接近总长，不会被误闭合）
        if (pts.length > 2) {
          const a = pts[0], b = pts[pts.length - 1];
          const gap = Math.hypot(a.x - b.x, a.y - b.y);
          let total = 0;
          for (let i = 1; i < pts.length; i++)
            total += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
          if (total > 0 && gap < total * 0.4) ctx.closePath();
        }
      }
      ctx.stroke();

    } else if (type === MK.POINTS || type === MK.SPHERE_LIST || type === MK.CUBE_LIST) {
      const r = Math.max(1.5, (mk.scale && mk.scale.x ? mk.scale.x : 0.05) / mPerCell * V.s / 2);
      for (const p of pts) {
        const [x, y] = P(p);
        ctx.beginPath(); ctx.arc(x, y, Math.min(r, 20), 0, Math.PI * 2); ctx.fill();
      }

    } else if (type === MK.TRIANGLE_LIST) {
      ctx.globalAlpha = 0.35;
      for (let i = 0; i + 2 < pts.length; i += 3) {
        const [x1, y1] = P(pts[i]), [x2, y2] = P(pts[i + 1]), [x3, y3] = P(pts[i + 2]);
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.lineTo(x3, y3); ctx.closePath(); ctx.fill();
      }
      ctx.globalAlpha = 1;

    } else if (type === MK.CUBE || type === MK.CYLINDER || type === MK.SPHERE || type === MK.ARROW) {
      // 俯视：画成带朝向的矩形（车/障碍物的外包框）
      const po = mk.pose && mk.pose.position;
      if (po && isFinite(po.x)) {
        const sx = (mk.scale && mk.scale.x) || 0.5, sy = (mk.scale && mk.scale.y) || 0.5;
        const th = yawOf(mk.pose && mk.pose.orientation) + T.theta;
        const co = Math.cos(th), si = Math.sin(th);
        // 四个角先在本地系旋转，再交给 apply2D 做坐标系变换
        const cs = [[-sx / 2, -sy / 2], [sx / 2, -sy / 2], [sx / 2, sy / 2], [-sx / 2, sy / 2]]
          .map(([dx, dy]) => {
            const wx = po.x + dx * co - dy * si, wy = po.y + dx * si + dy * co;
            const w = apply2D(T, { x: wx, y: wy });
            return w2s(w.x, w.y);
          });
        ctx.lineWidth = 2;
        ctx.beginPath();
        cs.forEach(([x, y], i) => { if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
        ctx.closePath();
        ctx.globalAlpha = 0.25; ctx.fill(); ctx.globalAlpha = 1;
        ctx.stroke();
      }
    }
    ctx.restore();
  }
}

/** 提取 nav_msgs/Path 里的点（兼容 PoseStamped 与裸 Pose） */
function pathPoints(m) {
  const poses = m.poses;
  if (!poses || !poses.length) return [];
  const out = [];
  for (const ps of poses) {
    const p = ps && (ps.pose || ps);
    const pos = p && p.position;
    if (pos && isFinite(pos.x) && isFinite(pos.y)) out.push([pos.x, pos.y]);
  }
  return out;
}

function drawPath(m, entry, color, dash) {
  const pts = pathPoints(m);
  if (!pts.length) return;
  const frame = m.header && m.header.frame_id;
  const T = toMap(frame, entry.stamp) || { x: 0, y: 0, theta: 0 };

  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.globalAlpha = 0.95;
  ctx.setLineDash(dash || []);
  ctx.beginPath();
  for (let i = 0; i < pts.length; i++) {
    const w = apply2D(T, { x: pts[i][0], y: pts[i][1] });
    const [sx, sy] = w2s(w.x, w.y);
    if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
  }
  ctx.stroke();
  ctx.setLineDash([]);

  // 起点画个方块、终点画个圆，方向一目了然
  const mark = (i, fn) => {
    const w = apply2D(T, { x: pts[i][0], y: pts[i][1] });
    const [sx, sy] = w2s(w.x, w.y);
    ctx.beginPath(); fn(sx, sy); ctx.fill();
  };
  ctx.fillStyle = color;
  mark(0, (x, y) => ctx.rect(x - 3, y - 3, 6, 6));
  if (pts.length > 1) mark(pts.length - 1, (x, y) => ctx.arc(x, y, 3.5, 0, Math.PI * 2));
  ctx.restore();
}

/** 右上角图层图例：颜色 + 名字，方便区分各类点云/路径/激光 */
function drawLegend(W) {
  if (!S.bag) return;
  const rows = [];
  for (const t of S.topics) {
    const r = S.roles.get(t);
    if (!S.visible.has(t)) continue;
    if (r === 'map' || r === 'tf' || r === 'image') continue;
    rows.push({ t, c: S.color.get(t) || ROLE_COLOR[r] || '#8d6e63', dash: r === 'path' ? (S.dash.get(t) || []) : null });
  }
  if (!rows.length) return;

  ctx.save();
  ctx.font = '11px sans-serif';
  const pad = 6, lh = 16, sw = 16;
  let maxW = 0;
  for (const r of rows) maxW = Math.max(maxW, ctx.measureText(r.t).width);
  const bw = pad * 2 + sw + 6 + maxW;
  const bh = pad * 2 + rows.length * lh;
  const bx = W - bw - 8, by = 8;

  ctx.fillStyle = 'rgba(18,22,28,.78)';
  ctx.strokeStyle = 'rgba(127,127,127,.35)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.rect(bx, by, bw, bh); ctx.fill(); ctx.stroke();

  rows.forEach((r, i) => {
    const y = by + pad + i * lh + lh / 2;
    ctx.fillStyle = r.c;
    ctx.strokeStyle = r.c;
    if (r.dash) {                       // 路径：画一小段同款线型
      ctx.lineWidth = 2.5;
      ctx.setLineDash(r.dash.length ? r.dash : []);
      ctx.beginPath(); ctx.moveTo(bx + pad, y); ctx.lineTo(bx + pad + sw, y); ctx.stroke();
      ctx.setLineDash([]);
    } else {                            // 点云/激光/轨迹：画实心点
      ctx.beginPath(); ctx.arc(bx + pad + sw / 2, y, 3, 0, Math.PI * 2); ctx.fill();
    }
    ctx.fillStyle = '#c9d1d9';
    ctx.textBaseline = 'middle';
    ctx.fillText(r.t, bx + pad + sw + 6, y);
  });
  ctx.restore();
}

function drawRobot(p) {
  const [sx, sy] = w2s(p.x, p.y);
  // 车宽约 0.7m：先换算成栅格数，再乘每格屏幕像素数
  const mPerCell = S.map ? S.map.res : 0.05;
  // 之前 0.35m 半径(直径 0.7m)画出来比车框还大，这里整体缩小一半：
  // 车体半径按 0.175m 算，上下限也同步收窄
  const r = Math.min(Math.max(3, (0.175 / mPerCell) * V.s), 23);

  ctx.save();
  ctx.translate(sx, sy);
  ctx.rotate(-((p.th || 0) + S.yawOffset + S.offT));   // 屏幕 y 轴向下，故取反

  // 车体（圆 + 描边，深色底图上也看得清）
  ctx.beginPath(); ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(66,165,245,.85)'; ctx.fill();
  ctx.lineWidth = 1.5; ctx.strokeStyle = '#0d1117'; ctx.stroke();

  // 朝向箭头（车头方向），黄色三角
  ctx.beginPath();
  ctx.moveTo(r * 1.9, 0);
  ctx.lineTo(r * 0.35, -r * 0.75);
  ctx.lineTo(r * 0.35, r * 0.75);
  ctx.closePath();
  ctx.fillStyle = '#ffee58'; ctx.fill();
  ctx.lineWidth = 1.2; ctx.strokeStyle = '#0d1117'; ctx.stroke();

  ctx.restore();
}

function drawScan(m, entry, pose, color) {
  const ranges = m.ranges;
  if (!ranges) return;
  const a0 = m.angle_min, inc = m.angle_increment;
  const rmax = m.range_max || 50;
  const frame = m.header && m.header.frame_id;

  // 优先用 TF：把激光点从它自己的坐标系直接换算到地图坐标系
  // 这样激光相对车体的安装偏移（base_link -> laser）也会被正确处理
  let T = toMap(frame, entry.stamp);
  if (!T) {
    // 没有 TF 时退回老办法：假设激光在车体原点、朝向与车体一致
    if (!pose) return;
    T = { x: pose.x, y: pose.y, theta: pose.th };
  }

  ctx.fillStyle = color;
  ctx.globalAlpha = 0.75;
  const step = ranges.length > 800 ? 2 : 1;
  for (let i = 0; i < ranges.length; i += step) {
    const r = ranges[i];
    if (!isFinite(r) || r <= 0 || r > rmax) continue;
    const a = a0 + i * inc;
    const p = apply2D(T, { x: r * Math.cos(a), y: r * Math.sin(a) });
    const [sx, sy] = w2s(p.x, p.y);
    ctx.fillRect(sx - 1, sy - 1, 2, 2);
  }
  ctx.globalAlpha = 1;
}

function drawCloud(m, entry, color) {
  // 只画 x,y 平面；按需自行扩展
  if (!m.data || !m.fields) return;
  const fX = m.fields.find(f => f.name === 'x'), fY = m.fields.find(f => f.name === 'y');
  if (!fX || !fY) return;
  const n = m.width * (m.height || 1);
  const stride = m.point_step;
  const dv = new DataView(m.data.buffer, m.data.byteOffset, m.data.byteLength);
  const offX = fX.offset, offY = fY.offset;
  if (offX == null || offY == null) return;
  const frame = m.header && m.header.frame_id;
  const T = toMap(frame, entry.stamp) || { x: 0, y: 0, theta: 0 };
  ctx.fillStyle = color; ctx.globalAlpha = 0.6;
  const step = n > 4000 ? Math.ceil(n / 4000) : 1;
  for (let i = 0; i < n; i += step) {
    const b = i * stride;
    if (b + stride > m.data.length) break;
    const x = dv.getFloat32(b + offX, true), y = dv.getFloat32(b + offY, true);
    if (!isFinite(x) || !isFinite(y)) continue;
    const p = apply2D(T, { x, y });
    const [sx, sy] = w2s(p.x, p.y);
    ctx.fillRect(sx - 1, sy - 1, 2, 2);
  }
  ctx.globalAlpha = 1;
}

// ═══════════════ 相机画面 ═══════════════
/**
 * sensor_msgs/Image -> ImageData
 * 支持 rgb8 / bgr8 / rgba8 / bgra8 / mono8 / mono16。
 * step 可能大于 width*通道数（行尾有 padding），所以按 step 跳行。
 */
function decodeImage(m) {
  const w = m.width | 0, h = m.height | 0;
  const src = m.data;
  const enc = (m.encoding || '').toLowerCase();
  if (!w || !h || !src) return null;

  let ch = 0;
  if (enc === 'rgb8' || enc === 'bgr8') ch = 3;
  else if (enc === 'rgba8' || enc === 'bgra8') ch = 4;
  else if (enc === 'mono8' || enc === '8uc1') ch = 1;
  else if (enc === 'mono16' || enc === '16uc1') ch = 2;
  else return { err: `格式 ${m.encoding || '?'} 暂不支持` };

  const step = m.step || w * ch;
  if (src.length < step * h) return { err: '图像数据不完整' };

  const out = new ImageData(w, h);
  const d = out.data;
  const be = !!m.is_bigendian;
  let p = 0;

  for (let y = 0; y < h; y++) {
    let s = y * step;
    for (let x = 0; x < w; x++, s += ch) {
      if (ch === 3) {
        if (enc === 'rgb8') { d[p] = src[s]; d[p + 1] = src[s + 1]; d[p + 2] = src[s + 2]; }
        else { d[p] = src[s + 2]; d[p + 1] = src[s + 1]; d[p + 2] = src[s]; }
        d[p + 3] = 255;
      } else if (ch === 4) {
        if (enc === 'rgba8') { d[p] = src[s]; d[p + 1] = src[s + 1]; d[p + 2] = src[s + 2]; d[p + 3] = src[s + 3]; }
        else { d[p] = src[s + 2]; d[p + 1] = src[s + 1]; d[p + 2] = src[s]; d[p + 3] = src[s + 3]; }
      } else if (ch === 1) {
        const v = src[s]; d[p] = v; d[p + 1] = v; d[p + 2] = v; d[p + 3] = 255;
      } else {
        const v = be ? src[s] : src[s + 1];     // 16 位取高字节
        d[p] = v; d[p + 1] = v; d[p + 2] = v; d[p + 3] = 255;
      }
      p += 4;
    }
  }
  return { img: out, w, h };
}

/**
 * 按"魔法字节"判断图片真实格式。
 * 不能直接信 format 字段：ROS 里常见 "jpeg compressed bgr8"、
 * 甚至格式串写错但数据其实是 png 的情况，浏览器按错误的 MIME 解码就会失败
 * （rqt_bag 用 OpenCV 会自动嗅探，所以它能显示）。
 */
function sniffImageType(d) {
  if (!d || d.length < 4) return null;
  const b = n => d[n];
  if (b(0) === 0xff && b(1) === 0xd8 && b(2) === 0xff) return 'image/jpeg';
  if (b(0) === 0x89 && b(1) === 0x50 && b(2) === 0x4e && b(3) === 0x47) return 'image/png';
  if (b(0) === 0x47 && b(1) === 0x49 && b(2) === 0x46) return 'image/gif';
  if (b(0) === 0x42 && b(1) === 0x4d) return 'image/bmp';
  if (b(0) === 0x52 && b(1) === 0x49 && b(2) === 0x46 && b(3) === 0x46
      && d.length > 11 && String.fromCharCode(b(8), b(9), b(10), b(11)) === 'WEBP') return 'image/webp';
  return null;
}
/** format 字符串里猜一个类型，作为嗅探失败时的备选 */
function typeFromFormat(fmt) {
  const f = (fmt || '').toLowerCase();
  if (/png/.test(f)) return 'image/png';
  if (/jpeg|jpg|mjpe?g/.test(f)) return 'image/jpeg';
  if (/webp/.test(f)) return 'image/webp';
  if (/bmp/.test(f)) return 'image/bmp';
  return null;
}
function hexHead(d, n = 8) {
  if (!d || !d.length) return '(空)';
  return Array.from(d.slice(0, n)).map(v => v.toString(16).padStart(2, '0')).join(' ');
}

/**
 * 解码 sensor_msgs/CompressedImage。
 * 顺序：先按魔法字节确定类型 → createImageBitmap → 失败退回 <img> + objectURL
 *      → 再失败就把诊断信息抛出去（格式/长度/头字节/错误原文）
 */
async function decodeCompressed(m) {
  const raw = m.data;
  const fmt = m.format || '';
  // 有些实现对 uint8[] 解成了普通数组，统一成真正的字节
  const d = (raw instanceof Uint8Array) ? raw
          : (raw && raw.buffer instanceof ArrayBuffer ? new Uint8Array(raw.buffer, raw.byteOffset || 0, raw.byteLength)
          : (Array.isArray(raw) ? new Uint8Array(raw) : null));
  if (!d || !d.length) return { err: `压缩图没有数据 (format=${fmt || '?'}, len=${raw ? raw.length : 0})` };

  const sniffed = sniffImageType(d);
  const guesses = [...new Set([sniffed, typeFromFormat(fmt), 'image/jpeg', 'image/png'])].filter(Boolean);

  let lastErr = null;
  for (const type of guesses) {
    try {
      const blob = new Blob([d], { type });
      // 方式一：createImageBitmap（快，支持性差一点）
      if (typeof createImageBitmap === 'function') {
        const bmp = await createImageBitmap(blob);
        const r = bitmapToImageData(bmp);
        bmp.close && bmp.close();
        if (r) return r;
      }
      // 方式二：<img> + objectURL，兼容性最好
      const im = await loadImage(blob);
      const r = bitmapToImageData(im);
      if (r) return r;
    } catch (err) {
      lastErr = err;
    }
  }
  return {
    err: `压缩图解码失败: format="${fmt}" len=${d.length} 嗅探=${sniffed || '未知'} `
       + `头字节=[${hexHead(d)}] ${lastErr ? lastErr.message || lastErr : ''}`,
  };
}

function loadImage(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = e => { URL.revokeObjectURL(url); reject(new Error('img 解码失败')); };
    img.src = url;
  });
}
/** 把 ImageBitmap / HTMLImageElement 转成 ImageData，交给统一的旋转逻辑 */
function bitmapToImageData(src) {
  const w = src.width | 0, h = src.height | 0;
  if (!w || !h) return null;
  if (!_tmpCv) { _tmpCv = document.createElement('canvas'); _tmpCtx = _tmpCv.getContext('2d'); }
  _tmpCv.width = w; _tmpCv.height = h;
  _tmpCtx.setTransform(1, 0, 0, 1, 0, 0);
  _tmpCtx.clearRect(0, 0, w, h);
  _tmpCtx.drawImage(src, 0, 0);
  return { img: _tmpCtx.getImageData(0, 0, w, h), w, h };
}

/**
 * 每个相机的初始旋转角（度，顺时针为正）。
 * 相机安装姿态不同，画面常常是倒的/侧着的，这里按话题名给默认值。
 * 界面上每个格子右上角有 ↻ 按钮可以随时改。
 */
const CAM_ROT_DEFAULT = [
  { test: /camera_01/, rot: 180 },     // 上下颠倒
  { test: /camera_02/, rot: 90 },      // 顺时针 90°（要逆时针就点格子右上角 ↻ 点到 270°）
  { test: /camera_03/, rot: 90 },      // 顺时针 90°
  { test: /camera_04/, rot: 0 },       // 不转
];
/** 旋转按钮上的文字：把内部角度(顺时针正)显示成人话 */
function rotLabel(r) {
  const d = ((r % 360) + 360) % 360;
  if (d === 0) return '↻ 0°';
  if (d === 90) return '↻ 90°';
  if (d === 180) return '↻ 180°';
  return '↻ 270°';      // 内部 270 顺时针 = 逆时针 90°
}
function defaultRot(topic) {
  for (const r of CAM_ROT_DEFAULT) if (r.test.test(topic)) return r.rot;
  return 0;
}

/** 把解码出的图像按角度旋转后画到目标 canvas（90/270 时交换宽高） */
let _tmpCv = null, _tmpCtx = null;
function blitRot(c, img, w, h, rotDeg) {
  const r = ((rotDeg % 360) + 360) % 360;
  const swap = r === 90 || r === 270;
  const W = swap ? h : w, H = swap ? w : h;

  if (!r) {
    if (c.cv.width !== w) c.cv.width = w;
    if (c.cv.height !== h) c.cv.height = h;
    c.ctx.setTransform(1, 0, 0, 1, 0, 0);
    c.ctx.putImageData(img, 0, 0);
    return;
  }
  if (!_tmpCv) { _tmpCv = document.createElement('canvas'); _tmpCtx = _tmpCv.getContext('2d'); }
  if (_tmpCv.width !== w) _tmpCv.width = w;
  if (_tmpCv.height !== h) _tmpCv.height = h;
  _tmpCtx.setTransform(1, 0, 0, 1, 0, 0);
  _tmpCtx.putImageData(img, 0, 0);

  if (c.cv.width !== W) c.cv.width = W;
  if (c.cv.height !== H) c.cv.height = H;
  c.ctx.setTransform(1, 0, 0, 1, 0, 0);
  c.ctx.clearRect(0, 0, W, H);
  c.ctx.save();
  c.ctx.translate(W / 2, H / 2);
  c.ctx.rotate(r * Math.PI / 180);          // canvas y 轴向下，正值即屏幕顺时针
  c.ctx.drawImage(_tmpCv, -w / 2, -h / 2);
  c.ctx.restore();
}

/** 建相机面板：检测图像类话题，最多 4 路，2x2 排布 */
function buildCams() {
  const box = $('cams');
  box.innerHTML = '';
  S.cams = [];

  const imgs = S.topics.filter(t => S.roles.get(t) === 'image');
  if (!imgs.length) {
    box.classList.add('hide');
    $('vdiv').classList.add('hide');
    return;
  }
  // 优先 /camera_XX/color/image_raw 这类彩色图，按编号排序；不够再补其他图像话题
  const byNum = (a, b) => a.localeCompare(b, undefined, { numeric: true });
  const isColor = t => /\/camera_\d+\/color\//.test(t) || (/color/.test(t) && !/depth/.test(t));
  const color = imgs.filter(isColor).sort(byNum);
  const rest = imgs.filter(t => !isColor(t)).sort(byNum);
  const pick = [...color, ...rest].slice(0, 4);
  box.classList.remove('hide');
  $('vdiv').classList.remove('hide');

  for (const topic of pick) {
    const el = document.createElement('div');
    el.className = 'cam';
    const short = topic.split('/').filter(Boolean).slice(-3, -1).join('/') || topic;
    const n = (S.bag.byTopic.get(topic) || []).length;
    el.innerHTML = `<div class="lbl">${short} <span style="opacity:.6">(${n})</span></div>`
                 + `<div class="err">等待数据…</div>`;
    const c = document.createElement('canvas');
    el.appendChild(c);

    // 右上角旋转按钮：每点一次顺时针 90°（想要逆时针就点到绕回来）
    const rot = defaultRot(topic);
    const btn = document.createElement('button');
    btn.className = 'rot' + (rot ? ' on' : '');
    btn.textContent = rotLabel(rot);
    btn.title = '旋转画面（每点一次 +90°）';
    el.appendChild(btn);

    box.appendChild(el);
    const cam = {
      topic, el, cv: c, ctx: c.getContext('2d'),
      err: el.querySelector('.err'), entry: null, rot, btn,
    };
    btn.onclick = () => {
      cam.rot = (cam.rot + 90) % 360;
      btn.textContent = rotLabel(cam.rot);
      btn.classList.toggle('on', cam.rot !== 0);
      cam.entry = null;                 // 强制重画
      drawCams(true);
    };
    S.cams.push(cam);
  }
  // 只有 1 路时铺满，2 路时一行两个
  if (S.cams.length === 1) box.style.gridTemplateColumns = '1fr';
  else if (S.cams.length === 2) { box.style.gridTemplateColumns = '1fr 1fr'; box.style.gridTemplateRows = '1fr'; }
  else { box.style.gridTemplateColumns = '1fr 1fr'; box.style.gridTemplateRows = '1fr 1fr'; }
}

const CAM_MIN_MS = 90;   // 播放时相机最快刷新间隔，避免解码拖垮播放

/** 把各相机在 S.t 时刻最近的帧画出来（压缩图分支是异步的，调用处无需 await） */
async function drawCams(force) {
  if (!S.cams.length || !S.bag) return;
  const now = performance.now();
  if (!force && S.playing && now - S.lastCamT < CAM_MIN_MS) return;
  S.lastCamT = now;

  for (const c of S.cams) {
    const e = S.bag.latestBefore(c.topic, S.t + 0.25e9);
    if (!e) { c.err.style.display = ''; c.err.textContent = '该时刻前无数据'; continue; }
    if (!force && c.entry === e) continue;          // 帧没变就不重复解码
    c.entry = e;
    const m = S.bag.decode(e);
    if (!m) { c.err.style.display = ''; c.err.textContent = '解析失败'; continue; }

    // 压缩图走浏览器原生解码
    if (m.format && m.data) {
      const r = await decodeCompressed(m);
      if (r && r.img) { blitRot(c, r.img, r.w, r.h, c.rot); c.err.style.display = 'none'; continue; }
      // 万一它不是真压缩图（有 format 但其实是裸像素），再试一次原始解码
      const r2 = (m.width && m.height && m.encoding) ? decodeImage(m) : null;
      if (r2 && !r2.err) { blitRot(c, r2.img, r2.w, r2.h, c.rot); c.err.style.display = 'none'; continue; }
      c.err.style.display = '';
      c.err.textContent = r ? r.err : '压缩图解码失败';
      continue;
    }

    const r = decodeImage(m);
    if (!r || r.err) {
      c.err.style.display = '';
      c.err.textContent = r ? r.err : '无法解码';
      continue;
    }
    blitRot(c, r.img, r.w, r.h, c.rot);
    c.err.style.display = 'none';
  }
}

// ═══════════════ 录制（WebM / GIF）═══════════════
const REC = { on: false, mode: 'webm', rec: null, chunks: [], frames: [],
              t0: 0, timer: null, fps: 10, maxSec: 30 };

/** 把区域2（地图 + 相机）按比例合成到一张离屏画布上 */
function composeFrame(out, octx) {
  const view = $('view');
  const vr = view.getBoundingClientRect();
  const k = out.width / Math.max(1, vr.width);
  octx.setTransform(1, 0, 0, 1, 0, 0);
  octx.fillStyle = '#0d1117';
  octx.fillRect(0, 0, out.width, out.height);
  const put = el => {
    if (!el || !el.width || !el.height) return;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    octx.drawImage(el, (r.left - vr.left) * k, (r.top - vr.top) * k, r.width * k, r.height * k);
  };
  put($('cv'));
  for (const c of S.cams) put(c.cv);
}

function grabFrame() {
  const view = $('view');
  const vr = view.getBoundingClientRect();
  // 限制尺寸，避免录出几百 MB
  const W = Math.min(960, Math.max(320, Math.round(vr.width)));
  const H = Math.round(W * (vr.height / Math.max(1, vr.width)));
  if (!REC._cv) { REC._cv = document.createElement('canvas'); }
  const cv2 = REC._cv;
  if (cv2.width !== W || cv2.height !== H) { cv2.width = W; cv2.height = H; }
  const cx = cv2.getContext('2d');
  composeFrame(cv2, cx);
  return { w: W, h: H, data: new Uint8ClampedArray(cx.getImageData(0, 0, W, H).data) };
}

function startRec(mode) {
  if (!S.bag) { alert('先打开一个 bag'); return; }
  REC.mode = mode; REC.frames = []; REC.chunks = []; REC.on = true;
  REC.t0 = performance.now();
  const btn = $('btnRec');
  btn.classList.add('recording');
  btn.textContent = '● 停止';

  if (mode === 'webm' && typeof MediaRecorder !== 'undefined' && $('cv').captureStream) {
    try {
      const { w, h } = (() => { const v = $('view').getBoundingClientRect();
        return { w: Math.min(960, Math.max(320, Math.round(v.width))),
                 h: Math.round(Math.min(960, Math.max(320, Math.round(v.width))) * (v.height / Math.max(1, v.width))) }; })();
      if (!REC._cv) REC._cv = document.createElement('canvas');
      REC._cv.width = w; REC._cv.height = h;
      REC._octx = REC._cv.getContext('2d');
      const stream = REC._cv.captureStream(REC.fps);
      const types = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
      const mt = types.find(t => MediaRecorder.isTypeSupported(t)) || '';
      REC.rec = new MediaRecorder(stream, mt ? { mimeType: mt, videoBitsPerSecond: 4e6 } : undefined);
      REC.rec.ondataavailable = e => { if (e.data && e.data.size) REC.chunks.push(e.data); };
      REC.rec.onstop = () => {
        const blob = new Blob(REC.chunks, { type: mt || 'video/webm' });
        download(blob, `${(S.fileName || 'bag').replace(/\.bag$/i, '')}.webm`);
      };
      REC.rec.start(200);
      // 持续把画面画到离屏画布上，MediaRecorder 才有东西可录
      REC.timer = setInterval(() => {
        if (!REC.on) return;
        composeFrame(REC._cv, REC._octx);
        if (performance.now() - REC.t0 > REC.maxSec * 1000) stopRec();
      }, 1000 / REC.fps);
      return;
    } catch (e) { console.warn('[录制] MediaRecorder 不可用，改用 GIF', e); }
  }
  // GIF：按固定 fps 抓帧，停止时编码
  REC.mode = 'gif';
  REC.timer = setInterval(() => {
    if (!REC.on) return;
    REC.frames.push(grabFrame());
    if (performance.now() - REC.t0 > REC.maxSec * 1000) stopRec();
  }, 1000 / REC.fps);
}

function stopRec() {
  if (!REC.on) return;
  REC.on = false;
  clearInterval(REC.timer); REC.timer = null;
  const btn = $('btnRec');
  btn.classList.remove('recording');
  btn.textContent = '⏺ 录制';
  if (REC.rec) {
    try { REC.rec.stop(); } catch (e) { /* 已停 */ }
    REC.rec = null;
    return;
  }
  if (!REC.frames.length) return;
  btn.textContent = '编码中…'; btn.disabled = true;
  setTimeout(() => {
    try {
      const gif = encodeGIF(REC.frames, Math.round(100 / REC.fps));
      download(new Blob([gif], { type: 'image/gif' }),
               `${(S.fileName || 'bag').replace(/\.bag$/i, '')}.gif`);
    } catch (e) {
      alert('GIF 编码失败: ' + e.message);
      console.error(e);
    }
    btn.textContent = '⏺ 录制'; btn.disabled = false;
  }, 30);
}

/** 触发浏览器下载 */
function download(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
}

// ── GIF89a 编码（自写，无外部依赖）─────────────────────
/** 位写入器：GIF 的字节流是 LSB-first */
class BitWriter {
  constructor() { this.buf = new Uint8Array(1024); this.len = 0; this.acc = 0; this.nbits = 0; }
  _need(n) { if (this.len + n + 8 > this.buf.length) { const b = new Uint8Array(Math.max(this.buf.length * 2, this.len + n + 8)); b.set(this.buf.subarray(0, this.len)); this.buf = b; } }
  write(code, size) {
    this.acc |= code << this.nbits; this.nbits += size;
    while (this.nbits >= 8) {
      this._need(1); this.buf[this.len++] = this.acc & 255;
      this.acc >>>= 8; this.nbits -= 8;
    }
  }
  flush() { if (this.nbits > 0) { this._need(1); this.buf[this.len++] = this.acc & 255; this.acc = 0; this.nbits = 0; } return this.buf.subarray(0, this.len); }
}
/** LZW 压缩索引序列 */
function lzwEncode(idx, minCodeSize) {
  const clear = 1 << minCodeSize, eoi = clear + 1;
  let codeSize = minCodeSize + 1, next = eoi + 1;
  const dict = new Map();
  const bw = new BitWriter();
  bw.write(clear, codeSize);
  if (!idx.length) { bw.write(eoi, codeSize); return bw.flush(); }
  let prefix = idx[0];
  for (let i = 1; i < idx.length; i++) {
    const k = idx[i], key = prefix * 256 + k;
    const v = dict.get(key);
    if (v !== undefined) { prefix = v; continue; }
    bw.write(prefix, codeSize);
    if (next < 4096) {
      dict.set(key, next++);
      if (next - 1 === (1 << codeSize) && codeSize < 12) codeSize++;
    } else {
      bw.write(clear, codeSize);
      codeSize = minCodeSize + 1; next = eoi + 1; dict.clear();
    }
    prefix = k;
  }
  bw.write(prefix, codeSize);
  bw.write(eoi, codeSize);
  return bw.flush();
}
/** 把 LZW 字节流切成 255 字节的子块 */
function toSubBlocks(bytes) {
  const out = [];
  for (let p = 0; p < bytes.length;) {
    const n = Math.min(255, bytes.length - p);
    out.push(n); out.push(...bytes.subarray(p, p + n)); p += n;
  }
  out.push(0);
  return out;
}
/**
 * 中值切分生成 256 色全局调色板（对首帧采样，速度快且够准）
 */
function buildPalette(frames) {
  const N = 256;
  // 采样：最多 4 万个点
  const total = frames[0].data.length / 4;
  const stepPx = Math.max(1, Math.floor(total * Math.min(frames.length, 4) / 40000));
  const pts = [];
  for (let fi = 0; fi < Math.min(frames.length, 4); fi++) {
    const d = frames[fi].data;
    for (let i = 0; i < d.length; i += 4 * stepPx) pts.push([d[i], d[i + 1], d[i + 2]]);
  }
  if (!pts.length) pts.push([0, 0, 0]);
  const boxes = [{ pts, r0: 0, r1: 255, g0: 0, g1: 255, b0: 0, b1: 255 }];
  // 初始范围收紧到实际数据范围
  const bounds = b => {
    let r0 = 255, r1 = 0, g0 = 255, g1 = 0, b0 = 255, b1 = 0;
    for (const p of b.pts) {
      if (p[0] < r0) r0 = p[0]; if (p[0] > r1) r1 = p[0];
      if (p[1] < g0) g0 = p[1]; if (p[1] > g1) g1 = p[1];
      if (p[2] < b0) b0 = p[2]; if (p[2] > b1) b1 = p[2];
    }
    b.r0 = r0; b.r1 = r1; b.g0 = g0; b.g1 = g1; b.b0 = b0; b.b1 = b1;
  };
  bounds(boxes[0]);
  while (boxes.length < N) {
    // 选体积最大且点数 >1 的盒子
    let bi = -1, best = 0;
    for (let i = 0; i < boxes.length; i++) {
      const b = boxes[i];
      if (b.pts.length < 2) continue;
      const v = (b.r1 - b.r0 + 1) * (b.g1 - b.g0 + 1) * (b.b1 - b.b0 + 1);
      if (v > best) { best = v; bi = i; }
    }
    if (bi < 0) break;
    const b = boxes[bi];
    const dr = b.r1 - b.r0, dg = b.g1 - b.g0, db = b.b1 - b.b0;
    const ch = dr >= dg && dr >= db ? 0 : (dg >= db ? 1 : 2);
    b.pts.sort((p, q) => p[ch] - q[ch]);
    const mid = b.pts.length >> 1;
    const A = { pts: b.pts.slice(0, mid) }, B = { pts: b.pts.slice(mid) };
    boxes.splice(bi, 1, A, B);
    bounds(A); bounds(B);
  }
  // 每个盒子取平均色
  const pal = [];
  for (const b of boxes) {
    let r = 0, g = 0, bl = 0;
    for (const p of b.pts) { r += p[0]; g += p[1]; bl += p[2]; }
    const n = Math.max(1, b.pts.length);
    pal.push([Math.round(r / n), Math.round(g / n), Math.round(bl / n)]);
  }
  while (pal.length < N) pal.push([0, 0, 0]);
  return pal.slice(0, N);
}
/** 用调色板把 RGBA 帧转成索引；用 32³ 查表加速 */
function quantize(d, pal) {
  const cache = new Int16Array(32768).fill(-1);
  const idx = new Uint8Array(d.length / 4);
  for (let i = 0, p = 0; i < d.length; i += 4, p++) {
    const r = d[i], g = d[i + 1], b = d[i + 2];
    const key = ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3);
    let v = cache[key];
    if (v < 0) {
      let best = 0, bd = 1 << 30;
      for (let c = 0; c < pal.length; c++) {
        const dr = r - pal[c][0], dg = g - pal[c][1], db = b - pal[c][2];
        const dist = dr * dr * 3 + dg * dg * 6 + db * db;   // 绿权重高些
        if (dist < bd) { bd = dist; best = c; }
      }
      cache[key] = best; v = best;
    }
    idx[p] = v;
  }
  return idx;
}
/**
 * 把若干帧编码成 GIF89a（动图）。
 * @param frames [{w,h,data(Uint8ClampedArray RGBA)}]
 * @param delay  每帧延迟，单位 1/100 秒
 */
function encodeGIF(frames, delay) {
  if (!frames || !frames.length) throw new Error('没有帧');
  const w = frames[0].w, h = frames[0].h;
  const pal = buildPalette(frames);
  const out = [];
  const push = (...a) => out.push(...a);
  const str = s => { for (const ch of s) out.push(ch.charCodeAt(0)); };
  const u16 = v => out.push(v & 255, (v >> 8) & 255);

  str('GIF89a');
  u16(w); u16(h);
  out.push(0xF7);      // 全局色表，256 色，8 位
  out.push(0, 0);      // 背景色索引、像素宽高比
  for (let i = 0; i < 256; i++) { const c = pal[i]; out.push(c[0], c[1], c[2]); }
  // Netscape 循环扩展（无限循环）
  out.push(0x21, 0xFF, 11);
  str('NETSCAPE2.0');
  out.push(3, 1, 0, 0, 0);

  for (const f of frames) {
    // 图形控制扩展：延迟时间
    out.push(0x21, 0xF9, 4, 0x04, delay & 255, (delay >> 8) & 255, 0, 0);
    // 图像描述符
    out.push(0x2C); u16(0); u16(0); u16(f.w); u16(f.h); out.push(0);
    out.push(8);    // LZW 最小码长
    const idx = quantize(f.data, pal);
    push(...toSubBlocks(lzwEncode(idx, 8)));
  }
  out.push(0x3B);      // 文件结束
  return new Uint8Array(out);
}

// ═══════════════ 数值曲线 ═══════════════
const CHART_COLORS = ['#42a5f5', '#ffb300', '#ab47bc', '#26c6da', '#ef5350', '#9ccc65', '#ffa726', '#78909c'];
const CHART_MAX = 12;

/** 摊平成 { 路径: 数值 }。数组和时间字段特殊处理 */
function flatNums(obj, prefix, out, depth) {
  if (depth > 4) return out;
  for (const k in obj) {
    const v = obj[k];
    if (v === null || v === undefined) continue;
    const p = prefix ? prefix + '.' + k : k;
    if (typeof v === 'number') { if (isFinite(v)) out[p] = v; }
    else if (typeof v === 'boolean') out[p] = v ? 1 : 0;
    else if (typeof v === 'object') {
      // time / duration 转成秒
      if (typeof v.sec === 'number' && typeof v.nanosec === 'number') out[p] = v.sec + v.nanosec * 1e-9;
      else if (!Array.isArray(v)) flatNums(v, p, out, depth + 1);
    }
  }
  return out;
}

/**
 * 一条曲线 = 一个话题里的一个数值字段。
 * 可以跨话题混搭（比如同时看 /ax_slam_state 的 quality 和 /debug_control 的速度）。
 */
function seriesKey(topic, field) { return topic + '\u0000' + field; }
function shortName(topic, field) {
  const seg = topic.split('/').filter(Boolean);
  const t = seg.length > 1 ? seg.slice(-2).join('/') : topic;
  return `${t} · ${field.split('.').pop()}`;
}

/** 抽取一条序列的数据点（缓存） */
const _seriesCache = new Map();
function getSeries(topic, field) {
  const key = seriesKey(topic, field);
  if (_seriesCache.has(key)) return _seriesCache.get(key);
  const idx = S.bag.byTopic.get(topic) || [];
  const pts = [];
  const MAX = 20000;
  const step = Math.max(1, Math.ceil(idx.length / MAX));
  for (let k = 0; k < idx.length; k += step) {
    const e = S.bag.entries[idx[k]];
    const m = S.bag.decode(e);
    if (!m) continue;
    const v = flatNums(m, '', Object.create(null), 0);
    const y = v[field];
    if (y !== undefined) pts.push({ t: e.stamp, v: y });
  }
  let mn = Infinity, mx = -Infinity;
  for (const p of pts) { if (p.v < mn) mn = p.v; if (p.v > mx) mx = p.v; }
  const s = { topic, field, pts, min: mn, max: mx, changed: mx > mn, on: true };
  _seriesCache.set(key, s);
  return s;
}

/** 加入 / 移除一条曲线 */
function toggleSeries(topic, field) {
  const key = seriesKey(topic, field);
  const i = S.series.findIndex(s => seriesKey(s.topic, s.field) === key);
  if (i >= 0) { S.series.splice(i, 1); renderLegend(); drawChart(); return; }
  if (S.series.length >= CHART_MAX) { S.chartMsg = `最多同时画 ${CHART_MAX} 条`; renderLegend(); return; }
  const s = getSeries(topic, field);
  if (!s.pts.length) { S.chartMsg = '该字段没有数据'; renderLegend(); return; }
  S.series.push(s);
  S.chartMsg = '';
  renderLegend(); drawChart();
}

function clearSeries() { S.series = []; renderLegend(); drawChart(); }

function chartCanvas() {
  const c = $('chartcv');
  const w = Math.max(80, c.clientWidth || 300);
  const h = Math.max(60, c.clientHeight || 140);
  const d = DPR || 1;
  const cw = Math.round(w * d), ch = Math.round(h * d);
  if (c.width !== cw) c.width = cw;
  if (c.height !== ch) c.height = ch;
  const ctx = c.getContext('2d');
  ctx.setTransform(d, 0, 0, d, 0, 0);
  return { ctx, w, h };
}

/** 曲线可视窗口（秒，相对 bag 开头） */
function chView() {
  const dur = S.bag ? Math.max((S.bag.endTime - S.bag.startTime) / 1e9, 0.001) : 1;
  const vd = dur / Math.max(1, S.chZoom);
  let v0 = S.chView0;
  if (v0 < 0) v0 = 0;
  if (v0 + vd > dur) v0 = Math.max(0, dur - vd);
  return { v0, vd: Math.min(vd, dur), dur };
}
function chClamp() { S.chView0 = chView().v0; }

/** 取某条曲线在时刻 ts 处的值（阶梯保持：取该时刻之前最后一个点） */
function seriesValueAt(s, ts) {
  const pts = s.pts;
  if (!pts || !pts.length) return null;
  let lo = 0, hi = pts.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (pts[m].t <= ts) lo = m + 1; else hi = m; }
  return lo > 0 ? pts[lo - 1].v : null;
}

function drawChart() {
  const { ctx, w, h } = chartCanvas();
  ctx.clearRect(0, 0, w, h);
  const on = S.series.filter(s => s.on);
  if (!on.length) {
    ctx.fillStyle = '#5c6673'; ctx.font = '11px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(S.chartMsg || '点「+ 曲线」挑要看的字段', w / 2, h / 2);
    ctx.textAlign = 'left';
    return;
  }
  const pad = 4, innerH = h - pad * 2 - 10;
  const { v0, vd } = chView();
  chClamp();
  const t0 = S.bag.startTime;
  // 时间(秒) -> x
  const X = ts => (ts - v0) / vd * w;

  ctx.strokeStyle = 'rgba(127,127,127,.16)'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad + innerH * i / 4;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }

  // 纵向网格 + 时间刻度
  const nice = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600];
  const gs = nice.find(s => vd / s <= 8) || vd / 4;
  ctx.strokeStyle = 'rgba(127,127,127,.13)';
  ctx.fillStyle = '#5c6673'; ctx.font = '9px sans-serif';
  for (let tv = Math.ceil(v0 / gs) * gs; tv <= v0 + vd; tv += gs) {
    const x = Math.round(X(tv));
    ctx.beginPath(); ctx.moveTo(x, pad); ctx.lineTo(x, pad + innerH); ctx.stroke();
    ctx.fillText(fmtSec(tv), x + 2, h - 3);
  }

  // 每条独立归一化：量纲不同（0~1 的开关 vs 0~50 的速度）也能同屏看
  on.forEach((s, i) => {
    const rng = s.max - s.min;
    ctx.strokeStyle = CHART_COLORS[i % CHART_COLORS.length];
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    let started = false;
    for (const p of s.pts) {
      const ts = (p.t - t0) / 1e9;
      if (ts < v0 - vd * 0.05 || ts > v0 + vd * 1.05) continue;
      const x = X(ts);
      const y = rng ? (pad + innerH - (p.v - s.min) / rng * innerH) : (pad + innerH / 2);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  });

  // 播放头
  const playTs = (S.t - t0) / 1e9;
  if (playTs >= v0 && playTs <= v0 + vd) {
    const x = X(playTs);
    ctx.strokeStyle = '#ef4444'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
  }

  // 悬停十字线 + 该时刻各曲线的实际数值
  if (S.hoverT != null) {
    const hx = X(S.hoverT);
    ctx.strokeStyle = 'rgba(201,209,217,.55)'; ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(hx, 0); ctx.lineTo(hx, h - 10); ctx.stroke();
    ctx.setLineDash([]);

    const tAbs = t0 + S.hoverT * 1e9;
    const rows = on.map((s, i) => {
      const v = seriesValueAt(s, tAbs);
      return { c: CHART_COLORS[i % CHART_COLORS.length], name: shortField(s), v, s };
    }).filter(r => r.v != null);
    if (rows.length) {
      // 各曲线上的圆点
      rows.forEach(r => {
        const rng = r.s.max - r.s.min;
        const y = rng ? (pad + innerH - (r.v - r.s.min) / rng * innerH) : (pad + innerH / 2);
        ctx.fillStyle = r.c;
        ctx.beginPath(); ctx.arc(hx, y, 2.8, 0, Math.PI * 2); ctx.fill();
      });
      // 数值气泡
      ctx.font = '10px sans-serif';
      const lines = rows.map(r => `${r.name} ${fmtNum(r.v)}`);
      const tw = Math.max(...lines.map(l => ctx.measureText(l).width)) + 10;
      const bw = Math.min(tw, w - 6), bh = rows.length * 12 + 6;
      let bx = hx + 8; if (bx + bw > w - 2) bx = hx - bw - 8; if (bx < 2) bx = 2;
      const by = 3;
      ctx.fillStyle = 'rgba(13,17,23,.92)';
      ctx.strokeStyle = 'rgba(127,127,127,.4)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.rect(bx, by, bw, bh); ctx.fill(); ctx.stroke();
      rows.forEach((r, i) => {
        ctx.fillStyle = r.c;
        ctx.fillRect(bx + 4, by + 4 + i * 12 + 2, 5, 5);
        ctx.fillStyle = '#c9d1d9';
        ctx.fillText(lines[i], bx + 12, by + 4 + i * 12 + 7);
      });
    }
  }
}

/** 曲线名：话题太长时只留最后一段 + 字段名 */
function shortField(s) {
  const f = s.field.split('.').slice(-1)[0];
  return f;
}

/** 图例：一条曲线一个色块，点击显示/隐藏，× 移除；色块后实时显示当前值 */
function renderLegend() {
  const box = $('legend');
  box.innerHTML = '';
  const on = S.series.filter(s => s.on).length;
  const zi = S.chZoom > 1.001 ? ` · ×${S.chZoom.toFixed(S.chZoom < 10 ? 1 : 0)}` : '';
  $('chartInfo').textContent = S.series.length
    ? `${on}/${S.series.length} 条` + zi + (S.chartMsg ? ' · ' + S.chartMsg : '')
    : (S.chartMsg || '未选择');
  S.series.forEach((s, i) => {
    const el = document.createElement('span');
    el.className = 'lg' + (s.on ? '' : 'off');
    el.title = `${s.topic}\n${s.field}\n范围 [${fmtNum(s.min)} .. ${fmtNum(s.max)}]`
             + (s.changed ? '' : '\n（全程无变化）') + '\n点击显示/隐藏';
    el.innerHTML = `<span class="sw" style="background:${CHART_COLORS[i % CHART_COLORS.length]}"></span>`
                 + `<span class="lgn">${shortName(s.topic, s.field)}</span>`
                 + `<span class="lgv">–</span>`
                 + `<span class="x" title="移除" style="cursor:pointer;padding-left:3px">×</span>`;
    el.title += `\n范围 [${fmtNum(s.min)} .. ${fmtNum(s.max)}]`;
    el.onclick = ev => {
      if (ev.target.classList.contains('x')) { toggleSeries(s.topic, s.field); return; }
      s.on = !s.on; renderLegend(); drawChart();
    };
    box.appendChild(el);
  });
  updateLegendValues();
}

/** 图例里实时显示"播放头所在时刻"的各曲线数值 */
function updateLegendValues() {
  const box = $('legend');
  if (!box) return;
  const chips = box.querySelectorAll('.lg');
  if (!chips.length) return;
  if (!S.bag) return;
  S.series.forEach((s, i) => {
    const el = chips[i]; if (!el) return;
    const v = el.querySelector('.lgv');
    if (!v) return;
    if (!s.on) { v.textContent = ''; return; }
    const val = seriesValueAt(s, S.t);
    v.textContent = val == null ? '–' : fmtNum(val);
  });
}
/**
 * 纳秒 -> 墙上时间 "MM-DD HH:MM:SS[.mmm]"。
 *
 * 用本地时区（getMonth/getHours…），不用 UTC —— 这是刻意对齐 `rosbag info`
 * 的行为：它显示 start/end 用的也是本地时间。所以在东八区机器上，
 * 1788423000 显示成 "09-03 16:10:00"，跟你 rosbag info 看到的完全一致。
 * 换成 UTC 会差 8 小时，排查时容易对不上号。
 */
function fmtRos(ns, opt = {}) {
  if (!isFinite(ns)) return '--';
  const d = new Date(Math.floor(ns / 1e6));
  const p2 = v => String(v).padStart(2, '0');
  const M = p2(d.getMonth() + 1), D = p2(d.getDate());
  const h = p2(d.getHours()), m = p2(d.getMinutes()), sec = p2(d.getSeconds());
  if (opt.min) return `${M}-${D} ${h}:${m}`;
  if (opt.ms) return `${M}-${D} ${h}:${m}:${sec}.${String(d.getMilliseconds()).padStart(3, '0')}`;
  return `${M}-${D} ${h}:${m}:${sec}`;
}

/** 秒 -> 简短时间文字（时间轴刻度用） */
function fmtSec(s) {
  const a = Math.abs(s);
  if (a < 1) return `${(s * 1000).toFixed(0)}ms`;
  if (a < 60) return `${s.toFixed(a < 10 ? 2 : 1)}s`;
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
}
function fmtNum(v) {
  if (!isFinite(v)) return '?';
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1e6 || a < 1e-3) return v.toExponential(1);
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(a < 1 ? 3 : 2);
}

/** 曲线字段选择菜单：按话题分组，只列数值字段 */
function renderChartMenu() {
  const box = $('chartMenu');
  box.innerHTML = '';
  const skip = new Set(['image', 'map', 'tf', 'marker']);
  for (const t of S.topics) {
    if (skip.has(S.roles.get(t))) continue;
    const idx = S.bag.byTopic.get(t) || [];
    if (!idx.length) continue;
    const m = S.bag.decode(S.bag.entries[idx[0]]);
    if (!m) continue;
    const fields = flatWithTypes(m, '', Object.create(null), 0);
    const keys = Object.keys(fields).filter(k => fields[k] === 'num' || fields[k] === 'bool' || fields[k] === 'time');
    if (!keys.length) continue;
    const g = document.createElement('div');
    g.className = 'grp';
    g.textContent = t;
    box.appendChild(g);
    for (const k of keys) {
      const has = S.series.some(s => s.topic === t && s.field === k);
      const it = document.createElement('div');
      it.className = 'it sub' + (has ? ' on' : '');
      it.innerHTML = `<span class="box"></span><span class="nm">${k}</span><span class="ty">${fields[k]}</span>`;
      it.onclick = ev => {
        ev.stopPropagation();
        toggleSeries(t, k);
        renderChartMenu();
      };
      box.appendChild(it);
    }
  }
}

/** 打开 bag 后默认只画 manual 一条，其余让用户自己加 */
function autoPickSeries() {
  S.series = [];
  S.chartMsg = '';
  const t = S.topics.find(x => /hardware/.test(x));
  if (t) {
    const idx = S.bag.byTopic.get(t) || [];
    if (idx.length) {
      const m = S.bag.decode(S.bag.entries[idx[0]]);
      if (m && getPath(m, 'manual') !== undefined) {
        const s = getSeries(t, 'manual');
        if (s.pts.length) S.series.push(s);
      }
    }
  }
  renderLegend(); drawChart();
}

// ═══════════════ 状态条 ═══════════════
/**
 * 状态条显示"此刻的关键量"。默认挑这几类，可自行增删：
 *   定位匹配分、线/角速度、手自动、急停、导航状态
 */
/**
 * 状态条槽位。顺序即显示顺序，且**位置固定**——
 * 即使某个话题在这个 bag 里不存在，槽位也留着（显示 –），
 * 这样切换播放位置时卡片不会左右跳动。
 * 速度成对排列：反馈在前、指令在后，方便对比。
 */
const STATUS_PRESET = [
  { key: 'nav',       topic: /debug_planning/, path: 'main_state.state',         label: '导航状态' },
  { key: 'score',     topic: /ax_slam_state/, path: 'lidar_matching_score',      label: '定位匹配' },
  { key: 'vlin',      topic: /odom/,         path: 'twist.twist.linear.x',       label: '线速度' },
  { key: 'vlinCmd',   topic: /debug_control/, path: 'control_msg.linear_command', label: '线速指令' },
  { key: 'vang',      topic: /odom/,          path: 'twist.twist.angular.z',      label: '角速度' },
  { key: 'vangCmd',   topic: /debug_control/, path: 'control_msg.angular_command', label: '角速指令' },
];

/** 按路径取值，支持 a.b.c */
function getPath(obj, path) {
  if (!obj) return undefined;
  let cur = obj;
  for (const k of path.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[k];
  }
  return cur;
}

/** 构建状态条槽位：预设的全部建出来，缺数据的留空位 */
function buildStatusDefaults() {
  S.status = [];
  for (const p of STATUS_PRESET) {
    const t = S.topics.find(x => p.topic.test(x));
    // 话题存在才确认字段真的存在；不存在就留空槽（topic=null）
    let ok = false;
    if (t) {
      const idx = S.bag.byTopic.get(t) || [];
      if (idx.length) {
        const m = S.bag.decode(S.bag.entries[idx[Math.min(idx.length - 1, Math.floor(idx.length / 2))]]);
        ok = !!(m && getPath(m, p.path) !== undefined);
      }
    }
    S.status.push({ key: p.key, topic: ok ? t : null, path: p.path, label: p.label, el: null });
  }
}

function renderStatus() {
  const bar = $('statusbar');
  bar.innerHTML = '';
  if (!S.bag) { bar.innerHTML = '<span class="hint2">未加载</span>'; return; }
  if (!S.status.length) {
    bar.innerHTML = '<span class="hint2">点右上角「+ 状态」挑几个字段显示在这里</span>';
    return;
  }
  for (const s of S.status) {
    const el = document.createElement('span');
    el.className = 'chip' + (s.topic ? '' : ' none');
    el.title = s.topic ? `${s.topic}\n${s.path}` : `${s.path}\n（这个 bag 里没有该话题）`;
    el.innerHTML = `<span class="k">${s.label}</span><span class="v">${s.topic ? '–' : '无'}</span>`
      + `<span class="x" title="移除">×</span>`;
    el.querySelector('.x').onclick = ev => {
      ev.stopPropagation();
      S.status = S.status.filter(x => x !== s);
      renderStatus(); updateStatusValues();
    };
    s.el = el.querySelector('.v');
    s.chip = el;
    bar.appendChild(el);
  }
  updateStatusValues();
}

/**
 * 顶栏两个指示灯：手自动 / 急停
 * 手自动  0=自动(绿)  1=手动(蓝)
 * 急停    0=正常(绿)  非0=急停(红)
 * 没有数据时灰掉，不误导
 */
function updateLamps() {
  const lm = $('lampManual'), le = $('lampEstop');
  if (!lm || !le) return;
  if (!S.bag) {
    lm.className = 'lamp off'; le.className = 'lamp off';
    lm.textContent = '手自动'; le.textContent = '急停';
    return;
  }
  const set = (el, base, cls, text) => {
    el.className = 'lamp ' + cls;
    el.textContent = text;
  };
  for (const [el, path, name] of [[lm, 'manual', '手自动'], [le, 'e_stop', '急停']]) {
    const s = lookupStatus(/hardware/, path);
    if (!s || s.v === undefined || s.v === null) {
      el.className = 'lamp off';
      el.textContent = name;
      continue;
    }
    const on = s.v === true || (typeof s.v === 'number' && s.v !== 0);
    if (name === '手自动') set(el, name, on ? 'manual-on' : 'manual-ok', on ? '手动' : '自动');
    else set(el, name, on ? 'estop-on' : 'estop-ok', on ? '急停' : '正常');
  }
}

/** 在状态条配置里找一个字段的当前值（不管它有没有被删掉） */
function lookupStatus(topicRe, path) {
  const t = S.topics.find(x => topicRe.test(x));
  if (!t) return null;
  const e = S.bag.latestBefore(t, S.t + 0.3e9);
  if (!e) return null;
  const m = S.bag.decode(e);
  if (!m) return null;
  return { topic: t, v: getPath(m, path) };
}

/** 每帧刷新状态条数值 */
function updateStatusValues() {
  updateLamps();
  if (!S.bag || !S.status.length) return;
  for (const s of S.status) {
    if (!s.el) continue;
    if (!s.topic) continue;                      // 空槽保持"无"，不动
    const e = S.bag.latestBefore(s.topic, S.t + 0.3e9);
    if (!e) { s.el.textContent = '–'; s.chip.className = 'chip'; continue; }
    const m = S.bag.decode(e);
    const v = m ? getPath(m, s.path) : undefined;
    s.chip.className = 'chip' + chipClass(s.key, v);
    s.el.textContent = fmtVal(v, s.key);
  }
}
/**
 * 语义配色：看一眼颜色就知道状态
 *   手自动  0=自动(绿)  1=手动(蓝)
 *   急停    0=正常(绿)  非0=急停(红)
 */
function chipClass(key, v) {
  if (v === undefined || v === null) return '';
  if (key === 'manual') {
    const on = v === true || (typeof v === 'number' && v !== 0);
    return on ? 'info' : 'ok';            // 蓝 / 绿
  }
  if (key === 'estop') {
    const on = v === true || (typeof v === 'number' && v !== 0);
    return on ? 'bad' : 'ok';             // 红 / 绿
  }
  return '';
}
function fmtVal(v, key) {
  if (v === undefined || v === null) return '–';
  if (key === 'manual') {
    const on = v === true || (typeof v === 'number' && v !== 0);
    return on ? '手动' : '自动';
  }
  if (key === 'estop') {
    const on = v === true || (typeof v === 'number' && v !== 0);
    return on ? '急停' : '正常';
  }
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'number') {
    if (!isFinite(v)) return '–';
    const a = Math.abs(v);
    if (a >= 1e5 || (a < 1e-3 && a > 0)) return v.toExponential(1);
    // 定宽：始终 4 位小数，位数一致才不会左右抖
    return v.toFixed(4);
  }
  return String(v) || '(空)';
}

// ═══════════════ 话题多选下拉 ═══════════════
/**
 * 「地图要素」下拉面板：按数据来源分组，逐类开关。
 * 数量从 mfStat 取；没有该话题时给一句人话提示，而不是空白面板。
 */
function renderMfMenu() {
  const box = $('mfMenu');
  if (!box) return;
  box.innerHTML = '';

  const tp = mapInfoTopic();
  if (!tp) {
    const d = document.createElement('div');
    d.className = 'grp';
    d.textContent = '本 bag 没有地图要素话题';
    box.appendChild(d);
    return;
  }
  // 面板可能先于首帧绘制被打开，这里保证统计已有值
  if (!S.mfStat) currentFeatures();
  const st = S.mfStat || {};

  const grp = t => { const g = document.createElement('div'); g.className = 'grp'; g.textContent = t; box.appendChild(g); };
  const item = (label, cnt, on, cb) => {
    const it = document.createElement('div');
    it.className = 'it' + (on ? ' on' : '');
    const ck = document.createElement('span');
    ck.className = 'ck'; ck.textContent = on ? '✓' : '';
    it.appendChild(ck);
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = cnt == null ? label : label + ' (' + cnt + ')';
    it.appendChild(nm);
    it.onclick = () => { cb(); renderMfMenu(); draw(); };
    box.appendChild(it);
  };

  grp('数据来源 · ' + tp);
  for (const g of MF_GROUPS) {
    item(g.label, st[g.key] || 0, S.mfLayer[g.key],
         () => { S.mfLayer[g.key] = !S.mfLayer[g.key]; });
  }
  item('标记点', st.marker || 0, S.mfLayer.marker,
       () => { S.mfLayer.marker = !S.mfLayer.marker; });

  grp('显示');
  item('标记点名称', null, S.mfLayer.name,
       () => { S.mfLayer.name = !S.mfLayer.name; });

  grp('快捷');
  const all = document.createElement('div');
  all.className = 'it';
  all.innerHTML = '<span class="ck"></span><span class="nm">全部显示 / 全部隐藏</span>';
  all.onclick = () => {
    const anyOn = MF_GROUPS.some(g => S.mfLayer[g.key]) || S.mfLayer.marker;
    for (const g of MF_GROUPS) S.mfLayer[g.key] = !anyOn;
    S.mfLayer.marker = !anyOn;
    renderMfMenu(); draw();
  };
  box.appendChild(all);

  const tip = document.createElement('div');
  tip.className = 'grp';
  tip.style.textTransform = 'none';
  tip.style.letterSpacing = '0';
  let tipTxt = '共 ' + (st.total || 0) + ' 个要素';
  if (!S.visible.has(tp)) tipTxt += ' · 话题未勾选，整体不显示';
  else if (!st.total) tipTxt += ' · 没解析出几何，见控制台(F12)输出';
  tip.textContent = tipTxt;
  box.appendChild(tip);
}

function renderTopicMenu() {
  const box = $('topicMenu');
  box.innerHTML = '';
  const visibleSwatches = [];          // 记录本次渲染出来的取色器
  const groups = new Map();
  for (const t of S.topics) {
    const r = S.roles.get(t) || 'data';
    if (!groups.has(r)) groups.set(r, []);
    groups.get(r).push(t);
  }
  for (const [role, list] of groups) {
    const g = document.createElement('div');
    g.className = 'grp';
    g.textContent = ROLE_LABEL[role] || '数据';
    box.appendChild(g);
    for (const t of list) {
      const cnt = (S.bag.byTopic.get(t) || []).length;
      const it = document.createElement('div');
      it.className = 'it' + (S.visible.has(t) ? ' on' : '');
      // 勾选标记：不选中就留空，一眼看出哪些在画
      const ck = document.createElement('span');
      ck.className = 'ck';
      ck.textContent = S.visible.has(t) ? '✓' : '';
      it.appendChild(ck);
      // 颜色可调：小方块本身就是一个 <input type=color> 的触发器
      const swatch = document.createElement('input');
      swatch.type = 'color';
      swatch.className = 'swatch';
      swatch.value = S.color.get(t) || ROLE_COLOR[role] || '#8d6e63';
      swatch.title = '点这里改这个话题的绘制颜色';
      swatch.onclick = ev => ev.stopPropagation();
      swatch.oninput = ev => {
        ev.stopPropagation();
        const v = swatch.value;
        S.color.set(t, v);
        S.userColor.set(t, v);          // 记住用户改过，别被自动配色覆盖
        draw();
      };
      // 右键恢复默认配色
      swatch.oncontextmenu = ev => {
        ev.preventDefault(); ev.stopPropagation();
        S.userColor.delete(t);
        S.color.set(t, fixedColor(t) || defaultColorFor(t));
        swatch.value = S.color.get(t);
        draw();
      };
      visibleSwatches.push([t, swatch]);
      it.appendChild(swatch);

      const nm = document.createElement('span');
      nm.className = 'nm'; nm.textContent = t;
      const ty = document.createElement('span');
      ty.className = 'ty'; ty.textContent = cnt;
      it.appendChild(nm); it.appendChild(ty);

      it.onclick = ev => {
        ev.stopPropagation();
        if (S.visible.has(t)) S.visible.delete(t); else S.visible.add(t);
        it.classList.toggle('on');
        ck.textContent = S.visible.has(t) ? '✓' : '';
        draw(); renderTimeline(); updateStatusValues();
      };
      box.appendChild(it);
    }
  }
}

// ═══════════════ 状态字段选择下拉 ═══════════════
/** 摊平一条消息，保留类型和字符串（状态条要显示字符串，如导航状态 Idle） */
function flatWithTypes(obj, prefix, out, depth) {
  if (depth > 4) return out;
  for (const k in obj) {
    const v = obj[k];
    if (v === null || v === undefined) continue;
    const p = prefix ? prefix + '.' + k : k;
    if (typeof v === 'number') out[p] = isFinite(v) ? 'num' : '?';
    else if (typeof v === 'boolean') out[p] = 'bool';
    else if (typeof v === 'string') out[p] = 'str';
    else if (typeof v === 'object') {
      if (typeof v.sec === 'number' && typeof v.nanosec === 'number') out[p] = 'time';
      else if (!Array.isArray(v) && v.BYTES_PER_ELEMENT === undefined) flatWithTypes(v, p, out, depth + 1);
    }
  }
  return out;
}

function renderStatMenu() {
  const box = $('statMenu');
  box.innerHTML = '';
  const skip = new Set(['image', 'map', 'tf', 'marker']);
  for (const t of S.topics) {
    if (skip.has(S.roles.get(t))) continue;
    const idx = S.bag.byTopic.get(t) || [];
    if (!idx.length) continue;
    const m = S.bag.decode(S.bag.entries[idx[0]]);
    if (!m) continue;
    const fields = flatWithTypes(m, '', Object.create(null), 0);
    const keys = Object.keys(fields);
    if (!keys.length) continue;
    const g = document.createElement('div');
    g.className = 'grp';
    g.textContent = t;
    box.appendChild(g);
    for (const k of keys) {
      const has = S.status.some(s => s.topic === t && s.path === k);
      const it = document.createElement('div');
      it.className = 'it sub' + (has ? ' on' : '');
      it.innerHTML = `<span class="box"></span><span class="nm">${k}</span><span class="ty">${fields[k]}</span>`;
      it.onclick = ev => {
        ev.stopPropagation();
        if (has) S.status = S.status.filter(s => !(s.topic === t && s.path === k));
        else S.status.push({ topic: t, path: k, label: k.split('.').pop(), el: null });
        renderStatus();
        renderStatMenu();
      };
      box.appendChild(it);
    }
  }
}

// ═══════════════ 叠加信息（已并入状态条） ═══════════════
function updateOverlay() { updateStatusValues(); }

// ═══════════════ 时间轴 ═══════════════
const ROW_H = 22;
let tlW = 800;
/** 时间轴可视窗口：[v0, v0+dur/zoom]，单位是秒（相对 bag 开头） */
function tlView() {
  const dur = S.bag ? Math.max((S.bag.endTime - S.bag.startTime) / 1e9, 0.001) : 1;
  const zd = dur / Math.max(1, S.tlZoom);
  let v0 = S.tlView0;
  if (v0 < 0) v0 = 0;
  if (v0 + zd > dur) v0 = Math.max(0, dur - zd);
  return { v0, vd: Math.min(zd, dur) };
}
function tlClampView() {
  const { v0 } = tlView();
  S.tlView0 = v0;
}

function renderTimeline() {
  if (!S.bag) return;
  const rows = S.topics.filter(t => S.visible.has(t));
  const box = $('tlscroll');
  tlW = Math.max(box.clientWidth, 600);
  const H = rows.length * ROW_H;
  tlcv.style.width = tlW + 'px';
  tlcv.style.height = H + 'px';
  tlcv.width = tlW * DPR; tlcv.height = H * DPR;
  tlctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  tlctx.clearRect(0, 0, tlW, H);

  const { v0, vd } = tlView();
  tlClampView();

  // 标签。当前"关注话题"那一行高亮 —— 右下角的内容面板显示的就是它，
  // 不给视觉标记的话，用户点了别的话题行根本看不出关注点已经变了。
  const lb = $('tllabels');
  lb.innerHTML = rows.map(t => `<div class="lb${t === S.selTopic ? ' sel' : ''}" title="${t}">${t}</div>`).join('');
  lb.style.paddingTop = '0';
  lb.scrollTop = box.scrollTop;

  // 纵向时间网格（放大后用来看消息间隔）
  tlctx.strokeStyle = 'rgba(127,127,127,.22)';
  tlctx.fillStyle = '#8b949e';
  tlctx.font = '9px sans-serif';
  const nice = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600];
  // 墙上时间标签（"09-07 16:35:21"）比 "12.5s" 宽得多，网格要稀一些才不重叠
  const maxTicks = (vd >= 1 && vd <= 600) ? 6 : 12;
  const gridStep = nice.find(s => vd / s <= maxTicks) || vd / 4;
  // 刻度标签：窗口较宽时用"月日 时分秒"（便于定位到具体时刻），
  // 放大到秒级以下时切回相对毫秒（这时要看的是消息间隔，不是墙上时间）
  const useWall = vd >= 1;
  for (let tv = Math.ceil(v0 / gridStep) * gridStep; tv <= v0 + vd; tv += gridStep) {
    const x = Math.round((tv - v0) / vd * tlW);
    tlctx.beginPath(); tlctx.moveTo(x, 0); tlctx.lineTo(x, H); tlctx.stroke();
    const label = useWall
      ? fmtRos(S.bag.startTime + tv * 1e9, { sec: true })
      : fmtSec(tv);
    tlctx.fillText(label, x + 2, 9);
  }

  rows.forEach((t, i) => {
    const y = i * ROW_H;
    if (i % 2) { tlctx.fillStyle = 'rgba(255,255,255,.03)'; tlctx.fillRect(0, y, tlW, ROW_H); }
    const idx = S.bag.byTopic.get(t) || [];
    const role = S.roles.get(t) || 'data';
    tlctx.fillStyle = S.color.get(t) || ROLE_COLOR[role] || '#888';
    let lastX = -99;
    for (const k of idx) {
      const e = S.bag.entries[k];
      const ts = (e.stamp - S.bag.startTime) / 1e9;
      if (ts < v0 - vd * 0.02 || ts > v0 + vd * 1.02) continue;   // 视窗外不画
      const x = Math.round((ts - v0) / vd * tlW);
      if (Math.abs(x - lastX) < 1) continue;
      tlctx.fillRect(x, y + 4, 2, ROW_H - 8);
      lastX = x;
    }
  });
  cacheTimeline();     // 先把静态层存好，之后每帧只重画播放头
  drawPlayhead();
}

/**
 * 播放头。
 * 时间轴的静态内容（背景/网格/消息点）缓存在 tlBase 里，
 * 每帧先把它贴回来再画一条竖线 —— 否则播放时竖线会一帧帧
 * 累加，把走过的区域糊成一大片绿色。
 */
let tlBase = null;
function cacheTimeline() {
  if (!tlBase) tlBase = document.createElement('canvas');
  if (tlBase.width !== tlcv.width || tlBase.height !== tlcv.height) {
    tlBase.width = tlcv.width; tlBase.height = tlcv.height;
  }
  const c = tlBase.getContext('2d');
  c.setTransform(1, 0, 0, 1, 0, 0);
  c.clearRect(0, 0, tlBase.width, tlBase.height);
  c.drawImage(tlcv, 0, 0);
}
function drawPlayhead() {
  if (!S.bag || !tlBase) return;
  tlctx.setTransform(1, 0, 0, 1, 0, 0);
  tlctx.clearRect(0, 0, tlcv.width, tlcv.height);
  tlctx.drawImage(tlBase, 0, 0);
  tlctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  const rows = S.topics.filter(t => S.visible.has(t));
  const { v0, vd } = tlView();
  const ts = (S.t - S.bag.startTime) / 1e9;
  const x = (ts - v0) / vd * tlW;
  if (x < -4 || x > tlW + 4) return;
  // 只画一条竖线，恒定红色（播放/暂停不变色），不填充已播放区域
  tlctx.fillStyle = '#ef4444';
  tlctx.fillRect(x - 1, 0, 2, rows.length * ROW_H);
}

// ═══════════════ 消息详情 ═══════════════
/**
 * 切「关注话题」——右下角内容面板显示的就是这个话题。
 *
 * 为什么要单独抽出来：点击时间轴上任意一行都要走它，而且必须
 * 【立即】刷新面板。原来只有 renderFrame 里那段跟随逻辑，而它
 * 只认 S.selTopic、不认"用户点的是哪一行"，于是点了别的话题，
 * 面板纹丝不动，看着就像功能坏了。
 */
function setSelTopic(t) {
  if (!t) return;
  S.selTopic = t;
  S.selected = null;      // 强制重画，否则同一条消息会被 e !== S.selected 挡掉
  markSelLabel();
  refreshDetail(true);
}

/** 只改标签的高亮 class，不重画 canvas（重画有明显开销，播放时每帧都在跑） */
function markSelLabel() {
  const lb = $('tllabels');
  if (!lb) return;
  const rows = S.topics.filter(x => S.visible.has(x));
  Array.prototype.forEach.call(lb.children, (el, i) => {
    el.classList.toggle('sel', rows[i] === S.selTopic);
  });
}

/**
 * 让右下角面板显示「关注话题在当前时刻的那条消息」。
 *
 * force=true 时无条件重画（切换话题用）；否则仍沿用"消息没变就不重画"
 * 的策略，避免播放时每帧都做一次 JSON.stringify。
 */
function refreshDetail(force) {
  const box = $('json');
  if (!box || !S.bag) return;
  if (!S.selTopic) { showDetail(null); return; }
  const e = S.bag.latestBefore(S.selTopic, S.t + 0.3e9);
  if (e) {
    if (force || e !== S.selected) { S.selected = e; showDetail(e); }
    return;
  }
  // 这个话题在当前时刻之前没有任何消息。必须明确说出来 ——
  // 否则面板还留着上一个话题的内容，用户会以为点击没生效。
  S.selected = null;
  const rel = (S.t - S.bag.startTime) / 1e9;
  box.textContent = `${S.selTopic}\n在 ${rel.toFixed(3)}s 之前没有消息`;
  const si = $('selinfo');
  if (si) si.textContent = `${S.selTopic} @ 此处无数据`;
}

function showDetail(entry) {
  if (!entry) { $('json').textContent = '点击时间轴上任意一行，这里显示该话题在当前时刻的消息'; $('selinfo').textContent = ''; return; }
  const m = S.bag.decode(entry);
  const conn = S.bag.connections.get(entry.connId);
  const reader = S.bag.readerFor(entry.connId);
  const consts = reader ? reader.constants : [];
  const txt = JSON.stringify(m, (k, v) => {
    if (v && v.BYTES_PER_ELEMENT !== undefined) return `Array(${v.length})`;
    if (typeof v === 'number') return +v.toFixed(6);
    return v;
  }, 2);
  $('json').innerHTML = highlight(txt, consts);
  const rel = (entry.stamp - S.bag.startTime) / 1e9;
  $('selinfo').textContent = `${entry.topic}  @ ${rel.toFixed(3)}s  [${conn ? conn.type : '?'}]`;
}
function escapeHtml(s) { return s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
function highlight(json, consts) {
  return escapeHtml(json).replace(
    /("(\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*")(\s*:)?|\b(true|false)\b|\b(null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (mm, str, _2, colon) => {
      let cls = 'jn';
      if (str) cls = colon ? 'jk' : 'jv';
      else if (/true|false/.test(mm)) cls = 'jb';
      else if (/null/.test(mm)) cls = 'jp';
      let extra = '';
      const num = mm.match(/^-?\d+$/);
      if (num && consts.length) {
        const hit = consts.filter(c => c.constValue === num[0]).map(c => c.name);
        if (hit.length) extra = ` <span class="jp">// ${hit.join(' | ')}</span>`;
      }
      return `<span class="${cls}">${mm}</span>${extra}`;
    });
}

// ═══════════════ 播放 ═══════════════
let rafId = 0, lastWall = 0;
function tick(ts) {
  if (!S.playing) return;
  if (!lastWall) lastWall = ts;
  const dt = (ts - lastWall) / 1000;
  lastWall = ts;
  S.t += dt * S.speed * 1e9;
  if (S.t >= S.bag.endTime) { S.t = S.bag.endTime; setPlaying(false); }
  renderFrame();
  rafId = requestAnimationFrame(tick);
}
function setPlaying(p) {
  S.playing = p;
  lastWall = 0;
  $('btnPlay').textContent = p ? '⏸ 暂停' : '▶ 播放';
  $('btnPlay').classList.toggle('active', p);
  if (p) rafId = requestAnimationFrame(tick); else cancelAnimationFrame(rafId);
}
function renderFrame() {
  draw(); drawPlayhead(); updateOverlay(); drawCams(); drawChart();
  // 详情面板显示的是"关注话题在当前时刻的那条消息"——它跟着播放头实时走。
  // 关注话题可以被切换：点时间轴某一行、或点左边的标签名。
  refreshDetail(false);
  const rel = (S.t - S.bag.startTime) / 1e9;
  const dur = (S.bag.endTime - S.bag.startTime) / 1e9;
  const wall = fmtRos(S.t, { ms: true });
  $('clock').innerHTML = `<b class="wall">${wall}</b>` +
    `<span class="rel">${rel.toFixed(2)}s / ${dur.toFixed(1)}s</span>`;
  // 选中消息时额外显示它的精确时间（消息自己的 header.stamp，可能比播放头略早）
  const wt = $('wallTime');
  if (wt) {
    if (S.selected) wt.textContent = `消息时间 ${fmtRos(S.selected.stamp, { ms: true })}`;
    else wt.textContent = '';
  }
}

function seek(t) {
  S.t = Math.max(S.bag.startTime, Math.min(S.bag.endTime, t));
  renderFrame();
  updateLegendValues();
  // 时间轴放大后，播放头滚出视窗就自动跟过去
  if (S.tlZoom > 1.001 && S.followTl) {
    const { v0, vd } = tlView();
    const ts = (S.t - S.bag.startTime) / 1e9;
    if (ts < v0 + vd * 0.05 || ts > v0 + vd * 0.95) {
      S.tlView0 = ts - vd / 2;
      tlClampView(); renderTimeline();
    }
  }
}

// ═══════════════ 事件绑定 ═══════════════
let DPR = window.devicePixelRatio || 1;
function resizeCanvas() {
  DPR = window.devicePixelRatio || 1;
  const r = cv.getBoundingClientRect();
  cv.width = Math.max(1, r.width * DPR);
  cv.height = Math.max(1, r.height * DPR);
  if (S.bag) { draw(); renderTimeline(); }
}
window.addEventListener('resize', resizeCanvas);

// 标题旁的版本号
(() => { const v = $('ver'); if (v) v.textContent = 'v' + APP_VERSION; })();

// 三个下拉菜单：点按钮开合，点外面或点菜单项后关闭
(() => {
  const dds = [['btnTopics', 'topicDD'], ['btnAddStat', 'statDD'],
               ['btnAddChart', 'chartDD'], ['btnMf', 'mfDD']];
  for (const [btn, dd] of dds) {
    const b = $(btn), d = $(dd);
    if (!b || !d) continue;
    b.onclick = e => {
      e.stopPropagation();
      const wasOpen = d.classList.contains('open');
      dds.forEach(([, o]) => $(o).classList.remove('open'));
      if (!wasOpen) d.classList.add('open');
    };
  }
  window.addEventListener('click', () => dds.forEach(([, o]) => $(o).classList.remove('open')));
  window.addEventListener('pointerdown', e => {
    if (!e.target.closest('.dd')) dds.forEach(([, o]) => $(o).classList.remove('open'));
  });
})();

$('btnOpen').onclick = () => $('fileInput').click();
$('fileInput').onchange = e => { if (e.target.files[0]) openFile(e.target.files[0]); e.target.value = ''; };
$('btnFit').onclick = () => { fitView(); draw(); };
$('chartTopic') && ($('chartTopic').onchange = e => {});
$('btnClearChart').onclick = () => clearSeries();

// 曲线：滚轮缩放横轴 · 拖动平移 · 移动显示该时刻数值 · 双击/按钮重置
(() => {
  const el = $('chartcv');
  if (!el) return;
  el.addEventListener('wheel', e => {
    if (!S.bag || !S.series.length) return;
    e.preventDefault();
    const r = el.getBoundingClientRect();
    const fx = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const { v0, vd, dur } = chView();
    if (e.shiftKey) {
      S.chView0 = v0 + (e.deltaY > 0 ? 1 : -1) * vd * 0.15;
      chClamp(); drawChart(); updateLegendValues(); return;
    }
    const tAt = v0 + fx * vd;
    const k = e.deltaY < 0 ? 1.4 : 1 / 1.4;
    S.chZoom = Math.max(1, Math.min(2000, S.chZoom * k));
    const nvd = dur / S.chZoom;
    S.chView0 = tAt - fx * nvd;
    chClamp();
    renderLegend(); drawChart();
  }, { passive: false });

  el.addEventListener('mousemove', e => {
    if (!S.bag) return;
    const r = el.getBoundingClientRect();
    const fx = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const { v0, vd } = chView();
    S.hoverT = v0 + fx * vd;
    drawChart();
  });
  el.addEventListener('mouseleave', () => { S.hoverT = null; drawChart(); });

  // 拖动平移
  let pan = null;
  el.addEventListener('pointerdown', e => {
    if (!S.bag || e.button !== 0) return;
    pan = { x: e.clientX, v0: chView().v0, vd: chView().vd, moved: false };
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener('pointermove', e => {
    if (!pan) return;
    const dx = e.clientX - pan.x;
    if (Math.abs(dx) > 2) pan.moved = true;
    S.chView0 = pan.v0 - dx / Math.max(1, el.clientWidth) * pan.vd;
    chClamp(); drawChart(); updateLegendValues();
  });
  const end = () => { pan = null; };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);

  el.addEventListener('dblclick', () => {
    S.chZoom = 1; S.chView0 = 0;
    renderLegend(); drawChart(); updateLegendValues();
  });
})();
$('btnChReset') && ($('btnChReset').onclick = () => {
  S.chZoom = 1; S.chView0 = 0;
  renderLegend(); drawChart(); updateLegendValues();
});
$('btnRec').onclick = () => {
  if (REC.on) stopRec();
  else startRec($('recMode').value || 'webm');
};
$('btnLegend').onclick = () => {
  S.showLegend = !S.showLegend;
  $('btnLegend').classList.toggle('active', S.showLegend);
  draw();
};
$('btnTf').onclick = () => {
  S.tfOn = !S.tfOn;
  S.tfCache.clear();
  $('btnTf').classList.toggle('active', S.tfOn);
  buildTraj();
  fitView();
  draw();
  updateOverlay();
};
$('btnPlay').onclick = () => setPlaying(!S.playing);
$('speed').onchange = e => S.speed = +e.target.value;

window.addEventListener('keydown', e => {
  if (e.code === 'Space' && S.bag) { e.preventDefault(); setPlaying(!S.playing); }
});

// 拖放
['dragover', 'dragenter'].forEach(ev => window.addEventListener(ev, e => { e.preventDefault(); view.classList.add('dragover'); }));
['dragleave', 'drop'].forEach(ev => window.addEventListener(ev, e => { e.preventDefault(); view.classList.remove('dragover'); }));
window.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f) openFile(f); });

// 时间轴交互
$('tlscroll').addEventListener('click', e => {
  if (!S.bag || tlDragged) return;         // 拖过就不当点击
  const r = tlcv.getBoundingClientRect();
  const x = e.clientX - r.left;
  const y = e.clientY - r.top;
  const { v0, vd } = tlView();
  // 按 y 算出点的是哪一个话题行，把右下角内容面板的关注话题切过去。
  // 之前这里只 seek、不换话题，所以面板永远停在打开 bag 时自动挑的那个话题上，
  // 点哪一行都没反应 —— 看着就像功能坏了。
  const rows = S.topics.filter(t => S.visible.has(t));
  const i = Math.floor(y / ROW_H);
  if (i >= 0 && i < rows.length) setSelTopic(rows[i]);
  seek(S.bag.startTime + (v0 + x / tlW * vd) * 1e9);
});
$('tlscroll').addEventListener('scroll', () => { $('tllabels').scrollTop = $('tlscroll').scrollTop; });

// 点左边的标签名也能切换关注话题 —— 话题名长、时间轴行又窄时，
// 直接点名字比去数第几行方便得多。
$('tllabels').addEventListener('click', e => {
  if (!S.bag) return;
  const el = e.target.closest ? e.target.closest('.lb') : null;
  if (!el) return;
  const rows = S.topics.filter(t => S.visible.has(t));
  const i = Array.prototype.indexOf.call($('tllabels').children, el);
  if (i >= 0 && i < rows.length) setSelTopic(rows[i]);
});

// 时间轴横向缩放：滚轮=以光标为中心缩放，右键/中键拖动=平移，Shift+滚轮=平移
let tlDragged = false;
$('tlscroll').addEventListener('wheel', e => {
  if (!S.bag) return;
  e.preventDefault();
  const r = tlcv.getBoundingClientRect();
  const fx = Math.max(0, Math.min(1, (e.clientX - r.left) / tlW));
  const dur = Math.max((S.bag.endTime - S.bag.startTime) / 1e9, 0.001);
  const { v0, vd } = tlView();

  if (e.shiftKey) {                       // Shift+滚轮 = 平移
    S.tlView0 = v0 + (e.deltaY > 0 ? 1 : -1) * vd * 0.15;
    tlClampView(); renderTimeline(); return;
  }
  const tAt = v0 + fx * vd;               // 光标处对应的时刻
  const k = e.deltaY < 0 ? 1.4 : 1 / 1.4;
  S.tlZoom = Math.max(1, Math.min(2000, S.tlZoom * k));
  const nvd = dur / S.tlZoom;
  S.tlView0 = tAt - fx * nvd;             // 保持光标下的时刻不动
  tlClampView();
  updateTlZoomInfo();
  renderTimeline();
}, { passive: false });

// 拖动平移（按住中键或右键拖；左键拖是框选时间，会触发点击，故不用）
(() => {
  let pan = null;
  const el = $('tlscroll');
  el.addEventListener('pointerdown', e => {
    if (!S.bag || (e.button !== 1 && e.button !== 2)) return;
    e.preventDefault();
    pan = { x: e.clientX, v0: tlView().v0, vd: tlView().vd };
    tlDragged = true;
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener('pointermove', e => {
    if (!pan) return;
    const dx = e.clientX - pan.x;
    S.tlView0 = pan.v0 - dx / tlW * pan.vd;
    tlClampView();
    renderTimeline();
  });
  const end = () => { if (pan) { pan = null; setTimeout(() => tlDragged = false, 0); } };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
  el.addEventListener('contextmenu', e => { if (S.bag) e.preventDefault(); });
})();

/** 在 tlhead 上显示当前缩放倍数，并给个"重置"按钮 */
function updateTlZoomInfo() {
  const el = $('tlZoomInfo');
  if (!el) return;
  if (S.tlZoom <= 1.001) { el.textContent = ''; return; }
  const { vd } = tlView();
  el.textContent = `×${S.tlZoom.toFixed(S.tlZoom < 10 ? 1 : 0)} · 窗口 ${vd.toFixed(vd < 1 ? 2 : 1)}s`;
}
$('btnTlReset') && ($('btnTlReset').onclick = () => {
  S.tlZoom = 1; S.tlView0 = 0; updateTlZoomInfo(); renderTimeline();
});

// 地图缩放/平移
cv.addEventListener('wheel', e => {
  if (!S.bag) return;
  e.preventDefault();
  const r = cv.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const [wx, wy] = s2w(mx, my);
  const k = e.deltaY < 0 ? 1.15 : 1 / 1.15;
  V.s *= k;
  const [nx, ny] = w2s(wx, wy);
  V.tx += mx - nx; V.ty += my - ny;
  draw();
}, { passive: false });

// 区域2 / 区域3 之间的分隔条：拖动改变时间轴高度
(() => {
  const hdiv = $('hdiv'), bottom = $('bottom'), center = $('center');
  let st = null;
  hdiv.addEventListener('pointerdown', e => {
    hdiv.setPointerCapture(e.pointerId);
    st = { y: e.clientY, h: bottom.getBoundingClientRect().height };
  });
  window.addEventListener('pointermove', e => {
    if (!st) return;
    const ch = center.getBoundingClientRect().height;
    if (ch <= 0) return;
    // 地图至少留 120px，时间轴至少 150px
    const h = Math.max(150, Math.min(ch - 120, st.h - (e.clientY - st.y)));
    bottom.style.flexBasis = (h / ch * 100).toFixed(2) + '%';
  });
  const end = () => {
    if (!st) return;
    st = null;
    resizeCanvas();
    if (S.bag) renderTimeline();
  };
  window.addEventListener('pointerup', end);
  window.addEventListener('pointercancel', end);
})();

// 地图 / 相机 之间的竖分隔条
(() => {
  const vdiv = $('vdiv'), box = $('cams'), view = $('view');
  let st = null;
  vdiv.addEventListener('pointerdown', e => {
    vdiv.setPointerCapture(e.pointerId);
    st = { x: e.clientX, w: box.getBoundingClientRect().width };
  });
  window.addEventListener('pointermove', e => {
    if (!st) return;
    const vw = view.getBoundingClientRect().width;
    if (vw <= 0) return;
    // 地图至少留 220px，相机至少 160px
    const w = Math.max(160, Math.min(vw - 220, st.w - (e.clientX - st.x)));
    box.style.flexBasis = (w / vw * 100).toFixed(2) + '%';
  });
  const end = () => { if (st) { st = null; resizeCanvas(); } };
  window.addEventListener('pointerup', end);
  window.addEventListener('pointercancel', end);
})();

let dragging = false, dragS = null;
cv.addEventListener('mousedown', e => { dragging = true; dragS = { x: e.clientX, y: e.clientY, tx: V.tx, ty: V.ty }; });
window.addEventListener('mousemove', e => {
  if (!dragging) return;
  V.tx = dragS.tx + (e.clientX - dragS.x);
  V.ty = dragS.ty + (e.clientY - dragS.y);
  draw();
});
window.addEventListener('mouseup', () => dragging = false);

resizeCanvas();

// 调试钩子：浏览器控制台里可用 __bag.S 查看状态，__bag.openFile(file) 手动加载
window.__bag = { S, openFile, renderTopicMenu, renderStatMenu, renderChartMenu, renderStatus, updateStatusValues, toggleSeries, clearSeries, draw, renderTimeline, seek, showDetail, fitView, V, buildTraj, updateOverlay, drawCams, decodeImage, drawChart, renderLegend, autoPickSeries, flatWithTypes, getPath, sniffImageType, typeFromFormat, hexHead, decodeCompressed, pathPoints, defaultColorFor, fixedColor, drawRobot, encodeGIF, buildPalette, quantize, lzwEncode, REC, grabFrame, composeFrame, tlView, chView, seriesValueAt, fmtSec, fmtRos, updateLegendValues, renderFrame, drawPlayhead, renderTimeline, renderLegend, setSelTopic, refreshDetail, markSelLabel, ROW_H };
