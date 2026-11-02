import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
const __d = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__d, '..', '..');
const src = fs.readFileSync(path.join(ROOT, 'aoting_tools', 'apps', 'statusweb', 'index.html'), 'utf8');
const js = src.match(/<script>([\s\S]*)<\/script>/)[1];
const grab = (name) => {
  const i = js.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('no ' + name);
  let d = 0, j = js.indexOf('{', i);
  for (let k = j; k < js.length; k++){
    if (js[k] === '{') d++;
    else if (js[k] === '}'){ d--; if (!d) return js.slice(i, k + 1); }
  }
};
// 假 DOM
const els = {};
global.$ = (id) => els[id] || null;
global.PC3 = {extInit:false};
const e0 = [0.34,0.03,0.13,1.93,-31.96,-2.88];
e0.forEach((v,i)=>{ els['ex'+i] = {value:String(v)}; });
els['pc3daxis'] = {value:'opt'};
els['pc3dinv'] = {checked:false};
// ES 模块里是严格模式，eval 里的函数声明不会泄漏到模块作用域，
// 直接 eval 会报 rpyMatJS is not defined。用 new Function 把函数显式返回出来。
const { extVals, rpyMatJS, applyExtJS } = new Function(
  grab('extVals') + '\n' + grab('rpyMatJS') + '\n' + grab('applyExtJS') +
  '\nreturn {extVals, rpyMatJS, applyExtJS};')();

let ok = true;
const chk=(n,c,x)=>{ console.log((c?'PASS ':'FAIL ')+n+(x?'  '+x:'')); if(!c) ok=false; };
const f3=(a)=>a.map(v=>v.toFixed(3)).join(',');

// 单位阵
let R = rpyMatJS(0,0,0);
chk('rpy全0 = 单位阵', R.every((v,i)=>Math.abs(v-[1,0,0,0,1,0,0,0,1][i])<1e-9));
// 与 python 一致性：yaw 90
R = rpyMatJS(0,0,90);
chk('yaw=90 与ROS一致', Math.abs(R[0]-0)<1e-9 && Math.abs(R[3]-1)<1e-9, 'col0='+f3([R[0],R[3],R[6]]));

// 相机正前方 1m 点，四种组合
const pt = [0,0,1];
function run(axis, inv){
  els['pc3daxis'].value = axis; els['pc3dinv'].checked = inv;
  const o = applyExtJS(pt);
  return [o[0],o[1],o[2]];
}
const a = run('opt', false), b = run('opt', true),
      c = run('body', false), d = run('body', true);
console.log('  光學系 直接 :', f3(a));
console.log('  光學系 取逆 :', f3(b));
console.log('  对齐系 直接 :', f3(c));
console.log('  对齐系 取逆 :', f3(d));
// 平移 0.13 应体现在 z 上（未取逆时）
chk('直接变换含平移', Math.abs(a[2] - (0.8479+0.13)) < 0.01, 'z='+a[2].toFixed(3));
// 四种结果必须互不相同，否则界面上切换看不出区别 = 白做
const keys = [a,b,c,d].map(v=>f3(v));
chk('四种组合互不相同', new Set(keys).size === 4, keys.join(' | '));
// 对齐系下：光学 Z(前) -> 对齐 X(前)，故未旋转时结果应贴近 X 轴
els['pc3daxis'].value='body'; els['pc3dinv'].checked=false;
els['ex3']={value:'0'}; els['ex4']={value:'0'}; els['ex5']={value:'0'};
els['ex0']={value:'0'}; els['ex1']={value:'0'}; els['ex2']={value:'0'};
const z0 = applyExtJS([0,0,1]);
chk('零外参+对齐系: 前方->X轴', Math.abs(z0[0]-1)<1e-9 && Math.abs(z0[1])<1e-9 && Math.abs(z0[2])<1e-9, f3(z0));
console.log(ok ? 'ALL OK' : 'HAS FAILURE');
process.exit(ok?0:1);
