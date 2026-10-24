@echo off
setlocal EnableExtensions

REM ============================================================
REM  幕键 —— 一键启动
REM
REM  用法:
REM    启动.bat              启动图形界面，不弹控制台窗口
REM    启动.bat --console    带控制台启动，便于看报错
REM    启动.bat --selftest   只跑离线自检，不开界面、不联网
REM    启动.bat --check-env  只查注入环境，按键无效时先跑这个
REM
REM  注意: 本文件必须以 GBK(cp936) 编码 + CRLF 换行保存。
REM       用 UTF-8 保存时，中文区域下的 cmd.exe 会显示乱码。
REM ============================================================

chcp 936 >nul

set "ROOT=%~dp0"
set "SRC=%ROOT%src"
set "ENTRY=%SRC%\screenkey\main.py"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
set "VENV_PYW=%ROOT%.venv\Scripts\pythonw.exe"

set "MODE=run"
if /i "%~1"=="--console"  set "MODE=console"
if /i "%~1"=="--selftest" set "MODE=selftest"
if /i "%~1"=="--check-env" set "MODE=checkenv"

echo ============================================
echo    幕键
echo ============================================
echo.

REM ---------- 0. 受限宿主预警 ----------
REM 在沙箱/自动化宿主里运行时，Windows 会**静默丢弃**注入的按键：
REM SendInput 返回成功、GetLastError 是 0，目标窗口却收不到。本脚本被这类
REM 宿主启动时提前说清楚，而不是让用户对着"日志全绿但按键没反应"排查半天。
REM 注意：`--selftest` / `--check-env` 是给自动化用的分支，**不能 pause**，
REM       否则在管道或无人值守环境里会永远等一个不会到来的按键。
set "RESTRICTED_HOST="
if defined DSH_SESSION_ID set "RESTRICTED_HOST=1"
if defined DSH_SHELL set "RESTRICTED_HOST=1"
if not defined RESTRICTED_HOST goto :after_warn

echo [警告] 检测到本脚本是在受限宿主（沙箱/自动化宿主）里启动的。
echo        这种环境下模拟按键会被 Windows 静默丢弃：
echo        日志里会显示按键已发送，记事本和游戏却收不到任何输入。
echo.
echo        想真正用起来，请改用这两种方式之一：
echo          1. 在 Windows 资源管理器里双击「启动.bat」
echo          2. 直接双击打包好的 exe
echo.
echo        只想确认环境问题（会移动光标 1 像素再复位）可运行：
echo          启动.bat --check-env
echo.
if /i not "%MODE%"=="selftest" if /i not "%MODE%"=="checkenv" pause
goto :after_warn

:after_warn

REM ---------- 1. 入口文件 ----------
if not exist "%ENTRY%" (
    echo [错误] 找不到入口文件:
    echo        %ENTRY%
    echo        请确认「启动.bat」放在项目根目录下，且 src 目录完整。
    goto :fail
)

REM ---------- 2. 虚拟环境 ----------
REM 本项目不自带依赖，也不会自动联网安装。缺 .venv 时只给指引。
if not exist "%VENV_PY%" (
    echo [错误] 没有找到虚拟环境 .venv
    echo.
    echo        本项目不会自动安装依赖，请先手工创建环境:
    echo.
    echo            cd /d "%ROOT%"
    echo            python -m venv .venv
    echo            .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    echo        依赖装好后再运行本脚本。
    goto :fail
)

REM ---------- 3. 依赖齐不齐，只检查不安装 ----------
"%VENV_PY%" -c "import sys;from importlib import util;mods=['PyQt5','pynput','requests','segno','websockets','aiohttp','blivedm'];missing=[m for m in mods if util.find_spec(m) is None];print('[检查] 依赖齐全') if not missing else (print('[检查] 缺少依赖: '+', '.join(missing)),print('       请执行: .venv\\Scripts\\python.exe -m pip install -r requirements.txt'));sys.exit(1 if missing else 0)"
if errorlevel 1 goto :fail

if /i "%MODE%"=="selftest" goto :selftest
if /i "%MODE%"=="checkenv" goto :checkenv

REM ---------- 4. 启动 ----------
if /i "%MODE%"=="console" goto :console

set "GUI_PY=%VENV_PYW%"
if not exist "%GUI_PY%" (
    echo [提示] 没有 pythonw.exe，改用 python.exe，会多一个控制台窗口。
    set "GUI_PY=%VENV_PY%"
)

echo [启动] 正在打开程序窗口 ...
start "" /D "%SRC%" "%GUI_PY%" "%ENTRY%"
echo [完成] 启动命令已发出。若窗口没出现，请改用:  启动.bat --console
goto :end

:console
echo [启动] 控制台模式。关闭窗口或按 Ctrl+C 退出。
echo.
"%VENV_PY%" "%ENTRY%"
set "RC=%ERRORLEVEL%"
echo.
echo [退出] 返回码: %RC%
if not "%RC%"=="0" echo [提示] 返回码非 0，请对照上面的错误信息。
pause
goto :end

:checkenv
echo [环境] 检查本机注入环境 ...
echo.
"%VENV_PY%" "%ENTRY%" --check-env
echo.
pause
goto :end

:selftest
echo [自检] 运行离线自检，不联网、不开界面 ...
echo.
REM 入口自己会把 src 插进 sys.path，这里不需要再设任何环境变量
"%VENV_PY%" "%ENTRY%" --selftest
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" echo [结果] 自检通过。
if not "%RC%"=="0" echo [结果] 自检失败，返回码 %RC%。
pause
goto :end

:fail
echo.
pause
exit /b 1

:end
endlocal
exit /b 0
