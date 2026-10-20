#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcget_gui.py —— mcget 的图形界面（CustomTkinter 暗黑风）

先装依赖：
    pip install customtkinter
跑起来：
    python mcget_gui.py
打包成 exe（做完这一步用户就不用装 Python 了）：
    pyinstaller -F -w mcget_gui.py

线程模型（很重要，改代码时别破坏它）：
    mcget 的下载跑在「工作线程」里，它抛出来的日志和进度事件先进 queue.Queue；
    「界面主线程」用 after() 每 100ms 取一次队列，再去刷新控件。
    工作线程里绝对不能直接碰界面控件 —— Tk 不是线程安全的，会随机崩。
"""

import json
import os
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace

try:
    import customtkinter as ctk
    from tkinter import messagebox
except ModuleNotFoundError as _e:
    sys.stderr.write(
        "缺少依赖：%s\n"
        "先装界面库：pip install customtkinter\n"
        "（Linux 用户还需要系统自带的 tkinter：apt install python3-tk）\n" % _e)
    raise SystemExit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcget  # noqa: E402

POLL_MS = 100                 # 界面刷新间隔
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".mcget_gui.json")
MAX_LOG_LINES = 600           # 日志区最多留多少行，防止下几千个文件时卡界面
PLACEHOLDER = "(正在获取版本列表…)"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("mcget · Minecraft 版本下载器")
        self.geometry("820x760")
        self.minsize(760, 640)

        # ---- 状态 ----
        self.q = queue.Queue()
        self.tracker = mcget.ProgressTracker()
        self.worker = None
        self.busy = False
        self.loading_versions = False
        self._tick_pending = False

        # ---- 变量（界面 <-> 代码 的桥）----
        self.var_version = ctk.StringVar(value="")
        self.var_dir = ctk.StringVar(value=os.path.abspath(".minecraft"))
        self.var_mirror = ctk.BooleanVar(value=True)       # 国内默认开镜像
        self.var_jobs = ctk.IntVar(value=16)
        self.var_extract = ctk.BooleanVar(value=False)
        self.var_snapshot = ctk.BooleanVar(value=False)
        self.var_auto_vanilla = ctk.BooleanVar(value=True)

        # mcget 的输出接到界面上
        mcget.set_log_hook(lambda msg: self.q.put({"type": "log", "text": msg}))
        mcget.set_progress_hook(self._on_progress_event)

        self._build_ui()
        self._load_config()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._poll)          # 启动轮询
        self.after(150, self._refresh_versions)  # 启动后自动拉版本列表

    # ============================================================
    # 界面搭建
    # ============================================================

    # 鼠标悬停时底部提示行显示的说明（说人话，不甩术语）
    HINTS = {
        "version": "要下载哪个 Minecraft 版本。",
        "snapshot": "勾上会多出「快照版」——Mojang 放出来的测试版，可能有 bug，一般不用管。",
        "dir": "游戏文件存到哪。一般就填你 PCL / HMCL 正在用的那个 .minecraft 文件夹。",
        "mirror": "国内连 Mojang 官方很慢，开着走国内公益镜像会快很多。人在国外可以关掉。",
        "jobs": "同时下载几个文件。调大更快，但太大容易被服务器限流，16 左右比较稳。",
        "extract": "把声音、手柄这些系统运行库解出来。多数启动器自己会处理，一般不用勾。",
        "loader": "装 mod 加载器。Fabric / Quilt 装完直接在启动器里选就行；"
                  "Forge / NeoForge 是下载一个安装器，得自己双击装。",
        "ldmc": "装到哪个 MC 版本上。留空就用上面选的那个版本。",
        "ldver": "加载器的版本号。留空自动用最新的稳定版。",
        "auto_vanilla": "Fabric / Quilt 要靠原版文件才能跑。没下过原版时，勾上会自动把原版也下了。",
        "start": "开始下载。下过的文件会自动跳过，中途停了再点一次能接着下。",
        "stop": "停下来。已经下好的文件都留着，没下完的版本不会出现在启动器里。",
        "clean": "扫一遍有没有没下完的版本（比如中途取消留下的空文件夹），可以删掉。",
    }
    DEFAULT_HINT = "把鼠标停在任意选项上，这里会说明它是干什么的。"

    def _bind_hint(self, widget, key):
        widget.bind("<Enter>", lambda _e: self._show_hint(key))
        widget.bind("<Leave>", lambda _e: self._show_hint(None))

    def _show_hint(self, key):
        self.lbl_hint.configure(text=self.HINTS.get(key, self.DEFAULT_HINT))

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)   # 日志区跟着窗口变高

        # ---------- 1. 版本选择 ----------
        f = ctk.CTkFrame(self)
        f.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        f.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(f, text="MC 版本", width=64, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=12)
        self.combo = ctk.CTkComboBox(f, variable=self.var_version, values=[PLACEHOLDER])
        self.combo.grid(row=0, column=1, sticky="ew", padx=6, pady=12)
        self.chk_snapshot = ctk.CTkCheckBox(
            f, text="显示测试版", width=90, variable=self.var_snapshot,
            command=self._refresh_versions)
        self.chk_snapshot.grid(row=0, column=2, padx=6, pady=12)
        self.btn_refresh = ctk.CTkButton(
            f, text="刷新列表", width=90, command=self._refresh_versions)
        self.btn_refresh.grid(row=0, column=3, padx=(6, 12), pady=12)

        # ---------- 2. 存到哪 + 下载设置 ----------
        f2 = ctk.CTkFrame(self)
        f2.grid(row=1, column=0, sticky="ew", padx=14, pady=8)
        f2.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(f2, text="游戏目录", width=64, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=(12, 4))
        self.entry_dir = ctk.CTkEntry(f2, textvariable=self.var_dir)
        self.entry_dir.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6, pady=(12, 4))
        ctk.CTkButton(f2, text="浏览…", width=72, command=self._pick_dir).grid(
            row=0, column=4, padx=(6, 12), pady=(12, 4))

        self.sw_mirror = ctk.CTkSwitch(
            f2, text="国内加速镜像",
            variable=self.var_mirror, onvalue=True, offvalue=False,
            command=self._on_mirror_toggle)
        self.sw_mirror.grid(row=1, column=0, columnspan=2, padx=(12, 6), pady=(4, 8), sticky="w")

        ctk.CTkLabel(f2, text="同时下载", width=72, anchor="e").grid(row=1, column=2, padx=(6, 2))
        self.slider = ctk.CTkSlider(
            f2, from_=1, to=32, number_of_steps=31, command=self._on_jobs)
        self.slider.grid(row=1, column=3, padx=2, sticky="ew")
        self.lbl_jobs = ctk.CTkLabel(f2, text="16 个", width=44, anchor="w")
        self.lbl_jobs.grid(row=1, column=4, padx=(4, 12))

        self.chk_extract = ctk.CTkCheckBox(
            f2, text="下载后解压运行库", variable=self.var_extract)
        self.chk_extract.grid(row=2, column=0, columnspan=3, padx=(12, 6), pady=(0, 12), sticky="w")

        # ---------- 3. mod 加载器 ----------
        f3 = ctk.CTkFrame(self)
        f3.grid(row=2, column=0, sticky="ew", padx=14, pady=8)
        f3.grid_columnconfigure(2, weight=1)
        f3.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(f3, text="加载器", width=64, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=(10, 4), sticky="w")
        self.opt_loader = ctk.CTkOptionMenu(
            f3, width=120, values=["Fabric", "Quilt", "Forge", "NeoForge"],
            command=self._on_loader_change)
        self.opt_loader.grid(row=0, column=1, padx=6, pady=(10, 4))

        self.entry_ld_mc = ctk.CTkEntry(f3, placeholder_text="MC 版本（留空 = 用上面选的）")
        self.entry_ld_mc.grid(row=0, column=2, padx=6, pady=(10, 4), sticky="ew")
        self.entry_ld_ver = ctk.CTkEntry(f3, placeholder_text="留空 = 最新稳定版")
        self.entry_ld_ver.grid(row=0, column=3, padx=6, pady=(10, 4), sticky="ew")

        self.btn_ld_go = ctk.CTkButton(
            f3, text="安装", width=80, command=self._do_loader)
        self.btn_ld_go.grid(row=0, column=4, padx=6, pady=(10, 4))
        self.btn_ld_list = ctk.CTkButton(
            f3, text="查可用版本", width=100, command=self._do_list_loaders)
        self.btn_ld_list.grid(row=0, column=5, padx=(6, 12), pady=(10, 4))

        # Fabric / Quilt 装完是个可启动版本，但它 inheritsFrom 依赖原版
        self.chk_auto_vanilla = ctk.CTkCheckBox(
            f3, text="缺原版时自动补下载", variable=self.var_auto_vanilla)
        self.chk_auto_vanilla.grid(
            row=1, column=0, columnspan=6, padx=(12, 12), pady=(0, 10), sticky="w")
        self.lbl_ld_hint = ctk.CTkLabel(
            f3, text="", anchor="w", justify="left", font=ctk.CTkFont(size=12),
            text_color=("gray30", "gray70"))
        self.lbl_ld_hint.grid(
            row=2, column=0, columnspan=6, sticky="w", padx=12, pady=(0, 10))

        # ---------- 4. 进度 ----------
        f4 = ctk.CTkFrame(self)
        f4.grid(row=3, column=0, sticky="ew", padx=14, pady=8)
        f4.grid_columnconfigure(0, weight=1)

        self.bar = ctk.CTkProgressBar(f4, height=14)
        self.bar.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=(12, 6))
        self.bar.set(0)
        self.lbl_pct = ctk.CTkLabel(f4, text="0%", width=52, anchor="e")
        self.lbl_pct.grid(row=0, column=1, padx=(0, 12), pady=(12, 6))

        self.lbl_stats = ctk.CTkLabel(f4, text="就绪", anchor="w", justify="left")
        self.lbl_stats.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 2))
        self.lbl_phase = ctk.CTkLabel(
            f4, text="", anchor="w", justify="left",
            font=ctk.CTkFont(size=12))
        self.lbl_phase.grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 12))

        # ---------- 5. 日志 ----------
        self.logbox = ctk.CTkTextbox(self, height=180, wrap="word", state="disabled")
        self.logbox.grid(row=4, column=0, sticky="nsew", padx=14, pady=8)

        # ---------- 6. 按钮 ----------
        f5 = ctk.CTkFrame(self, fg_color="transparent")
        f5.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 6))
        f5.grid_columnconfigure(5, weight=1)   # 右侧留白，按钮靠左排

        self.btn_start = ctk.CTkButton(
            f5, text="开始下载", width=120, height=34, command=self._start)
        self.btn_start.grid(row=0, column=0, padx=(0, 6))
        self.btn_stop = ctk.CTkButton(
            f5, text="停止", width=90, height=34, state="disabled",
            fg_color=("#d9534f", "#8a3b3b"), hover_color=("#c9302c", "#a34a4a"),
            command=self._stop)
        self.btn_stop.grid(row=0, column=1, padx=6)
        ctk.CTkButton(f5, text="打开目录", width=90, height=34,
                      command=self._open_dir).grid(row=0, column=2, padx=6)
        self.btn_clean = ctk.CTkButton(
            f5, text="清理残缺版本", width=110, height=34,
            fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"), command=self._do_clean)
        self.btn_clean.grid(row=0, column=3, padx=6)
        ctk.CTkButton(f5, text="清空日志", width=90, height=34,
                      fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"),
                      command=self._clear_log).grid(row=0, column=4, padx=6)

        # ---------- 7. 底部说明行 ----------
        self.lbl_hint = ctk.CTkLabel(
            self, text=self.DEFAULT_HINT, anchor="w", justify="left",
            font=ctk.CTkFont(size=12), text_color=("gray30", "gray70"),
            wraplength=780)
        self.lbl_hint.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 12))

        # 悬停提示：移到选项上，底部就解释它是干什么的
        for w, k in ((self.combo, "version"), (self.chk_snapshot, "snapshot"),
                     (self.entry_dir, "dir"), (self.sw_mirror, "mirror"),
                     (self.slider, "jobs"), (self.chk_extract, "extract"),
                     (self.opt_loader, "loader"), (self.entry_ld_mc, "ldmc"),
                     (self.entry_ld_ver, "ldver"), (self.chk_auto_vanilla, "auto_vanilla"),
                     (self.btn_start, "start"), (self.btn_stop, "stop"),
                     (self.btn_clean, "clean")):
            self._bind_hint(w, k)

        self.slider.set(16)
        self._on_loader_change()   # 初始化加载器提示文案

    # ============================================================
    # 小动作
    # ============================================================

    def _log(self, text):
        """主线程写日志区"""
        try:
            self.logbox.configure(state="normal")
            self.logbox.insert("end", str(text) + "\n")
            # 掐掉太老的日志，免得下载几千个资源文件时界面越来越卡
            lines = int(float(self.logbox.index("end-1c")))
            if lines > MAX_LOG_LINES:
                self.logbox.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
            self.logbox.see("end")
        except Exception:
            pass
        finally:
            try:
                self.logbox.configure(state="disabled")
            except Exception:
                pass

    def _clear_log(self):
        try:
            self.logbox.configure(state="normal")
            self.logbox.delete("1.0", "end")
            self.logbox.configure(state="disabled")
        except Exception:
            pass

    def _pick_dir(self):
        cur = self.var_dir.get().strip() or "."
        path = ctk.filedialog.askdirectory(initialdir=cur if os.path.isdir(cur) else ".")
        if path:
            self.var_dir.set(path)

    def _open_dir(self):
        path = os.path.abspath(self.var_dir.get().strip() or ".minecraft")
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self._log(f"[提示] 打不开目录：{e}")

    def _on_jobs(self, value):
        n = max(1, int(round(float(value))))
        self.var_jobs.set(n)
        self.lbl_jobs.configure(text=f"{n} 个")

    def _on_mirror_toggle(self):
        # 换源后列表也跟着换（镜像源一般更快）
        self._log(f"[设置] 下载源切换到 {'BMCLAPI 镜像' if self.var_mirror.get() else 'Mojang 官方'}")
        if not self.busy:
            self._refresh_versions()

    # ============================================================
    # 版本列表
    # ============================================================

    def _refresh_versions(self):
        if self.loading_versions or self.busy:
            return
        self.loading_versions = True
        self.btn_refresh.configure(state="disabled", text="获取中…")
        include = bool(self.var_snapshot.get())
        mirror = bool(self.var_mirror.get())
        current = self.var_version.get()

        def work():
            mcget.USE_MIRROR = mirror
            ids = mcget.fetch_version_ids(include_snapshots=include)
            self.q.put({"type": "versions", "ids": ids, "current": current})

        threading.Thread(target=work, daemon=True).start()

    # ============================================================
    # 下载主流程
    # ============================================================

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.btn_start.configure(state="disabled" if busy else "normal")
        self.btn_stop.configure(state="normal" if busy else "disabled")
        for w in (self.btn_ld_go, self.btn_ld_list, self.btn_refresh):
            try:
                w.configure(state="disabled" if busy else "normal")
            except Exception:
                pass
        if not busy:
            self.btn_refresh.configure(text="刷新列表")

    def _apply_settings(self):
        mcget.USE_MIRROR = bool(self.var_mirror.get())
        mcget.GAME_DIR = self.var_dir.get().strip() or ".minecraft"
        mcget.JOBS = max(1, min(int(self.var_jobs.get()), 64))

    def _start(self):
        if self.busy:
            return
        version = self.var_version.get().strip()
        if not version or version.startswith("("):
            self._log("[提示] 先选一个 MC 版本（列表还没拉到就点一下「刷新列表」）")
            return

        self._apply_settings()
        mcget.stat.update({"downloaded": 0, "skipped": 0, "failed": 0})
        mcget.reset_cancel()
        self.tracker.reset()
        self.bar.set(0)
        self.lbl_pct.configure(text="0%")

        args = SimpleNamespace(
            version=version, extract_natives=bool(self.var_extract.get()))
        self._set_busy(True)
        self.worker = threading.Thread(target=self._run_download, args=(args,), daemon=True)
        self.worker.start()

    def _run_download(self, args):
        try:
            rc = mcget.cmd_download(args)
        except Exception as e:                      # 兜底：别让异常把界面卡死
            self.q.put({"type": "log", "text": f"[异常] {e}"})
            rc = 1
        self.q.put({"type": "finished", "rc": rc})

    def _stop(self):
        if not self.busy:
            return
        self._log("[停止] 正在收尾 …")
        self._log("      下好的文件会留着，下次接着下；没下完的版本不会出现在启动器里")
        mcget.request_cancel()
        self.btn_stop.configure(state="disabled")

    def _do_clean(self):
        """清理「启动器里看得见、点进去打不开」的残缺版本"""
        if self.busy:
            return
        self._apply_settings()
        self.btn_clean.configure(state="disabled")
        self._log("正在扫描残缺版本 …")

        def work():
            try:
                found = mcget.find_incomplete_versions()
                parts = mcget.find_part_files()
            except Exception as e:
                self.q.put({"type": "log", "text": f"[异常] {e}"})
                found, parts = [], []
            self.q.put({"type": "clean_found", "found": found, "parts": parts})

        threading.Thread(target=work, daemon=True).start()

    def _on_clean_found(self, found, parts=None):
        parts = parts or []
        self.btn_clean.configure(state="normal")
        if not found and not parts:
            self._log("      没有残缺版本，目录是干净的")
            return

        if found:
            self._log(f"      发现 {len(found)} 个残缺版本：")
            for name, reason in found:
                self._log(f"        {name}  —— {reason}")
        if parts:
            self._log(f"      发现 {len(parts)} 个没写完的半成品")
            for p in parts[:5]:
                self._log(f"        {os.path.basename(p)}")

        # 弹窗确认再删，避免误伤
        listing = "\n".join(f"  · {n}（{r}）" for n, r in found[:12])
        if len(found) > 12:
            listing += f"\n  …… 还有 {len(found) - 12} 个"
        if parts:
            listing += f"\n  · {len(parts)} 个没写完的半成品文件"
        has_files = any("残留文件" in r or "缺主 jar" in r for _, r in found)
        extra = ("\n注意：其中有的目录里已经下了一部分文件，会一并删掉，"
                 "重下时会重新下载。\n" if has_files else "")
        total = len(found) + len(parts)
        ok = messagebox.askyesno(
            "清理残缺版本",
            f"要删掉这 {total} 项吗？\n\n{listing}\n"
            f"{extra}\n"
            f"这些都是没下完或缺了依赖的东西，删掉不影响已装好的版本。\n"
            f"要恢复的话重新下载一次即可。")
        if not ok:
            self._log("      已取消，没有删除任何东西")
            return

        removed, _ = mcget.cleanup_incomplete_versions(dry_run=False)
        removed_parts, _ = mcget.cleanup_part_files(dry_run=False)
        self._log(f"      已清理 {removed} 个残缺版本、{removed_parts} 个半成品")

    # ---- 统一后的加载器入口 ----

    LOADER_HINTS = {
        "fabric": "Fabric：装完在启动器里刷新一下就能直接选，不用再自己装。",
        "quilt": "Quilt：和 Fabric 一样，装完直接能选。",
        "forge": "Forge：下载一个安装器文件，要自己双击运行它来安装。",
        "neoforge": "NeoForge：也是下载安装器，双击运行来安装。",
    }

    def _loader_kind(self) -> str:
        return self.opt_loader.get().strip().lower()

    def _on_loader_change(self, _value=None):
        self.lbl_ld_hint.configure(
            text=self.LOADER_HINTS.get(self._loader_kind(), ""))

    def _do_loader(self):
        """点「安装」：MC 版本留空就用主下拉框选中的那个"""
        kind = self._loader_kind()
        mc = self.entry_ld_mc.get().strip() or self.var_version.get().strip()
        ver = self.entry_ld_ver.get().strip()

        if not mc or mc.startswith("("):
            self._log("[提示] 先填 MC 版本，或者在上面的下拉框里选一个")
            return
        if kind in ("forge", "neoforge") and not ver:
            self._log(f"[提示] {kind.capitalize()} 必须填一个版本号，"
                      f"不确定就点旁边的「查可用版本」")
            return
        self._start_loader(kind, mc, ver)

    def _do_list_loaders(self):
        """点「查可用版本」：把该 MC 版本能用的 loader 版本打到日志里"""
        kind = self._loader_kind()
        mc = self.entry_ld_mc.get().strip() or self.var_version.get().strip()
        if not mc or mc.startswith("("):
            self._log("[提示] 先填 MC 版本")
            return
        if kind in ("forge", "neoforge"):
            self._log(f"[提示] {kind.capitalize()} 没有公开的版本列表接口，"
                      f"去 https://files.minecraftforge.net 或 BMCLAPI 查")
            return

        self.btn_ld_list.configure(state="disabled")
        self._log(f"正在查 MC {mc} 可用的 {kind} 版本 ...")
        mirror = bool(self.var_mirror.get())

        def work():
            mcget.USE_MIRROR = mirror
            versions = mcget.fetch_loader_versions(kind, mc)
            self.q.put({"type": "loader_list", "kind": kind, "mc": mc,
                        "versions": versions})

        threading.Thread(target=work, daemon=True).start()

    def _start_loader(self, kind, mc, ver):
        if self.busy:
            return
        self._apply_settings()
        if kind in ("fabric", "quilt"):
            pass                       # 这两类有官方源，尊重镜像开关
        else:
            mcget.USE_MIRROR = True    # Forge/NeoForge 安装器只有 BMCLAPI 这一条路
        mcget.reset_cancel()
        self.tracker.reset()
        auto_vanilla = bool(self.var_auto_vanilla.get())
        self._set_busy(True)

        def work():
            try:
                if kind == "forge":
                    self.tracker.on_event(
                        {"type": "plan", "total_files": 1, "total_bytes": 0})
                    ok = mcget.download_forge_installer(mc, ver)
                elif kind == "neoforge":
                    self.tracker.on_event(
                        {"type": "plan", "total_files": 1, "total_bytes": 0})
                    ok = mcget.download_neoforge_installer(mc, ver or None)
                else:
                    ok = mcget.install_mod_loader(
                        kind, mc, ver or None, auto_vanilla=auto_vanilla)
            except Exception as e:
                self.q.put({"type": "log", "text": f"[异常] {e}"})
                ok = False
            self.q.put({"type": "finished", "rc": 0 if ok else 1})

        threading.Thread(target=work, daemon=True).start()

    # ============================================================
    # 从工作线程回来的一切都走这里（主线程）
    # ============================================================

    def _on_progress_event(self, ev):
        """
        在下载线程里被调用：只更新 tracker，再丢一个空事件催界面刷新。
        「bytes」事件一秒能来几千个，所以用一个标志位合并掉，
        否则队列会被刷爆、界面反而卡住。
        """
        self.tracker.on_event(ev)
        if not self._tick_pending:
            self._tick_pending = True
            self.q.put({"type": "tick"})

    def _poll(self):
        self._tick_pending = False
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if self.busy:
            self._update_progress()
        self.after(POLL_MS, self._poll)

    def _handle(self, item):
        t = item.get("type")
        if t == "log":
            self._log(item.get("text", ""))
        elif t == "versions":
            self._on_versions(item.get("ids"), item.get("current", ""))
        elif t == "loader_list":
            self._on_loader_list(item.get("kind"), item.get("mc"), item.get("versions"))
        elif t == "clean_found":
            self._on_clean_found(item.get("found") or [], item.get("parts") or [])
        elif t == "finished":
            self._on_finished(item.get("rc", 0))
        # "tick" 不需要处理，_poll 里统一刷新

    def _on_loader_list(self, kind, mc, versions):
        self.btn_ld_list.configure(state="normal")
        if not versions:
            self._log(f"[失败] 没查到 MC {mc} 可用的 {kind} 版本，检查版本号")
            return
        show = versions[:10]
        self._log(f"MC {mc} 的 {kind} 版本（共 {len(versions)} 个，最新 10 个）：")
        self._log("  " + " · ".join(show))
        self._log(f"  版本框留空就装最新的：{versions[0]}")

    def _on_versions(self, ids, current):
        self.loading_versions = False
        self.btn_refresh.configure(state="normal", text="刷新列表")
        if not ids:
            self.combo.configure(values=[PLACEHOLDER])
            self._log("[错误] 版本列表没拉到。检查网络，或者打开「BMCLAPI 镜像」再刷新一次")
            return
        self.combo.configure(values=ids)
        self.var_version.set(current if current in ids else ids[0])
        self._log(f"[就绪] 共 {len(ids)} 个版本，最新是 {ids[0]}")

    def _on_finished(self, rc):
        self._set_busy(False)
        snap = self.tracker.snapshot()
        if rc == 0:
            self.bar.set(1.0)
            self.lbl_pct.configure(text="100%")
        self.lbl_stats.configure(
            text=f"新下载 {mcget.stat['downloaded']} · 跳过 {mcget.stat['skipped']} "
                 f"· 失败 {mcget.stat['failed']}"
                 + ("（已取消）" if mcget.is_cancelled() else ""))
        self.lbl_phase.configure(text=snap["phase"] or "完成")
        mcget.reset_cancel()

    def _update_progress(self):
        s = self.tracker.snapshot()
        self.bar.set(s["percent"])
        self.lbl_pct.configure(text=f"{int(s['percent'] * 100)}%")
        self.lbl_stats.configure(
            text=f"文件 {s['done_files']}/{s['total_files']}"
                 f" · {mcget.fmt_bytes(s['done_bytes'])} / {mcget.fmt_bytes(s['total_bytes'])}"
                 f" · {mcget.fmt_speed(s['speed_bps'])}"
                 f" · 剩余 {mcget.fmt_eta(s['eta_seconds'])}")
        phase = s["phase"] or ""
        cur = s["current_file"]
        if cur:
            phase = f"{phase} · {cur}" if phase else cur
        self.lbl_phase.configure(text=phase)

    # ============================================================
    # 记住上次的设置
    # ============================================================

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.var_dir.set(d.get("dir", self.var_dir.get()))
            self.var_mirror.set(bool(d.get("mirror", True)))
            self.var_jobs.set(max(1, int(d.get("jobs", 16))))
            self.var_extract.set(bool(d.get("extract", False)))
            self.var_snapshot.set(bool(d.get("snapshot", False)))
            self.var_auto_vanilla.set(bool(d.get("auto_vanilla", True)))
            if d.get("loader") in ("Fabric", "Quilt", "Forge", "NeoForge"):
                self.opt_loader.set(d["loader"])
            if d.get("version"):
                self.var_version.set(d["version"])
        except Exception:
            pass
        self.slider.set(self.var_jobs.get())
        self.lbl_jobs.configure(text=f"{self.var_jobs.get()} 个")
        self._on_loader_change()

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "dir": self.var_dir.get(),
                    "mirror": bool(self.var_mirror.get()),
                    "jobs": int(self.var_jobs.get()),
                    "extract": bool(self.var_extract.get()),
                    "snapshot": bool(self.var_snapshot.get()),
                    "version": self.var_version.get(),
                    "loader": self.opt_loader.get(),
                    "auto_vanilla": bool(self.var_auto_vanilla.get()),
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _on_close(self):
        # 正在下载时直接关窗口会把工作线程一起带走：
        # 已经下好的文件不受影响，但正在写的那个文件会留下个 .part 半成品。
        # 所以先问一句，让「停止」有机会走优雅收尾那条路。
        if self.busy:
            ok = messagebox.askyesno(
                "正在下载",
                "下载还没结束，现在关闭的话会中断它。\n\n"
                "已经下好的文件都会保留，下次接着下；\n"
                "但正在写的那个文件会留下一个半成品，需要下次重下。\n\n"
                "建议先点「停止」再关闭。确定要直接关吗？")
            if not ok:
                return
        self._save_config()
        mcget.request_cancel()
        self.destroy()


def main():
    ctk.set_appearance_mode("light")     # 浅色主题
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
