@echo off
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"

rem ===================================================================
rem  哈工澳汀调试工具箱 - 启动脚本
rem
rem  【为什么要这么费劲地找 Python】
rem  Windows 自带一个 Microsoft Store 的 python 占位程序：
rem      C:\Users\XXX\AppData\Local\Microsoft\WindowsApps\python.exe
rem  它「存在」、where 也能查到，但一执行就弹出商店或返回 9009。
rem  只做 where 检测会误判成"有 Python"，然后在真正启动时失败 ——
rem  这就是之前别人电脑上 [ERROR] 9009 的原因。
rem  所以这里的规则是：不只要"找到"，必须真的跑一遍才算数。
rem
rem  【没有 Python 时怎么办】
rem  早期版本只提示"请自行下载安装"，结果每台新电脑都要人工教一遍。
rem  现在会自动装（用户级，不需要管理员权限）：
rem     1) winget   —— 官方渠道，能自动匹配 x64/ARM64
rem     2) 官方安装包 —— python.org 直连
rem     3) 国内镜像   —— registry.npmmirror.com（国内快得多）
rem  三条路都不通才退回手动安装提示。
rem ===================================================================

rem ---- 0. 确认文件完整 ----------------------------------------------
if not exist "portal_server.py" (
  echo.
  echo [ERROR] 当前目录下找不到 portal_server.py
  echo.
  echo         目录: %CD%
  echo.
  echo         多半是压缩包没有完整解压（只解压出了部分文件），
  echo         或者解压后又移动过文件夹。请重新完整解压再运行。
  echo.
  if not "%AOTING_HIDDEN_START%"=="1" pause
  exit /b 1
)

rem ---- 1. 查找可用的 Python -----------------------------------------
set PORT=8899
set PY=
set PYFROM=

rem 1a) PATH 里的命令：逐个候选真实执行，跳过商店占位程序
call :try_cmd python "PATH"
if defined PY goto :found
call :try_cmd python3 "PATH"
if defined PY goto :found
rem 1c) Python Launcher：用 py -3 反查真正的 python.exe 路径
rem     为什么需要这一段：部分机器 PATH 里没有 python.exe，只有
rem     C:\Windows\py.exe；而裸 py 的默认版本未必是 3.x，
rem     必须带 -3 才可靠。这里让 py -3 自己告诉我们解释器在哪，
rem     于是 PY 仍然是完整路径，后续调用无需再改。
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
  call :try_path "%%P" "py launcher"
  if defined PY goto :found
)
if defined PY goto :found

rem 1b) 默认安装位置（装了 Python 但没勾 Add to PATH 时靠这一步救回来）
for /D %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  call :try_path "%%D\python.exe" "用户目录"
  if defined PY goto :found
)
for /D %%D in ("%ProgramFiles%\Python3*") do (
  call :try_path "%%D\python.exe" "Program Files"
  if defined PY goto :found
)
for /D %%D in ("C:\Python3*") do (
  call :try_path "%%D\python.exe" "C 盘根目录"
  if defined PY goto :found
)

goto :no_python

:found
rem 刚装好的 Python 可能还没进当前会话的 PATH。补上它，
rem 否则子脚本（远程相机的 start.bat 里的 python）仍然会找不到。
if defined PY for %%D in ("%PY%") do set "PATH=%%~dpD;%%~dpD\Scripts;%PATH%"

echo.
echo   AOTING TOOLS
echo   ------------------------------------------
echo   Python : %PYFROM%
echo   URL    : http://127.0.0.1:%PORT%
echo   NOTE   : Close the browser tab and this window closes too.
echo            To stop manually, press Ctrl+C here.
echo   ------------------------------------------
echo.
goto :run

rem ---- 2. 没找到可用 Python -> 自动安装 ------------------------------
:no_python
rem Hidden startup cannot accept installation prompts; use the interactive launcher.
if "%AOTING_HIDDEN_START%"=="1" exit /b 1
echo.
echo ==================================================================
echo   没有找到可用的 Python 3.8+
echo ==================================================================
echo.
echo   本工具箱需要一个 Python 运行环境（只用其标准库，不装任何第三方包）。
echo.
echo   现在可以【自动安装】Python 3.12：
echo      · 只装给当前用户，不需要管理员权限
echo      · 不会动系统里已有的其它 Python
echo      · 约 30MB，装完自动继续启动工具箱
echo.
choice /C YN /N /T 20 /D Y /M "  是否自动安装？(Y=立即安装 / N=我自己装) "
if errorlevel 2 goto :manual_python

call :install_python
if defined PY goto :found

echo.
echo   [ERROR] 自动安装没有成功。请按下面的方式手动安装。
echo.

rem ---- 2b. 手动安装的说明 -------------------------------------------
:manual_python
echo   ------------------------------------------------------------------
echo   请到下面地址下载安装 Python 3.8 或更高版本：
echo.
echo       https://www.python.org/downloads/
echo.
echo   【安装时务必勾选】 Add Python to PATH
echo   （没勾选的话，安装完要重新双击本脚本才会被识别）
echo   ------------------------------------------------------------------
echo.
choice /C YN /N /T 15 /D N /M "  现在打开 Python 下载页？(Y/N) "
if errorlevel 2 goto :np_end
start "" https://www.python.org/downloads/
:np_end
echo.
if not "%AOTING_HIDDEN_START%"=="1" pause
exit /b 1

rem ---- 3. 正式启动 ---------------------------------------------------
:run
set PORT=8899
set ROOT=%~dp0

rem 杀掉旧进程：Windows 允许重复绑定同一端口，残留的旧窗口会继续提供服务，
rem 导致你看到的其实是旧页面（改了东西"没生效"多半是这个原因）。
rem Hidden launcher reuses an existing service; never kill another launch.
if "%AOTING_HIDDEN_START%"=="1" goto :start_server
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr LISTENING') do (
  echo   [0/3] Stopping old server on port %PORT% ^(PID %%a^) ...
  taskkill /F /PID %%a >nul 2>&1
)
rem Python sleep works with redirected stdin in windowless startup.
"%PY%" -c "import time; time.sleep(1)"

:start_server
echo   [1/3] Starting server ...

start "" http://127.0.0.1:%PORT%

"%PY%" portal_server.py
set RC=%ERRORLEVEL%

rem 退出码 0 = 正常（页面关闭 / Ctrl+C）-> 静默关窗
rem 只有真出错才 pause，方便看清报错
if "%RC%"=="0" exit /b 0

echo.
echo [ERROR] Server exited with code %RC%
echo.
echo   常见原因：
echo     9009 / 1  -> Python 命令异常，运行 tools\check_env.bat 查看检测结果
echo     10048     -> 端口 8899 被占用，关闭其它调试工具后重试
echo.
if not "%AOTING_HIDDEN_START%"=="1" pause
exit /b %RC%

rem ===================================================================
rem  子程序：验证一个 Python 命令是否真的可用
rem  规则：必须能执行、版本 >= 3.8、且所需标准库齐全
rem ===================================================================

:try_cmd
rem %1 = 命令名  %2 = 来源说明
for /f "delims=" %%P in ('where %1 2^>nul') do (
  rem 跳过 Microsoft Store 的占位程序（在 WindowsApps 目录下）
  echo "%%P" | findstr /i "WindowsApps" >nul
  if errorlevel 1 (
    call :try_path "%%P" "%2"
    if defined PY exit /b 0
  )
)
exit /b 1

:try_path
rem %1 = python.exe 完整路径  %2 = 来源说明
if not exist "%~1" exit /b 1
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul || exit /b 1
"%~1" -c "import json,hashlib,http.server,urllib.request,socketserver" >nul 2>nul || exit /b 1
set "PY=%~1"
set "PYFROM=%~2  %~1"
exit /b 0

rem ===================================================================
rem  子程序：自动安装 Python（用户级，无需管理员）
rem  成功 -> 设置 PY / PYFROM 并 exit /b 0
rem  失败 -> PY 为空并 exit /b 1
rem ===================================================================
:install_python
set PY=
set PYFROM=

echo.
echo   正在准备安装 Python 3.12，请稍候（一般 1-3 分钟）...
echo.

rem --- 方法 1：winget（官方渠道，自动匹配 x64 / ARM64）---
where winget >nul 2>nul
if errorlevel 1 goto :ip_dl

echo   [1/2] 尝试用 winget 安装 ...
echo         （若这里长时间不动，是公司网络访问微软商店源较慢，请耐心等）
winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements --disable-interactivity
call :find_new_python
if defined PY (
  echo   [OK] winget 安装完成。
  exit /b 0
)
echo   [!] winget 没能装上，改用下载安装包的方式。
echo.

rem --- 方法 2：下载官方安装包 ---
rem 三个源依次尝试：官方 -> 华为云 -> 阿里云。
rem 官方源已核对存在；两个国内镜像是给"公司网络访问 python.org 很慢"
rem 的情况兜底。注意镜像路径是 /python/<版本>/，不是 /-/binary/python/。
rem
rem 这里用 goto 链而不是 for 循环：for 块内的变量在解析时就被展开，
rem 循环里下载成功设置的标志在块内读不到（经典延迟展开陷阱）。
:ip_dl
set "PYVER=3.12.6"
set "PYEXE=%TEMP%\aoting_python_%PYVER%.exe"
if exist "%PYEXE%" del "%PYEXE%" >nul 2>&1
echo   [2/2] 下载 Python 安装包（约 30MB）...

:ip_dl1
call :try_download "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
if not errorlevel 1 goto :ip_run
echo         [!] 官方源不可达，换华为云镜像 ...

:ip_dl2
call :try_download "https://mirrors.huaweicloud.com/python/%PYVER%/python-%PYVER%-amd64.exe"
if not errorlevel 1 goto :ip_run
echo         [!] 华为云也不可达，换阿里云镜像 ...

:ip_dl3
call :try_download "https://npmmirror.com/mirrors/python/%PYVER%/python-%PYVER%-amd64.exe"
if not errorlevel 1 goto :ip_run
echo         [!] 三个源都没下载成功。
exit /b 1

:ip_run
echo.
echo         下载完成，正在安装（后台静默，约 1-2 分钟）...
echo.
"%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 AssociateFiles=0 Shortcuts=0 Include_launcher=0 Include_test=0
set IPRC=%ERRORLEVEL%
del "%PYEXE%" >nul 2>&1
echo.

rem 安装程序的 /quiet 理论上是同步的，但个别机器上会提前返回，
rem 所以这里轮询等 python.exe 真正出现，最多 3 分钟。
set WAITED=0
:ip_wait
call :find_new_python
if defined PY (
  echo   [OK] 安装完成（等待 %WAITED% 秒）。
  exit /b 0
)
if %WAITED% GEQ 180 (
  echo   [!] 等待超时（安装程序退出码 %IPRC%）。
  exit /b 1
)
timeout /t 3 >nul
set /a WAITED+=3
goto :ip_wait

rem ---------------------------------------------------------------
rem  从单个源下载，成功 exit /b 0，失败 exit /b 1
rem ---------------------------------------------------------------
:try_download
rem %1 = 完整下载 URL
echo         源: %~1
if exist "%PYEXE%" del "%PYEXE%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue';[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;(New-Object System.Net.WebClient).DownloadFile('%~1','%PYEXE%')"
call :check_installer "%PYEXE%"
exit /b %ERRORLEVEL%

rem ---------------------------------------------------------------
rem  检查下载到的文件是不是完整安装包
rem  曾经踩过：下载失败时服务器返回一段 HTML，文件照样存在，
rem  于是"安装了"却什么都没发生 —— 必须以文件大小为据。
rem ---------------------------------------------------------------
:check_installer
rem %1 = 文件路径；成功 exit /b 0，失败 exit /b 1
if not exist "%~1" (
  echo         [!] 没有下载到文件。
  exit /b 1
)
set FSIZE=0
for %%F in ("%~1") do set FSIZE=%%~zF
if %FSIZE% LSS 5000000 (
  echo         [!] 下载到的文件只有 %FSIZE% 字节，不是完整安装包（多半返回了网页）。
  del "%~1" >nul 2>&1
  exit /b 1
)
echo         已下载 %FSIZE% 字节。
exit /b 0

rem ---------------------------------------------------------------
rem  在安装完成后重新找一遍 Python
rem  顺序：用户目录（刚装的在这） -> Program Files -> PATH
rem ---------------------------------------------------------------
:find_new_python
set PY=
set PYFROM=
for /D %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  call :try_path "%%D\python.exe" "自动安装"
  if defined PY exit /b 0
)
for /D %%D in ("%ProgramFiles%\Python3*") do (
  call :try_path "%%D\python.exe" "自动安装"
  if defined PY exit /b 0
)
call :try_cmd python "自动安装"
if defined PY exit /b 0
exit /b 1
