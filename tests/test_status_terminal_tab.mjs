/* 真跑页面终端函数：验证 Tab 转发、按键顺序、就绪状态及错误处理。 */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const html = fs.readFileSync(new URL('../apps/statusweb/index.html', import.meta.url), 'utf8');
// 编译全部页面脚本，外部组件的脚本标签内容为空。
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const boxes = {};
const sent = [];
let pending = null;
let reply = {ok:true};
const context = vm.createContext({
  $: id => boxes[id] ??= {value:'', disabled:true, textContent:'', className:'',
    selectionStart:0, clientWidth:800, clientHeight:300,
    querySelector:() => ({getBoundingClientRect:() => ({width:700,height:360})})},
  getComputedStyle:() => ({paddingLeft:'16',paddingRight:'16',paddingTop:'16',paddingBottom:'16'}),
  fetch: async (url, options) => {
    if (options?.body) {
      sent.push(JSON.parse(options.body));
      if (pending) await pending;
    }
    return {json:async () => reply};
  },
  Terminal: class {
    constructor(options){ this.cols = options.cols; this.rows = options.rows; }
    open(){}
    attachCustomKeyEventHandler(fn){ this.keyHandler = fn; }
    onData(fn){ this.dataHandler = fn; }
    focus(){ this.focused = true; }
    resize(cols, rows){ this.cols = cols; this.rows = rows; }
    reset(){}
    write(txt){ this.output = (this.output || '') + txt; }
  },
  ResizeObserver: class { observe(){} },
  document:{createElement:() => ({getContext:() => ({measureText:() => ({width:7})})})},
});
const start = html.indexOf('var T = {on:false');
const end = html.indexOf('function renderDisk', start);
vm.runInContext(html.slice(start, end), context);
vm.runInContext('tmInitView(); T.on = true; T.ready = true;', context);
assert.equal(context.T.view.keyHandler({key:'Tab', shiftKey:true}), false);
assert.equal(context.T.view.keyHandler({key:'Tab', shiftKey:false}), true);

assert.equal(html.includes('id="tmcmd"'), false);
assert.equal(html.includes('id="tmsend"'), false);

// 快速按键必须等待前一个请求完成，Tab 和回车保持原始顺序。
let release;
pending = new Promise(resolve => { release = resolve; });
context.T.view.dataHandler('l');
context.T.view.dataHandler('\t');
context.T.view.dataHandler('\r');
await new Promise(resolve => setImmediate(resolve));
assert.equal(sent.length, 1);
release();
pending = null;
await context.T.queue;
assert.deepEqual(sent.map(x => x.cmd), ['l', '\t', '\r']);

// Shell 尚未就绪不能丢失输入；准备完成后才允许操作和同步尺寸。
context.T.ready = false;
context.T.view.dataHandler('ignored');
assert.equal(sent.length, 3);
reply = {ok:true, ready:true, running:true, len:4, txt:'\x1b[31mtest\x1b[0m'};
await context.tmPoll();
await context.T.queue;
assert.equal(context.T.ready, true);
assert.equal(context.T.view.output, reply.txt);
assert.equal(sent.at(-1).act, 'resize');

// API 错误停止排队的后续按键，轮询不能静默重新启用发送。
reply = {ok:false, err:'connection lost'};
const count = sent.length;
context.tmSendRaw('a');
context.tmSendRaw('b');
await context.T.queue;
assert.equal(sent.length, count + 1);
assert.equal(context.T.ready, false);
assert.equal(context.T.failed, true);
reply = {ready:true, running:true, len:4};
await context.tmPoll();
assert.equal(context.T.ready, false);
console.log('Terminal Tab, cursor, ordered input, readiness, rendering and failure checks passed');
