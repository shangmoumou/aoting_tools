# 数据分析工具箱 · 门户

三个工具集成在一个入口，新增工具不需要改门户代码。

---

## 一、怎么用（Windows）

解压后双击 **`start_aoting_tools.bat`**，浏览器自动打开 `http://127.0.0.1:8899`。

**关掉浏览器标签页，黑窗口会在 5 秒后自动关闭。**

手动停止：在黑窗口按 `Ctrl+C`。

---

## 二、目录结构

```
portal/
├── index.html              门户页面（自动扫描 apps，不硬编码工具）
├── sw.js                   Service Worker（离线 + 更新）
├── version.json            自动生成的应用清单
├── portal_server.py        静态服务（带心跳，关页面即退出）
├── start_aoting_tools.bat        Windows 启动
│
├── apps/                   ← 每个工具一个目录，互不干扰
│   ├── webbag/
│   │   ├── app.json        工具描述：名字/图标/版本/类型
│   │   ├── index.html      构建产物（单文件，可独立双击打开）
│   │   └── src/            源码 ← 改功能改这里
│   │       ├── index.html  页面骨架
│   │       ├── bag.js      bag 解析
│   │       ├── app.js      业务逻辑
│   │       └── build.py    打包成上层 index.html
│   ├── imuweb/
│   │   ├── app.json
│   │   ├── index.html
│   │   └── src/            index.html / bag.js / chart.js / app.js / build.py
│   └── camweb/
│       ├── app.json
│       ├── index.html
│       ├── server.py       本地服务（浏览器不能自己 SSH）
│       ├── cam_agent.py    上车运行的脚本
│       └── start.bat
│
└── tools/
    ├── make_version.py     扫描 apps，生成 version.json
    ├── add_tool.py         新增工具脚手架
    ├── build_all.py        一键重建所有工具
    ├── deploy.sh           发布到服务器（Linux/Mac）
    ├── deploy.bat          发布到服务器（Windows）
    ├── nginx.conf          服务器配置
    └── test_portal.py      测试
```

---

## 三、改某个工具的功能（以 imuweb 为例）

```bash
cd apps/imuweb/src        # 进源码目录
# 改 app.js / chart.js / index.html
python3 build.py          # 打包成 ../index.html
cd ../../..               # 回 portal 根目录
python3 tools/make_version.py
```

刷新页面即可。**不需要改门户的任何代码。**

> 为什么 src/ 和 index.html 要有两份：
> `src/` 分文件才好维护，但 ES module 在 `file://` 下会被浏览器拦掉，
> 双击打不开。所以 `build.py` 把它们内联成一个单文件 `index.html`。

---

## 四、新增一个工具

```bash
python3 tools/add_tool.py mytool 我的工具
```

会生成 `apps/mytool/`（含 app.json + src/ 模板），然后：

1. 写 `src/` 里的代码
2. `cd apps/mytool/src && python3 build.py`
3. 回根目录 `python3 tools/make_version.py`
4. 刷新页面，**卡片自动出现**

---

## 五、camweb（远程相机）单独说明

浏览器不能自己发起 SSH，所以它需要一个跑在你电脑上的 Python 服务。

**首次使用**：在门户页面点「远程相机」卡片 → 下载 `server.py` +
`cam_agent.py` + `start.bat` 到同一目录 → 双击 `start.bat` → 回门户刷新，
卡片状态变绿 → 点卡片即可进入。

它只监听 `127.0.0.1`，不外开端口；SSH 密码只在内存里，不落盘。
画面和账号不经过任何服务器。

---

## 六、部署到公司内网服务器

```bash
# 1. 服务器装 nginx，把 portal 目录内容放到 /var/www/tools
#    （注意：放的是目录里的内容，不含 portal 这一层）
cp tools/nginx.conf /etc/nginx/conf.d/tools.conf
nginx -t && systemctl reload nginx

# 2. 本地发布
./tools/deploy.sh        # Linux/Mac
tools\deploy.bat         # Windows
```

`nginx.conf` 用的是 `server_name _;`，内网 IP 直接访问，不需要域名。

### http + IP 的一个限制

Service Worker 只在 **https 或 localhost** 下能注册。别人用
`http://192.168.x.x` 打开时：

| 能力 | https / 127.0.0.1 | http + IP |
|---|---|---|
| 三个工具 | ✅ | ✅ |
| 版本更新提示 | ✅ | ✅（已降级，改用 localStorage） |
| 离线可用 | ✅ | ❌ |

页面顶部会显示当前处于哪种模式。需要离线能力就让 IT 签一张内网 IP 证书，
改成 `listen 443 ssl`。

---

## 七、测试

```bash
python3 tools/test_portal.py
```

覆盖：目录结构、app.json 完整性、版本号一致性、SW 不拦截本地服务、
**关页面后服务自动退出（真起进程实测）**、点进工具不被误杀。


---

## 工具箱结构（aoting_tools）

```
aoting_tools/
  start_aoting_tools.bat        双击启动（自动杀掉旧的同端口进程）
  create_shortcut.bat     在桌面生成带品牌图标的快捷方式
  brand.ico               品牌图标（favicon + 快捷方式图标）
  portal_server.py        本地静态服务，带"关页面即退出"
  index.html              门户页面
  sw.js                   Service Worker（离线 + 更新检测）
  apps/
    webbag/   app.json + index.html + src/
    imuweb/   app.json + index.html + src/
    camweb/   app.json + 完整 python 文件（server.py / cam_agent.py / start.bat）
  tools/
    make_version.py  扫描 apps 生成 version.json
    add_tool.py      新增工具脚手架
    build_all.py     一键重建
    test_portal.py   测试
```

### 新增工具

python3 tools/add_tool.py mytool "我的工具"
—— 门户完全由 apps/*/app.json 驱动，加工具不用改门户代码。

### 改某个工具

1. 改 apps/<工具>/src/ 下的源文件
2. cd apps/<工具>/src && python3 build.py    打包成 ../index.html
3. cd 回根目录 && python3 tools/make_version.py   更新版本清单

### 关掉网页会关掉黑窗口

门户页面每 4 秒发一次心跳；关闭时发告别。服务端收到告别后 5 秒退出。
即使页面不发心跳（老版本页面、工具页面），也会按"最后一次任意请求"
兜底退出 —— 不会出现黑窗口永远关不掉的情况。

IDLE_TIMEOUT 必须大于 60 秒：浏览器把后台标签页的定时器节流到
60 秒一次，小于 60 会把"切到后台"误判成"页面关了"。

### 远程相机（camweb）说明

浏览器不能自己发起 SSH，所以 camweb 需要一个跑在本机的服务。
**文件已经在 apps/camweb/ 里，不需要下载任何东西。**
用法：打开 apps/camweb/ 双击 start.bat，回到门户点远程相机卡片。


### 远程相机：点卡片即可，不用手动开 start.bat

门户后端跑在本机，可以替浏览器启动本地程序：

1. 点「远程相机」卡片
2. 门户调 `/api/launch`，后端启动 `apps/camweb/start.bat`（新控制台窗口）
3. 门户轮询 8765，最多等 90 秒（首次要 pip install paramiko）
4. 就绪后自动打开页面

安全约束：只能启动 `apps/<id>/app.json` 里**已声明**的脚本，
路径 realpath 必须仍在工具箱目录内，且只认 .bat/.cmd/.py/.exe。
任何越权请求都会被拒绝。20 秒内重复点击只启动一次，避免开一堆黑窗口。

> camweb 的黑窗口需要手动关闭（它是独立服务，不随门户退出）。
