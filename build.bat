@echo off
setlocal EnableExtensions

REM ============================================================
REM  弹幕键盘 V2 —— 用 PyInstaller 打包 exe
REM
REM  用法:
REM    build.bat            按 main.spec 打包
REM    build.bat --clean    先删掉 build\ 与 dist\ 再打包
REM    build.bat --check    只检查环境，不打包
REM
REM  前置条件:
REM    1. 已创建 .venv 并装好 requirements.txt 里的依赖
REM    2. 已在本虚拟环境里装好 PyInstaller
REM       本脚本不会替你安装 PyInstaller，缺了就直接报错退出。
REM
REM  【重要】项目路径必须是纯 ASCII
REM    PyQt5 5.15.x 的 QLibraryInfo 处理不了非 ASCII 安装路径，
REM    会把「弹幕键盘」变成「????」，导致 PyInstaller 的 Qt hook 报
REM        Qt plugin directory '.../????V2/.venv/.../plugins' does not exist!
REM    这不是 PyInstaller 的问题，换版本也没用。
REM    本脚本会自动调用 tools\build_helper.py：若项目路径含非 ASCII，
REM    就在 TEMP 下建一个纯 ASCII 的 junction 再从那里构建
REM    （产物仍然落在真实项目目录的 dist\ 里）。
REM    junction 需要普通用户权限即可，不需要管理员。
REM
REM  注意: 本文件必须以 GBK(cp936) 编码 + CRLF 换行保存。
REM ============================================================

chcp 936 >nul

set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
set "SPEC=%ROOT%main.spec"
set "HELPER=%ROOT%tools\build_helper.py"
set "ROOTTXT=%TEMP%\dmkb2_build_root.txt"

set "DO_CLEAN="
set "CHECK_ONLY="
if /i "%~1"=="--clean" set "DO_CLEAN=1"
if /i "%~1"=="--check" set "CHECK_ONLY=1"

echo ============================================
echo    弹幕键盘 V2 - 打包
echo ============================================
echo.

REM ---------- 0. 前置文件 ----------
if not exist "%VENV_PY%" (
    echo [错误] 没有找到虚拟环境:
    echo        %VENV_PY%
    echo        请先创建 .venv 并安装依赖，再运行本脚本。
    goto :fail
)

if not exist "%SPEC%" (
    echo [错误] 找不到打包配置:
    echo        %SPEC%
    goto :fail
)

if not exist "%HELPER%" (
    echo [错误] 找不到构建辅助脚本:
    echo        %HELPER%
    goto :fail
)

REM ---------- 1. PyInstaller 是否已安装 ----------
REM 检查用真实路径的 python 即可；真正构建时必须换用 BUILD_ROOT 下的 python，
REM 原因见第 3 步。
"%VENV_PY%" -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if errorlevel 1 (
    echo [错误] 当前虚拟环境没有安装 PyInstaller。
    echo        本脚本不会自动安装，请手工执行:
    echo.
    echo            "%VENV_PY%" -m pip install pyinstaller
    echo.
    echo        装好后重新运行 build.bat。
    goto :fail
)

REM ---------- 2. 确定构建根（必要时建 ASCII junction） ----------
REM 用 --out 写文件 + set /p 读，而不是 for /f：
REM for /f 解析带引号的命令时会把引号弄坏，实测报
REM 「文件名、目录名或卷标语法不正确」。
REM
REM PYTHONIOENCODING 必须设成 gbk：本脚本开头已经 chcp 936，控制台就是 GBK，
REM 而 Python 3 默认按 UTF-8 写 stderr，中文提示会变成乱码。
echo [环境] 检查项目路径是否可用于打包 ...
if exist "%ROOTTXT%" del "%ROOTTXT%" >nul 2>&1

set "PYTHONIOENCODING=gbk:replace"
REM stdout 会多打一行构建根（给人看的），批处理里已经有 --out 文件了，直接丢掉
"%VENV_PY%" "%HELPER%" --out "%ROOTTXT%" >nul
set "HELPER_RC=%ERRORLEVEL%"
set "PYTHONIOENCODING="

if not "%HELPER_RC%"=="0" goto :noroot
if not exist "%ROOTTXT%" goto :noroot

set "BUILD_ROOT="
set /p BUILD_ROOT=<"%ROOTTXT%"
if not defined BUILD_ROOT goto :noroot

if /i "%BUILD_ROOT%"=="%ROOT:~0,-1%" (
    echo        路径是纯 ASCII，直接构建: %BUILD_ROOT%
) else (
    echo        已切换到 ASCII 路径构建: %BUILD_ROOT%
    echo        junction 是透明重定向，产物仍在 %ROOT%dist\ 里
)

if defined CHECK_ONLY (
    echo.
    echo [完成] --check 模式：环境与路径都没问题，可以正常打包。
    goto :end
)

REM ---------- 3. 清理 ----------
if defined DO_CLEAN (
    echo [清理] 删除 build\ 与 dist\ ...
    if exist "%BUILD_ROOT%\build" rmdir /s /q "%BUILD_ROOT%\build"
    if exist "%BUILD_ROOT%\dist"  rmdir /s /q "%BUILD_ROOT%\dist"
)

REM ---------- 4. 打包 ----------
REM 【关键】必须用 BUILD_ROOT 下的 python.exe，而不是 %VENV_PY%。
REM 光换工作目录不够：PyQt5 的 QLibraryInfo 是按 **sys.prefix**（由 exe 自身
REM 的路径推出）去算 Qt 安装前缀的，跟 CWD 无关。从真实中文路径的
REM python.exe 启动，sys.prefix 仍是中文路径，QLibraryInfo 照样返回 '????V2'，
REM Qt hook 照样失败——实测就是这个原因让「已经有 junction 了还是报错」。
REM 所以：junction 下的 python.exe 启动后 sys.prefix 是纯 ASCII 的，
REM QLibraryInfo 才拿得到正确路径。
set "BUILD_PY=%BUILD_ROOT%\.venv\Scripts\python.exe"
if not exist "%BUILD_PY%" (
    echo [错误] 在构建根里找不到 python.exe:
    echo        %BUILD_PY%
    goto :fail
)

pushd "%BUILD_ROOT%"
echo [打包] 使用 main.spec 构建，这一步比较慢 ...
echo        使用的解释器: %BUILD_PY%
echo.
"%BUILD_PY%" -m PyInstaller --noconfirm --clean main.spec
set "RC=%ERRORLEVEL%"
popd

echo.
if not "%RC%"=="0" (
    echo [失败] PyInstaller 返回 %RC%，打包失败。请查看上面的输出。
    goto :fail
)

echo [完成] 产物目录: %ROOT%dist\弹幕键盘V2
if exist "%ROOT%dist\弹幕键盘V2\弹幕键盘V2.exe" (
    echo        可执行文件: %ROOT%dist\弹幕键盘V2\弹幕键盘V2.exe
    echo.
    echo        可以这样验证:
    echo            "%ROOT%dist\弹幕键盘V2\弹幕键盘V2.exe" --selftest
) else (
    echo        [提示] 没看到 弹幕键盘V2.exe，请自行检查 dist 目录内容。
)
goto :end

:noroot
echo.
echo [错误] 无法准备一个可用的构建根目录。
echo        通常是项目路径含非 ASCII 字符，而 junction 也没建成。
echo        请把项目移到纯 ASCII 路径，例如 D:\danmaku-kb ，或手工建 junction：
echo.
echo            mklink /J "C:\dmkb2" "%ROOT:~0,-1%"
echo.
echo        然后从 C:\dmkb2 运行 build.bat。
goto :fail

:fail
echo.
pause
exit /b 1

:end
endlocal
exit /b 0
