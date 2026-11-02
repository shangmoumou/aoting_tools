import fs from 'fs';
import path from 'path';
import {fileURLToPath} from 'url';
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const html = fs.readFileSync(path.join(ROOT,'apps','camweb','index.html'),'utf8');
// 按【标记】定位，不要硬编码整行文本：
// 之前写死 "let WM_USER = '';"，改了那一行就 slice 出 -1，
// 测试报 "drawWatermark is not defined"，看着像功能坏了，其实是测试碎了。
const START = html.indexOf('let WM_USER');
const END   = html.indexOf('/* 拉流的仍然是');
if (START < 0 || END < 0 || END <= START)
  throw new Error('index.html 结构变了：找不到水印代码段的起止标记');
const src = html.slice(START, END);
const mod = new Function(src + '\n; return {drawWatermark, wmUser, wmTime, setWM:(v)=>{WM_USER=v}};')();
const {drawWatermark, wmUser, wmTime, setWM} = mod;

function fakeCtx(){
  const rec = {texts:[], fills:[], rects:0, styles:[], transforms:0};
  let fs_ = '';
  const ctx = {
    save(){}, restore(){}, clearRect(){}, translate(){}, rotate(){}, setTransform(){rec.transforms++;},
    fillText(t,x,y){ rec.texts.push({t:String(t), x, y, fill:fs_}); },
    fillRect(){ rec.rects++; },
    measureText(t){ return {width: String(t).length*7}; },
    drawImage(){}, 
    set fillStyle(v){ fs_=v; rec.styles.push(v); }, get fillStyle(){return fs_;},
    font:'', globalAlpha:1, textAlign:'', textBaseline:''
  };
  return {ctx, rec};
}
let pass=0, fail=0;
const ok=(c,m)=>{ if(c){pass++; console.log('ok  '+m);} else {fail++; console.log('FAIL '+m);} };

setWM('zhangsan');
ok(wmUser()==='zhangsan', '账号取到 zhangsan');

// 1) 640x480 平铺 + 角标
let {ctx,rec}=fakeCtx();
drawWatermark(ctx, 640, 480, {user:'zhangsan', target:'frp.aotbot.com:21009', time:'2026-09-11 15:04:05'});
const tiles = rec.texts.filter(t=>t.fill==='#ffffff');
ok(tiles.length>=20, '满屏斜纹 fillText 数量 '+tiles.length+' >= 20');
ok(tiles.every(t=>t.t.includes('zhangsan')), '每一条斜纹都含账号');
const last = rec.texts[rec.texts.length-1];
ok(last.fill==='#7ee787', '角标为品牌绿 #7ee787');
ok(last.t.includes('zhangsan')&&last.t.includes('frp.aotbot.com:21009')&&last.t.includes('2026-09-11 15:04:05'),
   '角标含账号+目标+时间: '+last.t);
ok(rec.rects>=1, '角标有底色块（压扁截图也看得清）');

// 2) 旋转后的画布尺寸（90° 时宽高互换）依然画得下
({ctx,rec}=fakeCtx());
drawWatermark(ctx, 480, 640, {user:'lisi', target:'h:1', time:'t'});
ok(rec.texts.length>5, '旋转后画布(480x640)仍正常绘制');

// 3) 极小画布：跳过平铺，但角标不能丢
({ctx,rec}=fakeCtx());
drawWatermark(ctx, 30, 20, {user:'lisi', target:'h:1', time:'t'});
ok(rec.texts.length===1 && rec.texts[0].t.includes('lisi'), '极小画布跳过平铺但保留角标');

// 4) 未登录兜底
({ctx,rec}=fakeCtx());
drawWatermark(ctx, 640, 480, {});
ok(rec.texts[rec.texts.length-1].t.startsWith('未登录'), '未登录时显示「未登录」');

// 5) 时间格式
ok(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(wmTime()), '时间格式 YYYY-MM-DD HH:MM:SS -> '+wmTime());

// 6) 页面接线：canvas + 隐藏源图
ok(/const cv = document\.createElement\('canvas'\)/.test(html), 'attach() 创建 canvas');
ok(/img\.className = 'src'/.test(html), '源 img 标记为 .src（隐藏）');
ok(/\.view img\.src\{/.test(html), 'CSS 有 .src 隐藏规则');
ok(/\.view img,\.view canvas\{/.test(html), 'CSS 让 canvas 自适应容器');
ok(/drawWatermark\(ctx, W, H/.test(html), '每帧绘制时调用水印');
ok(/fallbackPlainImg/.test(html), '有 canvas 失败回退');
ok(/isConnected/.test(html), '画面移除后停止空转');

console.log('\nPASS=%d FAIL=%d', pass, fail);
process.exit(fail?1:0);
