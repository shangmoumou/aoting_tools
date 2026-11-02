@echo off
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"

set PY=
rem 为什么不能只靠 where: Windows 有商店占位程序 WindowsApps\python.exe,
rem where 能查到, 但一执行就弹商店并返回 9009。所以必须真跑一次验证。
rem 另外 py.exe 只是启动器, py 的默认版本未必是 3.x, 必须带 -3。
call :try_cmd python
if defined PY goto :py_ok
call :try_cmd python3
if defined PY goto :py_ok
rem Python Launcher: 反查真正的解释器路径; 拿不到路径就直接用 py -3
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
  if not defined PY if exist "%%P" for %%A in ("%%P") do set "PY=%%~sA"
)
if defined PY goto :py_ok
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul && set "PY=py -3"
if defined PY goto :py_ok
rem 默认安装位置(装了但没勾 Add to PATH); 取短路径名避免空格
for /D %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%D\python.exe" for %%A in ("%%D\python.exe") do set "PY=%%~sA"
  if defined PY goto :py_ok
)
for /D %%D in ("%ProgramFiles%\Python3*") do (
  if exist "%%D\python.exe" for %%A in ("%%D\python.exe") do set "PY=%%~sA"
  if defined PY goto :py_ok
)

echo [ERROR] Python not found.
echo Please install Python 3.8+ from https://www.python.org
echo Remember to check "Add Python to PATH" during install.
pause
exit /b 1

:py_ok
echo   python: %PY%

echo [1/2] Checking dependencies ...
%PY% -c "import paramiko" 2>nul
if errorlevel 1 (
  echo      installing paramiko ...
  %PY% -m pip install paramiko -i https://mirrors.cloud.tencent.com/pypi/simple
)
echo      done.
echo.

rem 装不上就直接报错退出: 否则服务照常启动, 页面只会显示"连接失败",
rem 而真正的原因(paramiko 没装)藏在别处, 排查方向全错。
%PY% -c "import paramiko" 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] paramiko is still missing.
  echo          Manual fix: run this in the window:
  echo              %PY% -m pip install paramiko
  echo.
  pause
  exit /b 1
)

echo [2/2] Starting server on port 8766 ...
echo   http://127.0.0.1:8766
echo   (this window can be minimized)
echo.

rem 由门户拉起时 PORTAL_NO_BROWSER=1, 门户自己探测端口再开页面,
rem 否则用户会拿到两个重复的标签页。
if not "%PORTAL_NO_BROWSER%"=="1" if not "%CAMWEB_NO_BROWSER%"=="1" start "" http://127.0.0.1:8766

%PY% server.py 8766

echo.
echo Server stopped.
rem pause 必须是无条件的: 服务挂掉时, 这个窗口是唯一能看到原因的地方。
pause

exit /b 0

rem ---------------------------------------------------------------
rem  验证某个命令是否真的是可用的 python; 命中后把 PY 设成短路径
rem  (短路径: 避免 Program Files 这类带空格的路径出问题)
rem ---------------------------------------------------------------
:try_cmd
rem %1 = 命令名
for /f "delims=" %%P in ('where %1 2^>nul') do (
  echo "%%P" | findstr /i "WindowsApps" >nul
  if errorlevel 1 (
    "%%P" -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
    if not errorlevel 1 (
      for %%A in ("%%P") do set "PY=%%~sA"
      exit /b 0
    )
  )
)
exit /b 1
