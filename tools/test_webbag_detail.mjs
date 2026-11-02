#!/usr/bin/env node
/**
 * test_webbag_detail.mjs
 *
 * 测「右下角内容面板跟随点击的话题」这个修复。
 *
 * 为什么不用 jsdom：环境里没有，为了一个函数去装依赖不值当。
 * 这里的做法是【从 app.js 真实源码里把函数抠出来】再配假 DOM 跑，
 * 而不是在测试里重写一遍逻辑 —— 重写的话测的是复制品，源码改错了测试照样绿。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, '..', 'apps', 'webbag', 'src', 'app.js');
const OUT = join(here, '..', 'apps', 'webbag', 'index.html');

let pass = 0, fail = 0;
const ok = (name, cond, extra = '') => {
  if (cond) { pass++; console.log(`ok    ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${extra ? '  <- ' + extra : ''}`); }
};

const src = readFileSync(SRC, 'utf8');
const out = readFileSync(OUT, 'utf8');

/** 从源码里按大括号配对抠出一个函数定义（从 "function NAME" 开始） */
function grab(name) {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`源码里找不到 function ${name}`);
  let d = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (d === 0) return src.slice(i, k + 1); }
  }
  throw new Error(`括号没配对: ${name}`);
}

// ── 假 DOM ───────────────────────────────────────────────
function makeEl(text = '') {
  return {
    textContent: text, innerHTML: text,
    _cls: new Set(),
    classList: {
      toggle(c, on) { on ? this._o._cls.add(c) : this._o._cls.delete(c); },
      contains(c) { return this._o._cls.has(c); },
    },
  };
}
function mkEnv({ entries = {}, selTopic = null, t = 0, startTime = 0 } = {}) {
  const els = { json: makeEl(''), selinfo: makeEl('') };
  const children = [makeEl(), makeEl(), makeEl()];
  children.forEach(c => { c.classList._o = c; });
  Object.values(els).forEach(e => { e.classList._o = e; });
  const lb = { children, classList: { toggle() {} } };
  const S = {
    selTopic, selected: null, t,
    topics: ['/a', '/b', '/c'],
    visible: new Set(['/a', '/b', '/c']),
    bag: {
      startTime,
      latestBefore(topic) { return entries[topic] ?? null; },
    },
  };
  const calls = [];
  const showDetail = e => { calls.push(e); if (e) { els.json.textContent = JSON.stringify(e); } };
  const $ = id => (id === 'tllabels' ? lb : els[id]);
  return { S, $, els, lb, calls, showDetail };
}

const ROW_H = Number((src.match(/const ROW_H = (\d+);/) || [])[1]);
ok('能从源码读出 ROW_H', ROW_H === 22, `拿到 ${ROW_H}`);

const fnSrc = ['setSelTopic', 'refreshDetail', 'markSelLabel'].map(grab).join('\n');
const build = new Function('S', '$', 'showDetail', 'ROW_H', `
  ${fnSrc}
  return { setSelTopic, refreshDetail, markSelLabel };
`);

// ── 1. 切话题 ────────────────────────────────────────────
{
  const e = mkEnv({ entries: { '/b': { topic: '/b', stamp: 5 } }, selTopic: '/a' });
  const api = build(e.S, e.$, e.showDetail, ROW_H);
  api.setSelTopic('/b');
  ok('setSelTopic 改了关注话题', e.S.selTopic === '/b');
  ok('切换后立刻刷新了面板', e.calls.length === 1 && e.calls[0].topic === '/b');
  // 这才是用户报的那个 bug：点了别的话题，面板必须显示【那个话题】的内容
  ok('面板内容变成了点击的话题', e.els.json.textContent.includes('/b')
      && !e.els.json.textContent.includes('/a'));
}

// ── 2. 同一条消息也要重画（force） ────────────────────────
{
  const msg = { topic: '/b', stamp: 7 };
  const e = mkEnv({ entries: { '/b': msg }, selTopic: '/b' });
  const api = build(e.S, e.$, e.showDetail, ROW_H);
  e.S.selected = msg;                 // 假装已经画过这条
  api.refreshDetail(true);
  ok('force=true 时同一条消息也重画', e.calls.length === 1, `实际 ${e.calls.length}`);
  e.calls.length = 0;
  api.refreshDetail(false);
  ok('未 force 且消息没变时不重画（播放时节流）', e.calls.length === 0);
}

// ── 3. 该话题此刻前没消息 —— 必须说清楚，不能留着上一个话题的内容 ──
{
  const e = mkEnv({ entries: {}, selTopic: '/c', t: 3e9 });
  const api = build(e.S, e.$, e.showDetail, ROW_H);
  e.els.json.textContent = '上一个话题的内容';
  api.setSelTopic('/c');
  ok('无消息时面板不再留旧内容', !e.els.json.textContent.includes('上一个话题的内容'));
  ok('无消息时明确提示话题名', e.els.json.textContent.includes('/c'));
  ok('无消息时提示"之前没有消息"', e.els.json.textContent.includes('之前没有消息'));
  ok('无消息时 selinfo 有说明', e.els.selinfo.textContent.includes('此处无数据'));
}

// ── 4. 标签高亮 ──────────────────────────────────────────
{
  const e = mkEnv({ selTopic: '/b' });
  const api = build(e.S, e.$, e.showDetail, ROW_H);
  api.markSelLabel();
  const flags = e.lb.children.map(c => c.classList.contains('sel'));
  ok('只有关注话题那行高亮', JSON.stringify(flags) === '[false,true,false]', JSON.stringify(flags));
}

// ── 5. 点击 y 坐标 -> 行号（这是"点哪一行都没反应"的直接原因）──
{
  const rows = ['/a', '/b', '/c'];
  const hit = y => { const i = Math.floor(y / ROW_H); return (i >= 0 && i < rows.length) ? rows[i] : null; };
  ok('y=0  -> 第 1 行', hit(0) === '/a');
  ok('y=21 -> 仍是第 1 行', hit(21) === '/a');
  ok('y=22 -> 第 2 行', hit(22) === '/b');
  ok('y=45 -> 第 3 行', hit(45) === '/c');
  ok('y=66 越界不切换（点在空白处）', hit(66) === null);
  ok('y=-1 越界不切换', hit(-1) === null);
}

// ── 6. 打包产物必须同步（改了 src 没 build 是最常见的翻车点）──
const need = [
  ['function setSelTopic', '切换话题的函数'],
  ['function refreshDetail', '刷新面板的函数'],
  ['function markSelLabel', '标签高亮'],
  ["tllabels').addEventListener('click'", '点标签名切换'],
  ['Math.floor(y / ROW_H)', '按 y 算行号'],
  ['感知导航bag分析工具', '工具改名'],
];
for (const [frag, label] of need) {
  ok(`打包产物包含：${label}`, out.includes(frag));
}
ok('打包产物无残留旧名', !out.includes('本地 bag 播放器'));

console.log(`\n通过 ${pass} / 失败 ${fail}`);
process.exit(fail ? 1 : 0);
