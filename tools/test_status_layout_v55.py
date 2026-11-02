#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.55 布局重排 + 深度点云距离过滤 的验收。

只测"这次改了什么"，不重复既有断言：
  1. 连接配置在顶部通栏（grp 之前），不再是独立左栏
  2. vers / hw / faults 已移入「车辆状态信息」组
  3. 故障块独占整行 + 限高滚动
  4. 倒计时告警提前到 30 秒
  5. 深度点云有「过滤 > X 米」输入，且真的按距离滤点
  6. pc3d 卡片在「调试工具」组内
"""
import io, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "apps", "statusweb", "index.html")
S = io.open(HTML, encoding="utf-8").read()

ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS " + name)
    else:
        fail += 1
        print("  FAIL " + name + ("  <- " + extra if extra else ""))


def between(a, b, src=None):
    """取 a 到 b 之间的片段；找不到返回 None。"""
    s = src if src is not None else S
    i = s.find(a)
    if i < 0:
        return None
    j = s.find(b, i)
    if j < 0:
        return None
    return s[i:j]


print("[1] 连接配置在顶部通栏")
i_conn = S.find('class="card connbar"')
i_grp1 = S.find("① 系统资源")
chk("connbar 存在", i_conn > 0)
chk("connbar 在分组之前（顶部）", 0 < i_conn < i_grp1,
    "conn=%d grp=%d" % (i_conn, i_grp1))
chk("main 已改为单栏", "grid-template-columns:300px 1fr" not in S)
chk("主机/端口/用户/按钮都在 connbar 内",
    all(k in between('class="card connbar"', "① 系统资源") or False
        for k in ('id="host"', 'id="port"', 'id="bconn"', 'id="hzbox"')))

print("[2] 版本/硬件/故障 移入 车辆状态信息")
grp2 = between("② 车辆状态信息", "③ 调试工具")
chk("② 分组存在且改名", grp2 is not None)
if grp2:
    for k in ('id="vers"', 'id="hw"', 'id="faults"', 'id="tasks"',
              'id="nav"', 'id="axslam"', 'id="map"'):
        chk("② 含 " + k, k in grp2)
grp1 = between("① 系统资源", "② 车辆状态信息")
chk("① 只剩资源项", grp1 is not None and 'id="vers"' not in grp1
    and 'id="faults"' not in grp1 and 'id="hw"' not in grp1)
chk("① 仍含 sum/tables/disk",
    grp1 is not None and all(k in grp1
                             for k in ('id="sum"', 'id="tables"', 'id="disk"')))

print("[3] 故障块独占整行 + 限高")
chk("faults 带 full 类", 'id="faults" class="full"' in S)
chk(".full 跨整行", ".full{grid-column:1 / -1}" in S)
chk("flist 限高滚动", ".flist{max-height:340px;overflow-y:auto}" in S)

print("[4] 倒计时 30 秒告警")
chk("ALERT_SEC = 30", "const ALERT_SEC = 30" in S)
chk("不再是 10 秒", "const ALERT_SEC = 10" not in S)

print("[5] 深度点云距离过滤")
chk("有 pc3dmaxd 输入框", 'id="pc3dmaxd"' in S)
chk("按距离滤点", "Math.sqrt(x * x + y * y + z * z) > mx" in S)
chk("阈值变化即重绘", "mde.oninput" in S)
grp3 = between("③ 调试工具", "</main>")
# v1.0.58 起深度点云按用户要求移到 ② 车辆位置及实时点云 下面，
# 不再属于 ③ 调试工具。这里跟着改，否则每次升级都会误报。
chk("pc3dbox 在 ② 内", grp2 is not None and 'id="pc3dbox"' in grp2)
chk("pc3dbox 不在 ③ 调试工具内", grp3 is not None and 'id="pc3dbox"' not in (grp3 or ""))

print("[6] 距离过滤的数值行为（抽出 JS 片段用 node 实跑）")
seg = between("const mxEl = $('pc3dmaxd');", "const n = Math.floor(flat.length / 3);")
if not seg:
    chk("能定位过滤片段", False)
else:
    js = ("function run(flatIn, mx){\n  let flat = flatIn.slice();\n  let cut = 0;\n"
          + seg.replace("const mxEl = $('pc3dmaxd');", "")
               .replace("const mx = mxEl ? (parseFloat(mxEl.value) || 0) : 0;", "")
               .replace("let cut = 0;", "")
               .replace("PC3.cut = cut;", "")
               .replace("PC3.total = Math.floor((PC3.pts || []).length / 3);", "")
          + "\n  return JSON.stringify({keep: flat, cut: cut});\n}\n"
          "const p=[0.5,0,0, 0,1.5,0, 0,0,3.0];\n"
          "console.log(run(p,0));console.log(run(p,2.0));console.log(run(p,0.4));\n")
    io.open("/tmp/_v55f.js", "w", encoding="utf-8").write(js)
    import subprocess
    r = subprocess.run(["node", "/tmp/_v55f.js"], capture_output=True, text=True)
    if r.returncode != 0:
        chk("过滤片段可执行", False, r.stderr[:200])
    else:
        import json
        a, b, c = [json.loads(x) for x in r.stdout.strip().split("\n")]
        chk("阈值 0 不过滤", a["cut"] == 0 and len(a["keep"]) == 9, str(a))
        chk("阈值 2m 滤掉 3.0 那个点", b["cut"] == 1 and len(b["keep"]) == 6, str(b))
        chk("滤掉的正是最远的点", b["keep"] == [0.5, 0, 0, 0, 1.5, 0], str(b["keep"]))
        chk("阈值过小则全滤掉", c["cut"] == 3 and len(c["keep"]) == 0, str(c))

print("[7] 结构完整性")
# 只数静态区：JS 里拼 HTML 的字符串也含 <div，一起数必然"不平衡"，
# 那是统计口径错，不是页面结构错（上一版测的就是静态区 44/44）。
i_script = S.find("<script>")
STATIC = S[:i_script] if i_script > 0 else S
chk("静态区 div 配对",
    STATIC.count("<div") == STATIC.count("</div>"),
    "%d vs %d" % (STATIC.count("<div"), STATIC.count("</div>")))
m = re.search(r"<script>(.*)</script>", S, re.S)
chk("JS 可抽出", m is not None)
if m:
    io.open("/tmp/_v55.js", "w", encoding="utf-8").write(m.group(1))
    import subprocess
    r = subprocess.run(["node", "--check", "/tmp/_v55.js"],
                       capture_output=True, text=True)
    chk("JS 语法通过", r.returncode == 0, r.stderr[:200])

print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)
