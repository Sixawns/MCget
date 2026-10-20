# -*- mode: python ; coding: utf-8 -*-
"""
mcget 图形界面版打包配置（PyInstaller）

用法：
    pyinstaller --noconfirm --clean mcget_gui.spec
产物：
    dist/mcget.exe     双击即用，不弹黑框

两个坑，这里已经处理好了：
1. customtkinter 没有官方 PyInstaller hook，它的主题 json 和 Roboto 字体
   不会被自动打包 —— 不加 collect_data_files 的话，exe 双击会直接崩。
2. console=False（-w）才能不弹黑框；同时别关掉 disable_windowed_traceback，
   否则出错时连个提示框都没有，用户只看到"闪一下就没了"。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# customtkinter 的运行时资源：assets/themes/*.json + assets/fonts/*
ctk_datas = collect_data_files("customtkinter")
ctk_hidden = collect_submodules("customtkinter")

icon_path = os.path.join(SPECPATH, "icon.ico")
has_icon = os.path.isfile(icon_path)

a = Analysis(
    [os.path.join(SPECPATH, "mcget_gui.py")],
    pathex=[SPECPATH],          # 让 mcget.py 能被自动收进来
    binaries=[],
    datas=ctk_datas,
    hiddenimports=ctk_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 这些是 pyinstaller 常见误带的大包，砍掉能小几十 MB
    excludes=[
        "numpy", "matplotlib", "scipy", "pandas", "PIL",
        "pytest", "IPython", "notebook", "tkinter.test",
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
    name="mcget",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # upx 容易被杀软误报，关掉
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,              # -w：不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path if has_icon else None,   # 想换图标就把 icon.ico 放同目录
)
