# -*- coding: utf-8 -*-
"""v1.0.51：深度点云多路相机 01~05 + /api/agentlog 修复的回归测试。

为什么要有这个文件：
  1) do_GET 里 s4 = SESSION 是从未定义过的变量，handler 一抛 NameError
     连接就被掐断，前端只看到 Failed to fetch。这个错在静态阅读时看不出来，
     必须用 AST 把所有 handler 分支扫一遍。
  2) 多路相机最容易错的是"只订阅一次、换号不重订"，于是看到的一直是
     上一路相机的点 —— 所以要断言车端会检测 cam 变化并退订重订。
"""
import ast
import builtins
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

OK = []
FAIL = []


def chk(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + "  " + name + (("  " + str(extra)) if extra else ""))


# ── 1. server.py：所有 handler 分支不得引用未定义名字 ──
srv = io.open(os.path.join(ROOT, "apps", "statusweb", "server.py"),
              encoding="utf-8").read()
tree = ast.parse(srv)
mod = set()
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        mod.add(n.name)
    elif isinstance(n, ast.Assign):
        for tg in n.targets:
            if isinstance(tg, ast.Name):
                mod.add(tg.id)
    elif isinstance(n, (ast.Import, ast.ImportFrom)):
        for a in n.names:
            mod.add((a.asname or a.name).split(".")[0])
bi = set(dir(builtins))
bad = []
for cls in ast.walk(tree):
    if not isinstance(cls, ast.ClassDef):
        continue
    for f in cls.body:
        if not isinstance(f, ast.FunctionDef):
            continue
        loc = set(a.arg for a in f.args.args)
        for s in ast.walk(f):
            if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Store):
                loc.add(s.id)
            if isinstance(s, ast.ExceptHandler) and s.name:
                loc.add(s.name)
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                for a in s.names:
                    loc.add((a.asname or a.name).split(".")[0])
            if isinstance(s, ast.FunctionDef):
                loc.add(s.name)
        for s in ast.walk(f):
            if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Load):
                if s.id not in loc and s.id not in mod and s.id not in bi:
                    bad.append((f.name, s.lineno, s.id))
chk("handler 分支无未定义变量", not bad, bad[:5])
chk("不再给 SESSION 赋值（注释除外）", "= SESSION" not in srv)

# ── 2. /api/agentlog：GET 与 POST 都要有 ──
chk("do_GET 有 /api/agentlog", "'/api/agentlog'" in srv)
g = srv.index("def do_GET")
p = srv.index("def do_POST")
chk("do_POST 也有 /api/agentlog", "'/api/agentlog'" in srv[p:p + 400])
chk("抽出共用的 api_log", "def api_log(self)" in srv)

# ── 3. api_pc3d 带 cam，且写相机号标志文件 ──
i = srv.index("def api_pc3d")
body = srv[i:i + 2200]
chk("api_pc3d 读 cam 参数", "get('cam')" in body or 'get("cam")' in body)
chk("cam 越界回落", "not in (1, 2, 3, 4, 5)" in body)
chk("下发相机号标志文件", "/tmp/aoting_pc3d_cam" in body)
chk("有 send_flag_val", "def send_flag_val" in srv)

# ── 4. 车端：多路相机话题 ──
ag = io.open(os.path.join(ROOT, "apps", "statusweb", "stat_agent.py"),
             encoding="utf-8").read()
ns = {"__name__": "notmain"}
head = ag[ag.index("CAM_IDXS ="):ag.index("DEPTH_IMG_TOPIC, DEPTH_INFO_TOPIC")]
exec(compile(head, "h", "exec"), ns)
_cam_topics = ns["_cam_topics"]
chk("相机 01 话题", _cam_topics(1) == ("/camera_01/depth/image_raw",
                                       "/camera_01/depth/camera_info"),
    _cam_topics(1))
chk("相机 02 话题", _cam_topics(2) == ("/camera_02/depth/image_raw",
                                       "/camera_02/depth/camera_info"),
    _cam_topics(2))
chk("相机 05 话题", _cam_topics(5)[0] == "/camera_05/depth/image_raw")
chk("越界回落 01", _cam_topics(99)[0] == "/camera_01/depth/image_raw")
chk("非法值回落 01", _cam_topics("x")[0] == "/camera_01/depth/image_raw")
chk("单引号禁令", ag.count("'") == 0, ag.count("'"))

# ── 5. 换相机必须退订重订 ──
w = ag[ag.index("def _pc3d_watch"):]
w = w[:w.index("\ndef ")] if "\ndef " in w[10:] else w
chk("watch 里比较当前相机号", "PC3D_CAM_NOW[0] != cam" in w)
chk("换号时先 unregister", "unregister()" in w)
chk("订阅用动态话题", "rospy.Subscriber(img_t" in w)
chk("订阅日志带相机号", 'subscribed cam %02d' in w)
chk("切换日志", 'switch to cam %02d' in w)

# ── 6. 相机号也能走 stdin ──
chk("stdin 解析 cam", 'if "cam" in c:' in ag)
chk("cam 越界回落(车端)", "PC3D_CAM[0] not in CAM_IDXS" in ag)

# ── 7. 前端 ──
html = io.open(os.path.join(ROOT, "apps", "statusweb", "index.html"),
               encoding="utf-8").read()
chk("前端有相机下拉", 'id="pc3dcam"' in html)
chk("下拉 5 项", html.count('<option value="') >= 5)
chk("勾选时带 cam", "JSON.stringify({on: on, cam: pc3dCam()})" in html)
chk("换相机单独下发", "pc3dCam()})});" in html or "cam: pc3dCam()" in html)
chk("话题文本随相机号", "function pc3dTopicText" in html)
chk("文案已改 10Hz", "深度图 10Hz" in html and "深度图 30Hz" not in html)

print("\n通过 %d，失败 %d" % (len(OK), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
