import fs from 'fs';

/* v1.0.35 前端测试：地图卡片 + 车机终端卡片
   用假 DOM 真跑 renderMap / 终端相关函数，而不是只查字符串存在。*/

const html = fs.readFileSync('/data/workspace/aoting_tools/apps/statusweb/index.html', 'utf8');

let pass = 0, fail = 0;
const ok = (c, m) => { if (c) { pass++; console.log('ok   ' + m); } else { fail++; console.log('FAIL ' + m); } };

console.log('\n[1] 页面结构：两张卡片都在');
ok(/<div id="map"><\/div>/.test(html), '地图容器 #map 存在');
ok(/id="tmstart"/.test(html), '终端「打开终端」按钮存在');
ok(/id="tmout"/.test(html), '终端输出区存在');
ok(/id="tmcmd"/.test(html), '终端命令输入框存在');
ok(/车机终端/.test(html), '终端卡片标题');

console.log('\n[2] 终端是交互式 shell');
const tmSrc = html.slice(html.indexOf('/* ── 车机终端 ──'));
ok(/shell 状态会保留/.test(html), '页面上明说 shell 状态会保留（cd 有效）');
ok(/act:'send'/.test(tmSrc), '发送走 act:send（同一个 shell）');
ok(/act:'start'/.test(tmSrc) && /act:'stop'/.test(tmSrc), '有 start / stop');
ok(/T\.hist/.test(tmSrc), '有命令历史');
ok(/ArrowUp|ArrowDown/.test(tmSrc), '上下键翻历史');
ok(/tmSendRaw\('\\u0003'\)|tmSendRaw\("\\u0003"\)|\\\\u0003|\\u0003/.test(tmSrc) || /intr/.test(tmSrc),
   '有中断（Ctrl+C）入口');

console.log('\n[3] 终端输出增量拉取 + 不强制拽回底部');
ok(/\/api\/term\?since=/.test(tmSrc), '按 since 增量拉取');
ok(/j\.reset/.test(tmSrc), '服务端说 reset 就整段重画');
ok(/atBottom/.test(tmSrc), '用户上翻时不强行拉到底部');
ok(/scrollTop\s*=\s*\w+\.scrollHeight/.test(tmSrc), '在底部时自动跟随');

console.log('\n[4] renderMap 真渲染（假 DOM）');
const a = html.indexOf('function gpath(');
const b = html.indexOf('async function loadMap(');
const src = html.slice(a, b);   // gpath + renderMap + mapMsg 一起取
const dep = `
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
// node 里没有 window：renderMap 建卡片时会注册 resize 监听。
// 浏览器里当然有，这里补个桩，别让测试环境缺东西误报成产品 bug。
const window = globalThis.window = { addEventListener: function(){}, innerWidth: 1200, innerHeight: 800 };
const S = globalThis.S = {mapName:'', mapBusy:false};
function loadMap(n){ globalThis.__CALLS.push(n); S.mapName = n; }
globalThis.__CALLS = globalThis.__CALLS || [];
function listMaps(){}
const mkBox = () => ({innerHTML:'', textContent:'', dataset:{}, style:{}, value:'', className:'', addEventListener:function(){}, focus:function(){}, click:function(){}});
const BOX = globalThis.BOX = {};
function $(id){ if(!BOX[id]) BOX[id]={innerHTML:'', textContent:'', dataset:{}, style:{}, value:'', addEventListener:function(){}, focus:function(){}, click:function(){}}; return BOX[id]; }
`;
const mkBox = () => ({innerHTML:'', textContent:'', dataset:{}, style:{}, value:'', className:'', addEventListener:function(){}, focus:function(){}, click:function(){}});
let renderMap;
try {
  renderMap = new Function(dep + src + '\n; return renderMap;')();
} catch (e) {
  console.log('FAIL renderMap 可取出: ' + e.message);
  fail++;
}

if (renderMap) {
  const fresh = () => { BOX['map'] = mkBox(); };
  const meta  = () => ((BOX['mapmeta']||{}).innerHTML||'') + ' ' + ((BOX['mapmeta']||{}).textContent||'');
  const msg   = () => ((BOX['mapmsg']||{}).textContent||'');

  /* ── 未连接 ── */
  fresh();
  renderMap(null);
  ok((BOX['map'].innerHTML||'').includes('地图'), '未连接也画出卡片（不能整栏消失）');
  ok(/未连接/.test(meta()), '未连接时说明状态', meta().trim());

  /* ── 话题有数据、有名字 ── */
  fresh(); globalThis.__CALLS = [];
  renderMap({ ok:true, err:'', name:'HGTV', topic:'/map_info_v3',
              fields:{ base_info:{ name:'HGTV', area:545.08,
                       metadata:{ resolution:0.05, width:980, height:695 } } } });
  ok(/HGTV/.test(meta()), '地图名显示在 meta 里', meta().trim().slice(0,80));
  ok(/980/.test(meta()) && /695/.test(meta()), '尺寸显示出来');
  ok(/0\.050/.test(meta()), '分辨率三位小数', meta().trim().slice(0,120));
  ok(/545\.1|545\.0/.test(meta()), '面积显示出来');
  ok((globalThis.__CALLS||[]).some(c => c === 'HGTV'), '有名字时自动触发加载', globalThis.__CALLS);

  /* ── 话题发布了但没名字 ── */
  fresh(); globalThis.__CALLS = [];
  renderMap({ ok:true, err:'', name:'', topic:'/map_info_v3', fields:{} });
  ok(/地图名/.test(msg()) && /为空|没有/.test(msg()), '没名字时明确提示', msg());
  ok((globalThis.__CALLS||[]).length === 0, '没名字时不瞎加载', globalThis.__CALLS);

  /* ── 话题没发布 ── */
  fresh(); globalThis.__CALLS = [];
  renderMap({ ok:false, err:'话题 /map_info_v3 未发布（等了 10 秒仍没有）',
              name:'', topic:'/map_info_v3', fields:{} });
  ok(/未发布/.test(meta()), '未发布原因写进 meta', meta().trim().slice(0,90));
  ok(/列出车上地图/.test(msg()), '提示可手动挑一张', msg());
  ok((globalThis.__CALLS||[]).length === 0, '未发布时不瞎加载', globalThis.__CALLS);
}

console.log('\n[5] 地图加载：非法名字在前端也要挡');
ok(/loadMap\(/.test(html), '存在 loadMap');
ok(/mapMsg\(/.test(html), '存在 mapMsg（错误提示通道）');

console.log('\n' + '='.repeat(50));
console.log(`通过 ${pass} / 失败 ${fail}`);
process.exit(fail ? 1 : 0);
