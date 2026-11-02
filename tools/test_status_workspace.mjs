/* 使用本机无头浏览器验证工作台；所有 HTTP 请求均在浏览器内拦截。 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import {spawn} from 'node:child_process';
import {pathToFileURL, fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = path.join(root, 'apps/statusweb/index.html');
const preview = path.join(root, 'outputs/statusweb-ui-preview.html');
for (const file of [source, preview]) {
  const html = fs.readFileSync(file, 'utf8');
  for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
}
console.log('PASS 正式页面与预览的 JS 语法');
const previewHTML = fs.readFileSync(preview, 'utf8');
assert(!/\/api\/|localStorage|sessionStorage|fetch\(|XMLHttpRequest|WebSocket\(/.test(previewHTML));
assert(previewHTML.includes("connect-src 'none'"));
assert(!/frp\.aotbot\.com|value="aoting"/.test(previewHTML));
console.log('PASS 预览无 API、凭据存储代码或默认主机，并设置网络隔离 CSP');

const browser = process.env.STATUSWEB_TEST_BROWSER || [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
].find(p => fs.existsSync(p));
assert(browser, '未找到本机浏览器，可用 STATUSWEB_TEST_BROWSER 指定');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'statusweb-ui-test-'));
const child = spawn(browser, ['--headless=new', '--no-first-run', '--no-default-browser-check',
  '--disable-background-networking', '--disable-component-update', '--disable-sync',
  '--disable-extensions', '--disable-default-apps', '--metrics-recording-only',
  '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
  '--user-data-dir=' + profile, 'about:blank'], {stdio:['ignore','ignore','pipe']});
let socket;
try {
  const endpoint = await new Promise((resolve, reject) => {
    let text = '';
    const timer = setTimeout(() => reject(new Error('无头浏览器启动超时')), 20000);
    child.stderr.on('data', chunk => {
      text += chunk;
      const m = text.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (m) { clearTimeout(timer); resolve(m[1]); }
    });
    child.on('error', reject);
    child.on('exit', code => { clearTimeout(timer); reject(new Error('浏览器提前退出：' + code)); });
  });
  socket = new WebSocket(endpoint);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let seq = 0;
  const pending = new Map(), handlers = new Set(), errors = [], intercepted = [];
  function send(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++seq;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(method + ' 超时')); }, 15000);
      pending.set(id, {resolve, reject, timer});
      socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
    });
  }
  socket.onmessage = ({data}) => {
    const msg = JSON.parse(data);
    if (msg.id) {
      const task = pending.get(msg.id);
      if (task) {clearTimeout(task.timer); pending.delete(msg.id); msg.error ? task.reject(new Error(msg.error.message)) : task.resolve(msg.result);}
    } else {
      for (const handler of handlers) handler(msg);
    }
  };
  const {targetId} = await send('Target.createTarget', {url:'about:blank'});
  const {sessionId} = await send('Target.attachToTarget', {targetId, flatten:true});
  const cdp = (method, params) => send(method, params, sessionId);
  await cdp('Page.enable');
  await cdp('Runtime.enable');
  await cdp('Fetch.enable', {patterns:[{urlPattern:'*', requestStage:'Request'}]});
  handlers.add(msg => {
    if (msg.sessionId !== sessionId) return;
    if (msg.method === 'Runtime.exceptionThrown') errors.push(msg.params.exceptionDetails.text + ' ' + (msg.params.exceptionDetails.exception?.description || ''));
    if (msg.method === 'Fetch.requestPaused') {
      const {requestId, request} = msg.params;
      if (request.url.startsWith('file:') && !request.url.includes('/api/')) {
        cdp('Fetch.continueRequest', {requestId}).catch(e => errors.push(e.message));
      } else {
        intercepted.push(request.url);
        const pathname = new URL(request.url).pathname.replace(/^.*(?=\/api\/)/, '');
        const body = pathname === '/api/stat' ? {connected:false} : pathname === '/api/profiles' ? {items:[]} : pathname === '/api/logs' ? {lines:[], seq:0} : {};
        cdp('Fetch.fulfillRequest', {requestId, responseCode:200,
          responseHeaders:[{name:'Content-Type', value:'application/json'}, {name:'Access-Control-Allow-Origin', value:'*'}],
          body:Buffer.from(JSON.stringify(body)).toString('base64')}).catch(e => errors.push(e.message));
      }
    }
  });
  async function evaluate(expression) {
    const result = await cdp('Runtime.evaluate', {expression, returnByValue:true, awaitPromise:true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  }
  async function navigate(file) {
    const loaded = new Promise(resolve => {
      const handler = msg => {if (msg.method === 'Page.loadEventFired' && msg.sessionId === sessionId) {handlers.delete(handler); resolve();}};
      handlers.add(handler);
    });
    await cdp('Page.navigate', {url:pathToFileURL(file).href});
    await loaded;
    await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  }
  await cdp('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await navigate(source);
  assert.deepEqual(errors, []);
  assert.equal(await evaluate("document.querySelectorAll('.sidebar button').length"), 5);
  assert.equal(await evaluate("document.getElementById('workspace-overview').hidden"), false);
  // 测试数据只存在于受拦截的浏览器内，不写入预览，也不触发远端连接。
  await evaluate(`setConnView(true); render({host:'UI 测试机器',cpu:42,mem_used:1024,mem_total:4096,nproc:72,up:3600,t:1,
    task:{ok:true,name:'测试任务',statusText:'执行中'},axslam:{ok:true,fields:{reliable:true}},
    faults:{ok:true,items:[],level:0}}); selectWorkspace('resources');`);
  const widths = await evaluate(`({sum:$('sum').clientWidth, table:$('tables').clientWidth,main:document.querySelector('main').clientWidth})`);
  assert(widths.sum > 1000 && widths.table > 1000, JSON.stringify(widths));
  assert(await evaluate("$('overview-metrics').textContent.includes('测试任务') && $('overview-metrics').textContent.includes('定位可靠')"));
  console.log('PASS 概览真实字段汇总与资源全宽布局');
  await evaluate(`selectWorkspace('spatial'); selectWorkspaceTab('spatial','map');
    Object.defineProperty($('mapimg'),'clientWidth',{configurable:true,value:640});
    Object.defineProperty($('mapimg'),'clientHeight',{configurable:true,value:400});
    S.lastD2l={ok:true,pts:[]}; fitMapCanvas();`);
  assert(await evaluate("$('mapcv').width===640 && $('mapcv').height===400 && S.lastD2l.ok"));
  console.log('PASS 地图画布重定尺寸保留 depth2laser 缓存');
  await evaluate(`window.resizeHits={map:0,pc:0}; fitMapCanvas=()=>resizeHits.map++; drawPC3D=()=>resizeHits.pc++;`);
  await evaluate("selectWorkspaceTab('spatial','pc3dbox'); new Promise(resolve=>requestAnimationFrame(resolve))");
  assert((await evaluate('resizeHits.pc')) > 0);
  await evaluate("selectWorkspaceTab('spatial','map'); new Promise(resolve=>requestAnimationFrame(resolve))");
  assert((await evaluate('resizeHits.map')) > 0);
  console.log('PASS 空间标签切换后触发地图与点云重绘');
  await evaluate("selectWorkspace('debug'); selectWorkspaceTab('debug','cardtm')");
  assert(await evaluate("!$('cardtm').hidden && $('cardgl').hidden && $('tmout').clientHeight >= 320"));
  // 使用本地终端组件与模拟输出检查布局，不连接车机或发送实际命令。
  await evaluate(`tmInitView(); T.view.write('aoting@robot:~$ cd /home/aoting\\r\\n'
    + 'aoting@robot:~$ ls\\r\\n\\x1b[34muser_config/  logs/  workspace/\\x1b[0m\\r\\n'
    + 'aoting@robot:~$ '); tmStatus('已连接 · Tab 补全可用','ok');
    T.ready=true; tmFit(); T.ready=false;`);
  await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  assert(await evaluate(`T.view.rows >= 5 && getComputedStyle($('tmout')).backgroundColor === 'rgb(17, 27, 43)'`));
  // 按真实渲染边界验证末行不会超出容器内容区，不能仅检查配置的行数。
  assert(await evaluate(`(() => {const box=$('tmout'), style=getComputedStyle(box);
    return box.querySelector('.xterm-screen').getBoundingClientRect().bottom
      <= box.getBoundingClientRect().bottom - parseFloat(style.paddingBottom);})()`));
  const terminalShot = await cdp('Page.captureScreenshot', {format:'png'});
  fs.writeFileSync(path.join(root,'outputs/status-terminal-desktop.png'), Buffer.from(terminalShot.data,'base64'));
  await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:false});
  await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  assert(await evaluate('document.documentElement.scrollWidth <= innerWidth'));
  const mobileShot = await cdp('Page.captureScreenshot', {format:'png',captureBeyondViewport:true});
  fs.writeFileSync(path.join(root,'outputs/status-terminal-mobile.png'), Buffer.from(mobileShot.data,'base64'));
  await cdp('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  console.log('PASS 真实终端渲染、自适应行数与窄屏布局，已保存截图');
  // 数据库页使用本地模拟结果，验证选择快照、切表及内容转义，不连接车机。
  await evaluate(`selectWorkspaceTab('debug','carddb'); window.originalDBFetch=window.fetch;
    window.fetch=async (url,options)=>{
      if(url!=='/api/database')return originalDBFetch(url,options);
      const body=JSON.parse(options.body);
      return {ok:true,json:async()=>body.act==='capture'
        ? {ok:true,token:'ui-test',tables:['events'],name:body.name,size:4096,at:'2026-10-29 12:00:00',machine:'测试机器'}
        : window.dbWideFixture || {ok:true,columns:['id','message'],schema:[{name:'id',type:'INTEGER'},{name:'message',type:'TEXT'}],
           page:0,more:false,rows:[[1,'<img src=x onerror=alert(1)>']]}};
    }; $('dbsources').querySelector('input').checked=true; $('dbcapture').click();`);
  await evaluate(`new Promise(resolve=>setTimeout(resolve,100))`);
  assert(await evaluate(`$('dbsnapshot').value==='ui-test' && $('dbtable').options.length===2`));
  await evaluate(`$('dbtable').value='events';$('dbtable').dispatchEvent(new Event('change'));`);
  await evaluate(`new Promise(resolve=>setTimeout(resolve,100))`);
  assert(await evaluate(`$('dbdata').textContent.includes('<img') && !$('dbdata').querySelector('img') && !$('dbcsv').disabled`));
  // 复现多字段 + 长 JSON：列宽不被压缩，长内容默认折叠，页面不被撑宽。
  await evaluate(`window.dbWideFixture={ok:true,columns:Array.from({length:20},(_,i)=>'field_'+i),
    schema:[],page:0,more:false,rows:[Array.from({length:20},(_,i)=>i===6?JSON.stringify({detail:'abc'.repeat(400)}):'value')]};
    $('dbapply').click();`);
  await evaluate(`new Promise(resolve=>setTimeout(resolve,100))`);
  assert(await evaluate(`$('dbdata').scrollWidth>$('dbdata').clientWidth
    && $('dbdata').querySelector('details') && !$('dbdata').querySelector('details').open
    && $('dbdata').querySelector('td').getBoundingClientRect().width>=159
    && document.documentElement.scrollWidth<=innerWidth`));
  const databaseShot=await cdp('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(root,'outputs/status-database-desktop.png'),Buffer.from(databaseShot.data,'base64'));
  await evaluate(`window.fetch=window.originalDBFetch;`);
  console.log('PASS 数据库页快照选择、分页结果与数据库文本转义');
  await evaluate("setConnView(false); render(null)");
  assert(await evaluate("$('cardtm').hidden && !$('debug-offline').hidden"));
  await evaluate("selectWorkspaceTab('debug','cardlog')");
  assert(await evaluate("!$('cardlog').hidden && $('debug-offline').hidden"));
  const duplicateIds = await evaluate(`Array.from(document.querySelectorAll('[id]')).map(e=>e.id).filter((id,i,ids)=>ids.indexOf(id)!==i)`);
  assert.deepEqual(duplicateIds, []);
  assert.deepEqual(errors, []);
  console.log('PASS 完整终端工作区、离线工具可见性与动态 DOM ID 唯一性');
  await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:false});
  await evaluate("selectWorkspace('overview')");
  assert(await evaluate('document.documentElement.scrollWidth <= innerWidth'));
  console.log('PASS 390px 窄屏概览无水平溢出');
  const before = intercepted.length;
  await navigate(preview);
  assert.deepEqual(errors, []);
  await evaluate("selectWorkspace('spatial'); selectWorkspaceTab('spatial','rgbbox')");
  assert(await evaluate("!$('rgbbox').hidden && $('map').hidden"));
  await evaluate("selectWorkspace('debug'); selectWorkspaceTab('debug','cardlog')");
  assert(await evaluate("!$('cardlog').hidden"));
  assert(await evaluate("['host','port','user','pwd'].every(id=>!$(id).value) && $('bconn').disabled"));
  assert.equal(intercepted.length, before);
  assert.deepEqual(errors, []);
  console.log('PASS 独立预览可打开、可导航、无保存主机信息且无网络请求');
  await send('Browser.close');
} finally {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({id:999999,method:'Browser.close'}));
    socket.close();
  }
  if (child.exitCode === null) await new Promise(resolve => {
    const timer = setTimeout(() => { child.kill(); resolve(); }, 5000);
    child.once('exit', () => {clearTimeout(timer); resolve();});
  });
  fs.rmSync(profile, {recursive:true, force:true, maxRetries:5, retryDelay:200});
}
console.log('ALL PASS');
