import fs from 'fs';
const html = fs.readFileSync('/data/workspace/aoting_tools/apps/statusweb/index.html','utf8');

/* 只取三个纯函数出来测：它们决定「开机时长 / 开机时刻 / 本次连接」怎么显示 */
const pick = (name) => {
  const i = html.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('找不到 ' + name);
  let d = 0, j = html.indexOf('{', i), k = j;
  for (; k < html.length; k++){
    if (html[k] === '{') d++;
    else if (html[k] === '}'){ d--; if (d === 0){ k++; break; } }
  }
  return html.slice(i, k);
};
const mod = new Function(pick('fmtUp') + pick('fmtBoot') + pick('fmtConn')
  + '\n; return {fmtUp, fmtBoot, fmtConn};')();
const {fmtUp, fmtBoot, fmtConn} = mod;

let pass=0, fail=0;
const ok=(c,m)=>{ if(c){pass++; console.log('ok   '+m);} else {fail++; console.log('FAIL '+m);} };

/* ---- 用户截图里的那个数：6天5小时6分钟 ---- */
const up6 = 6*86400 + 5*3600 + 6*60;      // 536760
ok(fmtUp(up6) === '6天 5小时 6分钟', 'fmtUp(536760) = 6天 5小时 6分钟  实际: '+fmtUp(up6));
ok(fmtUp(0) === '0分钟', 'fmtUp(0) 不显示负数/NaN: '+fmtUp(0));
ok(fmtUp(null) === '0分钟', 'fmtUp(null) 兜底: '+fmtUp(null));
ok(fmtUp(59) === '0分钟', 'fmtUp(59) = 0分钟');
ok(fmtUp(3600) === '1小时 0分钟', 'fmtUp(3600) = 1小时 0分钟  实际: '+fmtUp(3600));
ok(fmtUp(86400) === '1天 0分钟', 'fmtUp(86400) 省略 0 小时: '+fmtUp(86400));
ok(!/NaN/.test(fmtUp(undefined)), 'fmtUp(undefined) 不含 NaN');

/* ---- 开机时刻：格式必须是 M-D HH:MM，且能反推回同一个时刻 ---- */
const ts = new Date(2026, 9, 3, 7, 12, 0).getTime() / 1000;   // 2026-10-03 07:12
const bs = fmtBoot(ts);
ok(bs === '10-03 07:12', 'fmtBoot -> 10-03 07:12  实际: '+bs);
ok(fmtBoot(0) === '', 'fmtBoot(0) 返回空（旧车端脚本没有该字段时不显示）');
ok(!/NaN/.test(fmtBoot(ts)), 'fmtBoot 不含 NaN');

/* 关键点：uptime 是"时长"，开机时刻才是能自查的锚点。
   两者必须自洽 —— 开机时刻 + 运行时长 = 现在。 */
const now = 1791522000;
const bootTs = now - up6;
const d = new Date(bootTs * 1000);
ok(fmtBoot(bootTs) === (d.getMonth()+1) + '-' + String(d.getDate()).padStart(2,'0')
   + ' ' + String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0'),
   '开机时刻与运行时长自洽');

/* ---- 本次连接时长 mm:ss / h:mm:ss ---- */
ok(fmtConn(5000) === '0:05', 'fmtConn(5s) = 0:05  实际: '+fmtConn(5000));
ok(fmtConn(65000) === '1:05', 'fmtConn(65s) = 1:05  实际: '+fmtConn(65000));
ok(fmtConn(3700000) === '1:01:40', 'fmtConn(1h1m40s) = 1:01:40  实际: '+fmtConn(3700000));
ok(fmtConn(0) === '0:00', 'fmtConn(0) = 0:00  实际: '+fmtConn(0));
ok(!/NaN/.test(fmtConn(-1)), 'fmtConn 负数不产生 NaN');

console.log('\n通过 '+pass+'  失败 '+fail);
process.exit(fail ? 1 : 0);
