import ast, math, sys
src = open("aoting_tools/apps/statusweb/stat_agent.py", encoding="utf-8").read()
tree = ast.parse(src)
want = {"_rpy_matrix", "_mat3_mul", "_cam_extrinsic"}
ns = {"math": math}
for n in tree.body:
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name) and t.id == "CAM_EXTRINSICS":
                exec(compile(ast.Module([n], []), "<x>", "exec"), ns)
    if isinstance(n, ast.FunctionDef) and n.name in want:
        exec(compile(ast.Module([n], []), "<x>", "exec"), ns)

_rpy = ns["_rpy_matrix"]; _cam = ns["_cam_extrinsic"]
ok = True
def chk(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond: ok = False

# 1) 单位矩阵：rpy 全 0
r = _rpy(0, 0, 0)
chk("rpy全0 = 单位阵", all(abs(r[i] - [1,0,0,0,1,0,0,0,1][i]) < 1e-9 for i in range(9)))

# 2) 纯 yaw 90 度：X 轴应转到 Y 轴（右手系，绕 Z 转 +90）
#    注意 r 是行主序扁平数组，col0 = (r[0], r[3], r[6])，不是 (r[0], r[1])。
#    上一版把行当成了列，于是断言出 Rz(-90) 的形状 —— 是测试写错，不是矩阵错。
r = _rpy(0, 0, 90)
chk("yaw=90: X->Y", abs(r[0])<1e-9 and abs(r[3]-1)<1e-9 and abs(r[6])<1e-9,
    "col0=(%.3f,%.3f,%.3f)" % (r[0], r[3], r[6]))

# 3) 纯 pitch -90 度：Z(前) 应转到 ... Ry(-90) 作用于 (0,0,1) -> (sin(-90),0,cos(-90))=(-1,0,0)
r = _rpy(0, -90, 0)
chk("pitch=-90: Z->-X", abs(r[2]+1)<1e-9 and abs(r[8])<1e-9,
    "col2=(%.3f,%.3f,%.3f)" % (r[2], r[5], r[8]))

# 4) 相机1 外参
t, R = _cam(1)
chk("cam1 t 正确", t == (0.34, 0.03, 0.13), str(t))
chk("cam1 有旋转阵", R is not None and len(R) == 9)

# 5) 相机系正前方 1m 的点 (0,0,1) 变换后
x, y, z = 0.0, 0.0, 1.0
nx = R[0]*x + R[1]*y + R[2]*z + t[0]
ny = R[3]*x + R[4]*y + R[5]*z + t[1]
nz = R[6]*x + R[7]*y + R[8]*z + t[2]
print("   相机正前 1m -> base (%.4f, %.4f, %.4f)" % (nx, ny, nz))
# 这里不能想当然：在 ROS 标准约定 R = Rz(yaw)*Ry(pitch)*Rx(roll) 下，
# 相机系正前方 (0,0,1) 变换后是 (sin p, 0, cos p)。只要 |p| < 90 度，
# cos p 恒为正 —— 也就是说标准约定下 pitch=-31.96 算不出"朝下"。
# 这正是轴约定存在歧义的地方，所以界面上把四种组合做成可切换。
# 断言只校验"符合标准约定的数值"，不校验物理直觉。
# 用另一条独立代码路径（教科书闭式解）交叉验证整个矩阵，
# 而不是拿物理直觉当预期 —— 直觉在这个约定上是错的（见上）。
rr, pp, yy = [math.radians(v) for v in (1.93, -31.96, -2.88)]
cr, sr = math.cos(rr), math.sin(rr)
cp, sp = math.cos(pp), math.sin(pp)
cy, sy = math.cos(yy), math.sin(yy)
ref = (cy*cp,  cy*sp*sr - sy*cr, cy*sp*cr + sy*sr,
       sy*cp,  sy*sp*sr + cy*cr, sy*sp*cr - cy*sr,
       -sp,    cp*sr,            cp*cr)
chk("旋转阵与闭式解一致",
    all(abs(R[i] - ref[i]) < 1e-9 for i in range(9)),
    "maxdiff=%.2e" % max(abs(R[i]-ref[i]) for i in range(9)))
chk("(0,0,1) 变换后 = R[:,2]+t",
    abs(nz - (ref[8] + 0.13)) < 1e-9, "z=%.4f" % nz)
chk("距离保持 1m", abs(math.sqrt((nx-t[0])**2+(ny-t[1])**2+(nz-t[2])**2) - 1.0) < 1e-6)

# 6) 未标定的相机
chk("cam2 未标定", _cam(2) == (None, None))
chk("越界回落", _cam(99) == (None, None))
chk("非法输入", _cam("x") == (None, None))

# 7) 单引号禁令
chk("无单引号", "'" not in src)
print("OK" if ok else "HAS FAILURE")
sys.exit(0 if ok else 1)
