"""从真实工作台生成可直接打开、无业务请求的断开状态预览。"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'apps/statusweb/index.html').read_text(encoding='utf-8')
script = re.search(r'<script>([\s\S]*?)</script>', source).group(1)
# 只保留导航和概览展示代码，不携带 API、存储读写、认证或远端操作。
workspace = script[script.index('const WORKSPACES ='):script.index('// 超时全屏告警。')]
workspace = workspace.replace(
    "    if (activeSpatial === 'map' && $('mapcv') && typeof fitMapCanvas === 'function') fitMapCanvas();\n", '')
workspace = workspace.replace(
    "    if (activeSpatial === 'pc3dbox' && $('pc3dcv') && typeof drawPC3D === 'function') drawPC3D(null);\n", '')
html = re.sub(r'<script>[\s\S]*?</script>', '', source)
html = html.replace('<title>远程机器状态监控</title>', '<title>远程机器工作台 · 离线 UI 预览</title>')
html = html.replace('<meta charset="utf-8">', '''<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'">''')
html = re.sub(r'(<input id="(?:host|port|user|pwd)"[^>]*?)\svalue="[^"]*"', r'\1', html)
html = html.replace('frp.aotbot.com', '').replace('autocomplete="current-password"', 'autocomplete="off"')
html = html.replace('<body>', '''<body>
<div class="preview-banner">仅预览 UI · 当前为断开状态 · 所有连接及对外请求已禁用 · 不读取已保存机器或浏览器凭据</div>''')
preview_script = '''
const $ = id => document.getElementById(id);
const S = {connected:false};
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
''' + workspace + '''
initWorkspace();
$('sum').innerHTML = '<div class="card"><div class="empty">未连接机器，资源指标尚未加载。此文件仅预览 UI。</div></div>';
$('veh').innerHTML = '<div class="workspace-empty">未连接机器，任务、定位与硬件数据尚未加载。</div>';
$('log').textContent = '仅预览 UI：无真实会话，无采集日志。';
$('profnote').textContent = '预览不读取已保存机器';
$('pwdnote').textContent = '预览不读取、不保存任何主机、账号或密码。';
$('msg').textContent = '离线预览，可切换左侧工作区及标签页。';
$('rgbbox').innerHTML = '<div class="workspace-empty">彩色相机 · 未连接，未加载画面。实际使用保留账号门禁与水印。</div>';
$('pc3dbox').innerHTML = '<div class="workspace-empty">深度点云 · 未订阅，未加载任何空间数据。</div>';
for (const input of document.querySelectorAll('input,select,textarea')) {
  input.disabled = true;
  if (['host','port','user','pwd'].includes(input.id)) {
    input.value = ''; input.removeAttribute('value'); input.placeholder = '离线预览';
  }
}
for (const button of document.querySelectorAll('button')) {
  if (!button.matches('[data-workspace],[data-spatial],[data-debug],#connection-toggle')) {
    button.disabled = true;
    button.title = '仅预览 UI，操作已禁用';
  }
}
'''
html = html.replace('</body>', '<script>\n' + preview_script + '\n</script>\n</body>')
assert not re.search(r'/api/|localStorage|sessionStorage|fetch\(|XMLHttpRequest|WebSocket\(', html)
assert 'frp.aotbot.com' not in html
output = ROOT / 'outputs/statusweb-ui-preview.html'
output.parent.mkdir(exist_ok=True)
output.write_text(html, encoding='utf-8')
print(f'已生成离线预览：{output}')
