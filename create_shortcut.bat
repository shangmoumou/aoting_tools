@echo off
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  为什么快捷方式指向 cmd.exe，而不是直接指向 .bat
rem ------------------------------------------------------------
rem  Windows 不允许把 .bat / .cmd（以及指向它们的快捷方式）固定
rem  到任务栏：脚本没有 AppUserModelID，右键菜单里根本不会出现
rem  「固定到任务栏」，拖到任务栏上也没反应。
rem
rem  让 .lnk 指向 cmd.exe、把 bat 作为参数传进去，Windows 看到的
rem  就是一个真正的可执行文件，于是可以固定 —— 微软官方文档也是
rem  这么建议的。图标、名称、起始位置都照常生效。
rem ============================================================

set "NAME=AOTING Tools Portal.lnk"
set "BAT=%~dp0start_aoting_tools.bat"
set "ICO=%~dp0brand.ico,0"
set "PS1=%~dp0tools\make_shortcut.ps1"

if not exist "%BAT%" (
  echo.
  echo [ERROR] 找不到启动脚本:
  echo         %BAT%
  echo.
  pause
  exit /b 1
)

if not exist "%PS1%" (
  echo.
  echo [ERROR] 缺少 tools\make_shortcut.ps1
  echo         请完整解压后再运行本脚本。
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Bat "%BAT%" -Ico "%ICO%" -Name "%NAME%"

if errorlevel 1 (
  echo.
  echo [ERROR] 快捷方式创建失败。
  echo.
  pause
  exit /b 1
)

echo.
echo   快捷方式已创建（带公司图标）:
echo     1. 桌面     : %NAME%
echo     2. 本文件夹 : %NAME%
echo.
echo   固定到任务栏:
echo     在桌面上右键该快捷方式 - 显示更多选项 - 固定到任务栏
echo.
echo   两点注意:
echo     a. 固定后请不要再改名或移动本文件夹，否则图标会失效；
echo        移动了就取消固定，重新运行本脚本再固定一次。
echo     b. 如果任务栏上已经有「命令提示符」，新图标可能会跟它
echo        合并成一组 —— 把那个取消固定即可。
echo.
pause
