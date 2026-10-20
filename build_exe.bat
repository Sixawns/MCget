@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==========================================
echo   mcget 打包脚本 (Windows)
echo   产出: dist\mcget.exe  和  dist\mcget-cli.exe
echo ==========================================
echo.

rem ---------- 0. 找 Python ----------
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 没找到 python。
    echo        先装 Python 3.9~3.12: https://www.python.org/downloads/
    echo        装的时候一定勾上 "Add Python to PATH"。
    echo        别用 Microsoft Store 版，它可能不带 tcl/tk，打出来的界面会开不了。
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python -c "import sys; print(sys.version.split()[0], sys.version_info.major, sys.version_info.minor)"') do set "PYINFO=%%i"
echo [python] %PYINFO%
echo.

rem ---------- 1. 虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
    echo [1/5] 创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败。
        pause
        exit /b 1
    )
) else (
    echo [1/5] .venv 已存在，直接用。
)
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [错误] 激活虚拟环境失败。
    pause
    exit /b 1
)
echo.

rem ---------- 2. 装依赖 ----------
echo [2/5] 安装依赖 (customtkinter + pyinstaller)，走清华源 ...
python -m pip install --upgrade pip -q -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install -q customtkinter pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [错误] 依赖装不上，检查网络或换个源。
    pause
    exit /b 1
)
echo       依赖 OK
echo.

rem ---------- 2.5 检查 tkinter ----------
python -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [警告] 这个 Python 里没有 tkinter，图形界面版打出来也开不了。
    echo        重装 Python 时勾上 "tcl/tk and IDLE"，或者换 python.org 官方版。
    echo        （Microsoft Store 版和某些精简安装经常缺这个。）
    echo.
    pause
    exit /b 1
)

rem ---------- 2.6 旧 exe 还开着的话先关掉 ----------
rem 正在运行的 exe 会被系统锁住，PyInstaller 覆盖它时会报
rem "PermissionError: [WinError 5] 拒绝访问"。这里提前试删一次，
rem 删不掉就说明它还开着 —— 早点告诉用户，免得等几分钟后才炸。
set "LOCKED=0"
for %%N in ("dist\mcget.exe" "dist\mcget-cli.exe") do (
    if exist %%~N (
        del /f /q %%~N >nul 2>&1
        if exist %%~N set "LOCKED=%%~N"
    )
)
if not "%LOCKED%"=="0" (
    echo [错误] %LOCKED% 删不掉，说明它正在运行。
    echo.
    echo        请关掉正在运行的 mcget 窗口再重新跑这个脚本。
    echo        （关了还不行的话，任务管理器里结束掉残留的 mcget.exe 进程。）
    echo.
    tasklist /fi "imagename eq mcget.exe" 2>nul | find /i "mcget.exe" >nul
    if not errorlevel 1 (
        echo        现在确实有一个 mcget.exe 在跑：
        tasklist /fi "imagename eq mcget.exe" 2>nul | find /i "mcget.exe"
        echo.
    )
    pause
    exit /b 1
)

rem ---------- 3. 打包图形界面版 ----------
echo [3/5] 打包图形界面版 (这一步要 1~3 分钟) ...
pyinstaller --noconfirm --clean mcget_gui.spec
if errorlevel 1 (
    echo [错误] GUI 打包失败，往上翻看报错。
    pause
    exit /b 1
)
echo.

rem ---------- 4. 打包命令行版 ----------
echo [4/5] 打包命令行版 ...
pyinstaller --noconfirm --clean mcget_cli.spec
if errorlevel 1 (
    echo [警告] 命令行版打包失败，但图形界面版不受影响。
)
echo.

rem ---------- 5. 收尾 ----------
echo [5/5] 清理中间文件 ...
if exist "build" rmdir /s /q "build"
if exist "__pycache__" rmdir /s /q "__pycache__"
echo.
echo ==========================================
if exist "dist\mcget.exe" (
    for %%A in ("dist\mcget.exe") do set /a "MB=%%~zA/1048576"
    echo   图形界面  dist\mcget.exe      约 !MB! MB   ← 双击这个
) else (
    echo   [失败] dist\mcget.exe 没生成
)
if exist "dist\mcget-cli.exe" (
    for %%A in ("dist\mcget-cli.exe") do set /a "MB2=%%~zA/1048576"
    echo   命令行    dist\mcget-cli.exe  约 !MB2! MB
) else (
    echo   [跳过] 没有 dist\mcget-cli.exe
)
echo ==========================================
echo.
echo 接下来:
echo   1. 把 dist\mcget.exe 拷到别的文件夹，双击试试能不能打开
echo   2. 第一次打开如果闪一下就没了，用命令行跑一下看报错:
echo        dist\mcget-cli.exe list --mirror
echo   3. 杀软可能误报(自解压单文件exe通病)，加白名单即可
echo.
echo 想换图标: 把一个 icon.ico 放到本目录，重新跑这个脚本就会自动用上。
echo.
pause
