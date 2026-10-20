# -*- mode: python ; coding: utf-8 -*-
"""
mcget 命令行版打包配置（PyInstaller）

用法：
    pyinstaller --noconfirm --clean mcget_cli.spec
产物：
    dist/mcget-cli.exe     在 cmd / PowerShell 里跑，会显示实时进度

这个版本只用标准库，打包出来很小（约 8MB），适合放 U 盘或脚本里调用。
"""

import os

icon_path = os.path.join(SPECPATH, "icon.ico")
has_icon = os.path.isfile(icon_path)

a = Analysis(
    [os.path.join(SPECPATH, "mcget.py")],
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "matplotlib", "scipy", "pandas", "PIL",
        "pytest", "IPython", "notebook", "customtkinter",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mcget-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,               # 命令行版要保留黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path if has_icon else None,
)
