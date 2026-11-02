@echo off
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"

set PY=
rem 为什么不能只 where: Windows 有个商店占位程序 WindowsApps\python.exe,
rem where 能查到, 但一执行就弹商店或返回 9009。必须真跑一遍才算数。
rem 另外 py.exe 只是启动器, 裸 py 的默认版本未必是 3.x, 必须带 -3。
call :try_cmd python
if defined PY goto :py_ok
call :try_cmd python3
if defined PY goto :py_ok
rem Python Launcher: 反查真实解释器路径; 查不到就直接用 py -3 当命令
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
  if not defined PY if exist "%%P" for %%A in ("%%P") do set "PY=%%~sA"
)
if defined PY goto :py_ok
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul && set "PY=py -3"
if defined PY goto :py_ok
rem 默认安装位置(装了但没勾 Add to PATH); 取短路径避免空格
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

echo [1/3] Checking dependencies ...
%PY% -c "import paramiko" 2>nul
if errorlevel 1 (
  echo      installing paramiko ...
  %PY% -m pip install paramiko -i https://mirrors.cloud.tencent.com/pypi/simple
)
%PY% -c "import PIL" 2>nul
if errorlevel 1 (
  echo      installing pillow ...
  %PY% -m pip install pillow -i https://mirrors.cloud.tencent.com/pypi/simple
)
echo      done.
echo.

rem Verify paramiko really is importable now.
rem Installing is best-effort (mirror may be blocked); if it did not work,
rem starting the server anyway only produces a useless "cannot connect"
rem with no clue. Fail loudly here instead.
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

echo [2/3] Stopping any old server on port 8765 ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr LISTENING') do (
  echo      killing old PID %%a
  taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 >nul

echo [3/3] Starting server ...
echo   http://127.0.0.1:8765
echo   (this window can be minimized - logs also shown in page)
echo.

rem When launched by the portal, CAMWEB_NO_BROWSER=1 is set.
rem The portal probes the port and opens the page itself,
rem otherwise the user would get a duplicate tab.
if not "%CAMWEB_NO_BROWSER%"=="1" start "" http://127.0.0.1:8765

%PY% server.py 8765

echo.
echo Server stopped.
rem pause must be unconditional: if the server dies, this window is the
rem only place to see why. Never skip it.
pause

exit /b 0

rem ---------------------------------------------------------------
rem  验证命令是否真的可用; 可用则把 PY 设成它的短路径
rem  (短路径: 下面调用不带引号, 含空格的路径会出事)
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
