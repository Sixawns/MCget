#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcget —— Minecraft 版本下载器（自用 / 纯下载，不含启动功能）

只用 Python 标准库，不需要 pip install 任何东西。
把某个 MC 版本的完整文件拉到本地，之后交给 PCL / HMCL 等启动器启动。

命令行用法：
    python mcget.py list
    python mcget.py download 1.20.1 --mirror
    python mcget.py download 1.21.1 --mirror --dir D:/Minecraft/.minecraft
    python mcget.py forge 1.20.1 47.4.13
    python mcget.py neoforge 1.21.1
    python mcget.py fabric 1.20.1              # Fabric，loader 留空 = 最新稳定版
    python mcget.py fabric 1.20.1 0.15.11
    python mcget.py quilt 1.20.1               # Quilt 同理

图形界面用法（需 pip install customtkinter）：
    python mcget_gui.py

给 GUI 用的扩展点（CLI 完全不依赖它们）：
    set_log_hook(fn)        接管日志输出
    set_progress_hook(fn)   接管进度事件（GUI 拿它画进度条/速度）
    ProgressTracker         把事件聚合成「百分比 / 速度 / 剩余时间」
    request_cancel()        请求中断当前下载
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace

# ============================================================
# 常量区：官方源 与 BMCLAPI 国内镜像 的对应关系
# ============================================================

OFFICIAL_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
BMCLAPI_MANIFEST = "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json"

OFFICIAL_LIB = "https://libraries.minecraft.net/"
BMCLAPI_LIB = "https://bmclapi2.bangbang93.com/maven/"

OFFICIAL_ASSET = "https://resources.download.minecraft.net/"
BMCLAPI_ASSET = "https://bmclapi2.bangbang93.com/assets/"

# Fabric / Quilt 的元数据服务（mod loader 的版本清单和 profile 都从这里拿）
FABRIC_META = "https://meta.fabricmc.net"
BMCLAPI_FABRIC_META = "https://bmclapi2.bangbang93.com/fabric-meta"
QUILT_META = "https://meta.quiltmc.org"
BMCLAPI_QUILT_META = "https://bmclapi2.bangbang93.com/quilt-meta"

# 第三方 Maven 仓库 -> BMCLAPI 镜像。
# Fabric/Quilt 的库都放在 maven.fabricmc.net / maven.quiltmc.org 上，
# 国内直连慢，所以一并换掉（BMCLAPI 的 /maven 是这些仓库的合流）。
MAVEN_MIRRORS = {
    "https://maven.fabricmc.net/": BMCLAPI_LIB,
    "https://maven.quiltmc.org/repository/release/": BMCLAPI_LIB,
    "https://maven.quiltmc.org/repository/snapshot/": BMCLAPI_LIB,
    "https://maven.minecraftforge.net/": BMCLAPI_LIB,
    "https://libraries.minecraft.net/": BMCLAPI_LIB,
}

USER_AGENT = "mcget/1.0 (self-use downloader)"

CHUNK = 64 * 1024  # 下载分块大小：既能算进度/速度，又不至于太多事件

# 全局状态
USE_MIRROR = False
GAME_DIR = ".minecraft"
JOBS = 16

stat = {"downloaded": 0, "skipped": 0, "failed": 0}

# GUI / CLI 注入的回调（默认 None，谁也不通知）
_log_hook = None
_progress_hook = None
_cancel = threading.Event()


# ============================================================
# 基础工具
# ============================================================

def log(msg):
    """输出一行日志。GUI 会用 set_log_hook 把它接走"""
    if _log_hook is not None:
        try:
            _log_hook(str(msg))
        except Exception:
            pass
    print(msg, flush=True)


def set_log_hook(fn):
    """GUI 注入日志接管函数；传 None 恢复默认打印"""
    global _log_hook
    _log_hook = fn


def set_progress_hook(fn):
    """
    GUI 注入进度接管函数。

    事件是一个 dict，目前有以下几种：
      {"type": "phase",      "name": "下载中"}
      {"type": "plan",       "total_files": 1234, "total_bytes": 123456789}
      {"type": "file_start", "label": "client.jar", "size": 37000000}
      {"type": "bytes",      "delta": 65536}                # 真实网络字节数
      {"type": "file_done",  "label": "...", "size": N, "ok": True, "skipped": False}
    注意：这个回调是在下载线程里被调用的，不要直接在里面操作界面控件，
    放进队列、由界面主线程取（mcget_gui.py 里就是这么做的）。
    """
    global _progress_hook
    _progress_hook = fn


def _emit(event: dict):
    fn = _progress_hook
    if fn is not None:
        try:
            fn(event)
        except Exception:
            pass


def request_cancel():
    """请求中断：正在下载的块读完就停，未开始的任务直接跳过"""
    _cancel.set()


def reset_cancel():
    _cancel.clear()


def is_cancelled() -> bool:
    return _cancel.is_set()


def fmt_bytes(n) -> str:
    """12345678 -> '11.8 MB'"""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_speed(bps) -> str:
    """每秒字节数 -> '2.4 MB/s'"""
    try:
        bps = float(bps or 0)
    except (TypeError, ValueError):
        return "0 B/s"
    if bps <= 0:
        return "0 B/s"
    return fmt_bytes(bps) + "/s"


def fmt_eta(seconds) -> str:
    """剩余秒数 -> '1 分 23 秒'；算不出来就返回 '--'"""
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN 也算不出来
        return "--"
    if seconds < 1:
        return "不到 1 秒"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m} 分 {s:02d} 秒" if m else f"{s} 秒"
    h, m = divmod(m, 60)
    return f"{h} 时 {m:02d} 分"


def to_mirror(url: str) -> str:
    """把官方地址换成 BMCLAPI 镜像地址；本来就不是官方地址则原样返回"""
    if not USE_MIRROR:
        return url
    if url.startswith(OFFICIAL_LIB):
        return BMCLAPI_LIB + url[len(OFFICIAL_LIB):]
    if url.startswith(OFFICIAL_ASSET):
        return BMCLAPI_ASSET + url[len(OFFICIAL_ASSET):]
    if url.startswith("https://piston-meta.mojang.com/"):
        return BMCLAPI_MANIFEST
    # Fabric / Quilt / Forge 的 Maven 仓库
    for official, mirror in MAVEN_MIRRORS.items():
        if url.startswith(official):
            return mirror + url[len(official):]
    return url


def http_get_json(url: str, retries: int = 3):
    """下载一个 URL 并解析成 JSON，失败自动重试"""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == retries - 1:
                log(f"    [错误] 请求失败 {url} : {e}")
                return None
            time.sleep(1.5)
    return None


def sha1_of_file(path: str) -> str:
    """算文件的 SHA1，返回 40 位小写十六进制"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: str, expect_sha1: str = None, label: str = None, size: int = None) -> bool:
    """
    下载一个文件。
    如果文件已存在且 SHA1 正确 -> 直接跳过（这是续传的关键）
    下载完会校验 SHA1，不对就删掉重来（最多重试 3 次）
    size 只是清单里记录的大小，用来算总进度，不影响下载本身
    """
    name = label or os.path.basename(dest) or url

    if is_cancelled():
        return False

    # 已存在且校验通过 -> 跳过
    if expect_sha1 and os.path.isfile(dest):
        try:
            if sha1_of_file(dest) == expect_sha1.lower():
                stat["skipped"] += 1
                got = size if size else (os.path.getsize(dest) if os.path.isfile(dest) else 0)
                _emit({"type": "file_done", "label": name, "size": got or 0,
                       "ok": True, "skipped": True})
                return True
        except OSError:
            pass

    # 注意：这里刻意不提前建目录。
    # 以前是开工就 makedirs，结果一取消就留下一个空的 versions/<id>/ 空壳，
    # 启动器里看不到版本、资源管理器里却多一堆文件夹。
    # 现在改成「确认要写盘时才建目录」，取消/失败就一个目录都不会留下。
    _emit({"type": "file_start", "label": name, "size": size or 0})

    for attempt in range(3):
        try:
            if is_cancelled():
                return False

            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                # 分块读：一边读一边报进度，这样界面上的百分比和速度才是连续的
                buf = bytearray()
                net = 0
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    net += len(chunk)
                    _emit({"type": "bytes", "delta": len(chunk)})
                    if is_cancelled():
                        return False
                data = bytes(buf)

            # 校验通过了才写盘，避免留下坏文件
            if expect_sha1:
                actual = hashlib.sha1(data).hexdigest()
                if actual != expect_sha1.lower():
                    raise ValueError(f"SHA1 不匹配 期望={expect_sha1} 实际={actual}")

            atomic_write(dest, data)

            stat["downloaded"] += 1
            _emit({"type": "file_done", "label": name, "size": len(data),
                   "ok": True, "skipped": False})
            return True

        except Exception as e:
            if is_cancelled():
                return False
            if attempt == 2:
                log(f"    [失败] {name} : {e}")
                stat["failed"] += 1
                _emit({"type": "file_done", "label": name, "size": 0,
                       "ok": False, "skipped": False})
                return False
            time.sleep(1.0)

    return False


def atomic_write(dest: str, data: bytes):
    """
    原子写盘：先写到 <dest>.part，写完整了再 os.replace 成正式名字。

    为什么非要绕这一步：直接 open(dest,"wb") 写的话，写盘途中进程被强杀
    （关窗口、任务管理器结束进程、断电），磁盘上就会留下一个**文件名正确、
    内容却只有一半**的文件。启动器和 mcget 自己都会把它当成完整文件看待。

    用 .part 中转后，同样的中断只会留下一个 .part 垃圾 ——
    不会被任何东西误认成正式文件，下次下载直接覆盖它。

    os.replace 在同一分区内是原子操作（要么看到旧文件，要么看到新文件，
    不存在中间态），而且它会覆盖已存在的目标文件，Windows / Linux 上都可用。

    正常失败（异常）时这里会主动删掉 .part，不留垃圾；
    只有进程被强杀、Python 来不及执行清理时才会残留，那种情况由 clean 收拾。
    """
    tmp = dest + ".part"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def find_part_files():
    """
    找出所有 .part 残留 —— 也就是「写盘写到一半进程没了」留下的半成品。
    返回绝对路径列表。
    """
    parts = []
    for sub in ("versions", "libraries", "assets", "installers"):
        base = os.path.join(GAME_DIR, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if fn.endswith(".part"):
                    parts.append(os.path.join(dirpath, fn))
    return parts


def cleanup_part_files(dry_run: bool = False):
    """删掉 .part 残留。返回 (删掉的数量, 路径列表)"""
    parts = find_part_files()
    if not parts:
        return 0, []
    removed = 0
    for p in parts:
        if dry_run:
            continue
        try:
            os.remove(p)
            removed += 1
        except OSError as e:
            log(f"    [警告] 删不掉 {p} : {e}")
    return removed, parts


def fetch_many(tasks):
    """
    并发下载（多个文件同时下）。
    tasks: [(url, dest, expect_sha1, label, size), ...]  （size 允许为 None）
    """
    if not tasks:
        return
    norm = []
    for t in tasks:
        t = tuple(t) + (None,) * (5 - len(t))
        norm.append(t[:5])

    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        futures = [pool.submit(fetch, *t) for t in norm]
        for _ in as_completed(futures):
            pass


# ============================================================
# 进度聚合：把下载线程抛出的事件算成人能看懂的百分比/速度/剩余时间
# ============================================================

class ProgressTracker:
    """
    线程安全。用法：

        tracker = ProgressTracker()
        mcget.set_progress_hook(tracker.on_event)   # 下载线程里被调用
        ...
        snap = tracker.snapshot()   # 界面主线程里定时取一次
        snap["percent"] / snap["speed_bps"] / snap["eta_seconds"]

    进度按字节算（total_bytes 来自清单里记录的大小）；
    速度只按「真实网络字节」算，所以第二次运行大量跳过时不会虚报几百 MB/s。
    """

    def __init__(self, window: float = 5.0):
        self._lock = threading.Lock()
        self._window = window
        self.reset()

    def reset(self):
        with self._lock:
            self.phase = ""
            self.total_files = 0
            self.done_files = 0
            self.failed_files = 0
            self.total_bytes = 0
            self.done_bytes = 0       # 含跳过的（用来算百分比）
            self.net_bytes = 0        # 只算真下载的（用来算速度）
            self.current_file = ""
            self.finished = False
            self._samples = deque()   # (时间戳, net_bytes)

    # ---- 事件入口（下载线程调用） ----
    def on_event(self, ev: dict):
        if not ev:
            return
        t = ev.get("type")
        now = time.time()
        with self._lock:
            if t == "plan":
                self.total_files = int(ev.get("total_files") or 0)
                self.total_bytes = int(ev.get("total_bytes") or 0)
            elif t == "phase":
                self.phase = ev.get("name") or ""
            elif t == "file_start":
                self.current_file = ev.get("label") or ""
            elif t == "bytes":
                d = int(ev.get("delta") or 0)
                self.net_bytes += d
                self.done_bytes += d
                self._samples.append((now, self.net_bytes))
                self._trim(now)
            elif t == "file_done":
                self.done_files += 1
                if not ev.get("ok", True):
                    self.failed_files += 1
                if ev.get("skipped"):
                    # 跳过的文件没有网络流量，但进度要算它
                    self.done_bytes += int(ev.get("size") or 0)
                self.current_file = ""
            elif t == "done":
                self.finished = True

    def _trim(self, now):
        limit = now - self._window
        while self._samples and self._samples[0][0] < limit:
            self._samples.popleft()

    # ---- 读取（界面主线程调用） ----
    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._trim(now)
            speed = 0.0
            if len(self._samples) >= 2:
                t0, b0 = self._samples[0]
                t1, b1 = self._samples[-1]
                dt = t1 - t0
                if dt >= 0.3:
                    speed = max(0.0, (b1 - b0) / dt)

            if self.total_bytes > 0:
                percent = min(1.0, self.done_bytes / self.total_bytes)
            elif self.total_files > 0:
                percent = min(1.0, self.done_files / self.total_files)
            else:
                percent = 0.0

            remain = max(0, self.total_bytes - self.done_bytes)
            eta = (remain / speed) if (speed > 0 and remain > 0) else (0.0 if remain == 0 else None)

            return {
                "phase": self.phase,
                "percent": percent,
                "total_files": self.total_files,
                "done_files": self.done_files,
                "failed_files": self.failed_files,
                "total_bytes": self.total_bytes,
                "done_bytes": self.done_bytes,
                "net_bytes": self.net_bytes,
                "speed_bps": speed,
                "eta_seconds": eta,
                "current_file": self.current_file,
                "finished": self.finished,
            }


# ============================================================
# 命令：列出所有正式版
# ============================================================

def fetch_manifest():
    """拿版本清单；镜像开关由 USE_MIRROR 决定"""
    url = BMCLAPI_MANIFEST if USE_MIRROR else OFFICIAL_MANIFEST
    return http_get_json(url)


def fetch_version_ids(include_snapshots: bool = False):
    """
    返回版本号列表（新的在前）。失败返回 None。
    GUI 的下拉框就用的这个。
    """
    data = fetch_manifest()
    if not data:
        return None
    if include_snapshots:
        return [v["id"] for v in data.get("versions", []) if v.get("id")]
    return [v["id"] for v in data.get("versions", [])
            if v.get("type") == "release" and v.get("id")]


def cmd_list(_args):
    data = fetch_manifest()
    if not data:
        log("[提示] 官方源访问不了的话，加 --mirror 走国内镜像")
        return 1

    latest = data.get("latest", {}).get("release")
    if latest:
        log(f"最新正式版: {latest}\n")

    releases = [v["id"] for v in data.get("versions", []) if v.get("type") == "release"]
    for i in range(0, len(releases), 8):
        log("\t".join(releases[i:i + 8]))
    log(f"\n共 {len(releases)} 个正式版")
    return 0


# ============================================================
# 命令：下载一个完整版本
# ============================================================

def library_applies(lib: dict) -> bool:
    """
    判断这个 library 是否适用于 Windows。
    rules 规则：action=allow 且 os.name 匹配 -> 要；action=disallow 且匹配 -> 不要
    没有 rules 表示全平台都需要。
    """
    rules = lib.get("rules")
    if not rules:
        return True

    allowed = False
    for rule in rules:
        action = rule.get("action")
        os_info = rule.get("os") or {}
        os_name = os_info.get("name")
        match = True if os_name is None else (os_name == "windows")

        if action == "allow" and match:
            allowed = True
        elif action == "disallow" and match:
            allowed = False
    return allowed


def collect_libraries(version: dict):
    """收集所有需要下载的库（含 Windows 原生库）"""
    tasks = []
    for lib in version.get("libraries", []):
        if not library_applies(lib):
            continue
        downloads = lib.get("downloads") or {}

        # 主 jar
        artifact = downloads.get("artifact")
        if artifact and artifact.get("url"):
            tasks.append((
                to_mirror(artifact["url"]),
                os.path.join(GAME_DIR, "libraries", artifact["path"]),
                artifact.get("sha1"),
                artifact["path"],
                artifact.get("size"),
            ))

        # Windows 原生库
        natives = downloads.get("classifiers") or {}
        win = natives.get("natives-windows")
        if win and win.get("url"):
            tasks.append((
                to_mirror(win["url"]),
                os.path.join(GAME_DIR, "libraries", win["path"]),
                win.get("sha1"),
                win["path"],
                win.get("size"),
            ))
    return tasks


def collect_assets(version: dict):
    """收集所有资源文件（贴图、音效、语言文件）"""
    asset_index = version.get("assetIndex") or {}
    index_url = asset_index.get("url")
    index_id = asset_index.get("id")
    if not index_url or not index_id:
        return []

    index_data = http_get_json(to_mirror(index_url))
    if not index_data:
        log("[错误] 资源索引下载失败")
        return []

    # 保存索引文件，启动器会读它
    idx_path = os.path.join(GAME_DIR, "assets", "indexes", f"{index_id}.json")
    os.makedirs(os.path.dirname(idx_path), exist_ok=True)
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f)

    objects = index_data.get("objects") or {}
    asset_root = BMCLAPI_ASSET if USE_MIRROR else OFFICIAL_ASSET

    tasks = []
    for _name, obj in objects.items():
        h = obj.get("hash")
        if not h:
            continue
        # 资源 URL 规则：<root>/<hash前2位>/<完整hash>
        url = f"{asset_root}{h[:2]}/{h}"
        dest = os.path.join(GAME_DIR, "assets", "objects", h[:2], h)
        tasks.append((url, dest, h, h[:8], obj.get("size")))
    return tasks


def collect_all(version: dict, version_id: str):
    """客户端主 jar + 依赖库 + 资源文件，一次性收齐，好在开下之前算出总量"""
    tasks = []
    client = (version.get("downloads") or {}).get("client") or {}
    if client.get("url"):
        tasks.append((
            to_mirror(client["url"]),
            os.path.join(GAME_DIR, "versions", version_id, f"{version_id}.jar"),
            client.get("sha1"),
            "client.jar",
            client.get("size"),
        ))
    tasks += collect_libraries(version)
    tasks += collect_assets(version)
    return tasks


def extract_natives(version: dict, version_id: str):
    """解压 Windows 原生库"""
    import zipfile
    native_dir = os.path.join(GAME_DIR, "versions", version_id, f"{version_id}-natives")
    os.makedirs(native_dir, exist_ok=True)
    count = 0
    for lib in version.get("libraries", []):
        if not library_applies(lib):
            continue
        win = (lib.get("downloads") or {}).get("classifiers", {}).get("natives-windows")
        if not win:
            continue
        jar = os.path.join(GAME_DIR, "libraries", win["path"])
        if not os.path.isfile(jar):
            continue
        exclude = set((lib.get("extract") or {}).get("exclude") or [])
        try:
            with zipfile.ZipFile(jar) as z:
                for item in z.namelist():
                    if item.endswith("/"):
                        continue
                    if any(item.startswith(e) for e in exclude):
                        continue
                    z.extract(item, native_dir)
                    count += 1
        except Exception as e:
            log(f"    [警告] 解压失败 {win['path']} : {e}")
    log(f"      已解压 {count} 个文件到 {native_dir}")


def cmd_download(args):
    version_id = args.version
    extract = bool(getattr(args, "extract_natives", False))

    log(f"游戏目录: {os.path.abspath(GAME_DIR)}")
    log(f"下载源  : {'BMCLAPI 镜像（国内）' if USE_MIRROR else 'Mojang 官方'}")
    log(f"同时下载: {JOBS} 个\n")

    # 1) 版本清单
    _emit({"type": "phase", "name": "获取版本清单"})
    manifest = fetch_manifest()
    if not manifest:
        log("[提示] 试试加 --mirror")
        return 1

    version_url = None
    for v in manifest.get("versions", []):
        if v.get("id") == version_id:
            version_url = v.get("url")
            break
    if not version_url:
        log(f"[错误] 找不到版本 {version_id}，先用 mcget.py list 看看有哪些版本")
        return 1

    # 2) 该版本的详细 JSON
    log(f"正在获取 {version_id} 的版本信息 ...")
    version = http_get_json(to_mirror(version_url))
    if not version:
        return 1

    vjson_path = os.path.join(GAME_DIR, "versions", version_id, f"{version_id}.json")

    # 3) 先把全部要下的东西收齐，这样进度条一开始就有总量
    _emit({"type": "phase", "name": "收集文件清单"})
    tasks = collect_all(version, version_id)
    total_bytes = sum(int(t[4] or 0) for t in tasks)
    log(f"[1/3] 共 {len(tasks)} 个文件，约 {fmt_bytes(total_bytes)}")
    _emit({"type": "plan", "total_files": len(tasks), "total_bytes": total_bytes})

    # 4) 并发下载（client / 库 / 资源一起跑，谁先就绪谁先下）
    log("[2/3] 下载中（贴图和音效较多，首次较慢，之后自动跳过）...")
    _emit({"type": "phase", "name": "下载中"})
    fetch_many(tasks)

    # 5) 可选：解压原生库
    if extract and not is_cancelled():
        log("\n[3/3] 解压 Windows 原生库 ...")
        _emit({"type": "phase", "name": "解压原生库"})
        extract_natives(version, version_id)

    _emit({"type": "phase", "name": "完成"})
    _emit({"type": "done"})

    log("\n========== 完成 ==========")
    log(f"新下载: {stat['downloaded']}    跳过(已存在): {stat['skipped']}    失败: {stat['failed']}")

    # 6) 版本 json 最后才写。
    #    它是启动器「认出这个版本」的唯一凭据 —— 提前写的话，
    #    中途取消或崩溃就会留下一个能看见、却打不开的残缺版本。
    #    放到最后写，没下完就根本不会出现这个版本，也不会有残留可清。
    if is_cancelled():
        remove_empty_version_dir(version_id)
        log("已取消。已经下好的文件都留着，重新运行即可接着下")
        return 1

    if stat["failed"]:
        remove_empty_version_dir(version_id)
        log(f"有 {stat['failed']} 个文件失败，重新运行同一条命令即可续传")
        return 1

    os.makedirs(os.path.dirname(vjson_path), exist_ok=True)
    with open(vjson_path, "w", encoding="utf-8") as f:
        json.dump(version, f)

    log(f"版本 {version_id} 已完整下载到 {os.path.abspath(GAME_DIR)}")
    log("提示: 本工具只负责下载，启动请用 PCL / HMCL 等启动器")
    return 0


# ============================================================
# 命令：Forge / NeoForge 安装器
# ============================================================

def download_forge_installer(mc: str, forge: str) -> bool:
    """下载 Forge 安装器 jar，返回是否成功"""
    url = ("https://bmclapi2.bangbang93.com/forge/download"
           f"?mcversion={mc}&version={forge}&category=installer&format=jar")
    dest = os.path.join(GAME_DIR, "installers", f"forge-{mc}-{forge}-installer.jar")
    log(f"下载 Forge 安装器 -> {dest}")
    ok = fetch(url, dest, None, f"forge-{mc}-{forge}")
    if ok:
        log("完成。双击这个 jar 即可安装 Forge")
    else:
        log("[失败] 请确认版本号是否存在，例: 1.20.1 / 47.4.13")
    return ok


def fetch_neoforge_list(mc: str):
    """取某个 MC 版本可用的 NeoForge 列表，失败返回 None"""
    return http_get_json(f"https://bmclapi2.bangbang93.com/neoforge/list/{mc}")


def download_neoforge_installer(mc: str, version: str = None) -> bool:
    """下载 NeoForge 安装器；version 留空表示要最新的那个"""
    data = fetch_neoforge_list(mc)
    if not data or not isinstance(data, list) or not data:
        log("[失败] 无法获取 NeoForge 列表，检查 MC 版本号")
        return False

    picked = None
    if version:
        for item in data:
            if str(item.get("version")) == str(version):
                picked = item
                break
        if picked is None:
            log(f"[失败] MC {mc} 没有 NeoForge {version}")
            return False
    else:
        picked = data[0]

    installer_path = picked.get("installerPath")
    if not installer_path:
        log("[失败] 列表里没有 installerPath")
        return False

    ver = picked.get("version", mc)
    url = "https://bmclapi2.bangbang93.com" + installer_path
    dest = os.path.join(GAME_DIR, "installers", f"neoforge-{ver}-installer.jar")
    log(f"下载 NeoForge 安装器 ({ver}) -> {dest}")
    ok = fetch(url, dest, None, f"neoforge-{ver}")
    if ok:
        log(f"完成: {dest}")
    return ok


def cmd_forge(args):
    return 0 if download_forge_installer(args.mcversion, args.forgeversion) else 1


def cmd_neoforge(args):
    return 0 if download_neoforge_installer(args.mcversion) else 1


# ============================================================
# 命令：Fabric / Quilt（直接装成可启动版本，不用跑安装器 jar）
# ============================================================
#
# 为什么 Fabric/Quilt 和 Forge 走两条不同的路：
#   Forge/NeoForge 只有「安装器 jar」这一种分发方式，得双击跑一遍装。
#   Fabric/Quilt 的 meta 服务直接给出一份 launcher profile（内含完整库清单 +
#   mainClass），把这份 json 写进 versions/ 就成了标准的可启动版本 ——
#   PCL / HMCL / 官方启动器刷新一下就能直接选它，比跑安装器干净得多。

def maven_to_path(name: str):
    """
    Maven 坐标 -> 库文件在 libraries/ 下的相对路径。

        net.fabricmc:intermediary:1.20.1
            -> net/fabricmc/intermediary/1.20.1/intermediary-1.20.1.jar
        org.ow2.asm:asm:9.8
            -> org/ow2/asm/asm/9.8/asm-9.8.jar
        net.fabricmc:yarn:1.14+build.7:v2        （带 classifier）
            -> net/fabricmc/yarn/1.14+build.7/yarn-1.14+build.7-v2.jar
    """
    if not name:
        return None
    parts = name.split(":")
    if len(parts) < 3:
        return None
    group, artifact, version = parts[0], parts[1], parts[2]
    base = f"{artifact}-{version}"
    if len(parts) > 3 and parts[3]:        # classifier
        base += f"-{parts[3]}"
    return f"{group.replace('.', '/')}/{artifact}/{version}/{base}.jar"


def loader_meta_base(kind: str) -> str:
    """根据镜像开关返回 Fabric/Quilt meta 的根地址"""
    kind = kind.lower()
    if kind == "fabric":
        return BMCLAPI_FABRIC_META if USE_MIRROR else FABRIC_META
    if kind == "quilt":
        return BMCLAPI_QUILT_META if USE_MIRROR else QUILT_META
    raise ValueError(f"未知的加载器类型：{kind}")


def fetch_loader_versions(kind: str, mc: str):
    """
    取某个 MC 版本可用的 loader 版本列表，只返回版本号字符串数组。
    失败返回 None。
    """
    kind = kind.lower()
    api = "/v2" if kind == "fabric" else "/v3"
    url = f"{loader_meta_base(kind)}{api}/versions/loader/{mc}"
    data = http_get_json(url)
    if not data or not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        loader = item.get("loader") or {}
        v = loader.get("version") or item.get("version")
        if v:
            out.append(v)
    return out


def fetch_loader_profile(kind: str, mc: str, loader: str):
    """拿 launcher profile（内含库清单 + mainClass）"""
    kind = kind.lower()
    api = "/v2" if kind == "fabric" else "/v3"
    url = f"{loader_meta_base(kind)}{api}/versions/loader/{mc}/{loader}/profile/json"
    return http_get_json(url)


def collect_profile_libraries(profile: dict):
    """
    收集 profile 里的库。

    Fabric/Quilt 的库格式跟原版不一样：没有 downloads.artifact，
    只有 name（Maven 坐标）+ url（仓库根），路径要自己拼。
    少数情况下会给 download.artifact（带 path/url/sha1），那种优先用。
    """
    tasks = []
    for lib in profile.get("libraries") or []:
        if not isinstance(lib, dict):
            continue

        url = None
        path = None
        sha1 = lib.get("sha1")
        size = lib.get("size")

        artifact = (lib.get("downloads") or {}).get("artifact")
        if artifact and artifact.get("url"):
            url = artifact["url"]
            path = artifact.get("path")
            sha1 = sha1 or artifact.get("sha1")
            size = size or artifact.get("size")

        if not url:
            base = lib.get("url")
            path = path or maven_to_path(lib.get("name"))
            if base and path:
                url = base.rstrip("/") + "/" + path

        if not url or not path:
            continue

        tasks.append((
            to_mirror(url),
            os.path.join(GAME_DIR, "libraries", path),
            sha1,
            path,
            size,
        ))
    return tasks


def vanilla_version_exists(mc: str) -> bool:
    """原版 json 在不在（Fabric/Quilt 的 profile 靠 inheritsFrom 依赖它）"""
    return os.path.isfile(os.path.join(GAME_DIR, "versions", mc, f"{mc}.json"))


# ============================================================
# 清理残缺版本
# ============================================================
#
# 启动器靠 versions/<id>/<id>.json 认出版本。只要有 json，
# 哪怕里面的 jar 根本没下完，列表里也会显示出来 —— 点了才发现打不开。
# 现在的代码是「全下完才写 json」，但更早的版本是提前写的，
# 所以老用户目录里可能已经留着这种残缺版本。下面这组函数用来清掉它们。

def find_incomplete_versions():
    """
    扫描 versions/ 找出残缺的版本目录。

    判定标准（满足任一即算残缺）：
      1. 目录里根本没有 <id>.json —— 下载中途取消留下的空壳
      2. json 里声明了 client jar，但那个 jar 不存在
      3. 有 inheritsFrom（Fabric/Quilt），但被继承的原版不在了

    返回 [(version_id, 原因), ...]
    """
    root = os.path.join(GAME_DIR, "versions")
    if not os.path.isdir(root):
        return []

    bad = []
    for name in sorted(os.listdir(root)):
        vdir = os.path.join(root, name)
        if not os.path.isdir(vdir):
            continue

        vjson = os.path.join(vdir, f"{name}.json")

        # 情况 1：连 json 都没有 —— 取消留下的空壳，启动器根本认不出它
        if not os.path.isfile(vjson):
            try:
                has_file = any(os.path.isfile(os.path.join(vdir, x))
                               for x in os.listdir(vdir))
            except OSError:
                has_file = True
            bad.append((name, "没下载完（有残留文件）" if has_file else "没下载完（空目录）"))
            continue

        try:
            with open(vjson, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        client = (data.get("downloads") or {}).get("client") or {}
        jar_name = client.get("id") or name      # 有的 json 用 id 指定 jar 名
        jar_path = os.path.join(vdir, f"{jar_name}.jar")
        if client.get("url") and not os.path.isfile(jar_path):
            # Fabric/Quilt 这类靠继承的版本本来就没有自己的 jar，不算残缺
            if not data.get("inheritsFrom"):
                bad.append((name, "缺主 jar"))
                continue

        inherits = data.get("inheritsFrom")
        if inherits and not vanilla_version_exists(inherits):
            bad.append((name, f"缺原版 {inherits}"))
    return bad


def remove_empty_version_dir(version_id: str) -> bool:
    """
    兜底清理：如果 versions/<id>/ 里一个文件都没有，就把它删掉。
    只在目录为空时动手，所以永远不会删掉任何已下载的内容。
    返回是否删掉了。
    """
    vdir = os.path.join(GAME_DIR, "versions", version_id)
    if not os.path.isdir(vdir):
        return False
    try:
        if any(os.path.isfile(os.path.join(vdir, x)) for x in os.listdir(vdir)):
            return False                 # 里面有东西，不动
        os.rmdir(vdir)
        return True
    except OSError:
        return False


def cleanup_incomplete_versions(dry_run: bool = False):
    """
    删掉残缺版本的目录。

    dry_run=True 时只列出不删。
    返回 (清理掉的数量, 说明文本列表)
    """
    bad = find_incomplete_versions()
    if not bad:
        return 0, []

    notes = []
    removed = 0
    for name, reason in bad:
        vdir = os.path.join(GAME_DIR, "versions", name)
        notes.append(f"{name}（{reason}）")
        if dry_run:
            continue
        try:
            shutil.rmtree(vdir)
            removed += 1
        except Exception as e:
            log(f"    [警告] 删不掉 {vdir} : {e}")
    return removed, notes


def cmd_clean(args):
    dry = bool(getattr(args, "dry_run", False))
    bad = find_incomplete_versions()
    parts = find_part_files()

    if not bad and not parts:
        log("没有残缺版本，目录是干净的")
        return 0

    if bad:
        log(f"发现 {len(bad)} 个残缺版本：")
        for name, reason in bad:
            log(f"    {name}  —— {reason}")
    if parts:
        log(f"\n发现 {len(parts)} 个没写完的半成品（.part）：")
        for p in parts[:10]:
            log(f"    {os.path.relpath(p, GAME_DIR)}")
        if len(parts) > 10:
            log(f"    …… 还有 {len(parts) - 10} 个")

    if dry:
        log("\n（--dry-run 只查看，没删。去掉这个参数就会真的清理）")
        return 0

    removed, _ = cleanup_incomplete_versions(dry_run=False)
    removed_parts, _ = cleanup_part_files(dry_run=False)
    log(f"\n已清理 {removed} 个残缺版本、{removed_parts} 个半成品。"
        f"重新下载对应版本即可恢复正常")
    return 0


def install_mod_loader(kind: str, mc: str, loader: str = None,
                       auto_vanilla: bool = False) -> bool:
    """
    装一个 Fabric / Quilt 版本。

    kind    : "fabric" 或 "quilt"
    mc      : MC 版本，如 1.20.1
    loader  : loader 版本，留空 = 挑最新的稳定版
    返回是否成功。
    """
    kind = kind.lower()
    label = "Fabric" if kind == "fabric" else "Quilt"

    # 记下起始的失败数：stat 是全局的，不这么做的话，
    # 上一轮留下的失败数会让这次被误判为失败
    failed_at_start = stat["failed"]

    # 1) 决定用哪个 loader 版本
    if loader:
        chosen = loader
    else:
        versions = fetch_loader_versions(kind, mc)
        if not versions:
            log(f"[失败] 取不到 MC {mc} 可用的 {label} 版本，检查版本号是否正确")
            return False
        # 列表是「新的在前」，优先挑 stable 的；拿不到稳定标记就用第一个
        chosen = versions[0]
        try:
            api = "/v2" if kind == "fabric" else "/v3"
            raw = http_get_json(f"{loader_meta_base(kind)}{api}/versions/loader/{mc}")
            for item in raw or []:
                if isinstance(item, dict) and (item.get("loader") or {}).get("stable"):
                    chosen = (item.get("loader") or {}).get("version") or chosen
                    break
        except Exception:
            pass
        log(f"      未指定版本，选用最新的 {label} {chosen}")

    # 2) 拉 profile
    log(f"正在获取 {label} {chosen} (MC {mc}) 的 profile ...")
    profile = fetch_loader_profile(kind, mc, chosen)
    if not profile or not isinstance(profile, dict):
        log(f"[失败] 拿不到 profile，确认 MC {mc} / {label} {chosen} 是否存在")
        return False

    version_id = profile.get("id") or f"{kind}-loader-{chosen}-{mc}"
    inherits = profile.get("inheritsFrom")

    # 3) 原版得先有，否则启动器认不出这个版本
    if inherits and not vanilla_version_exists(inherits):
        log(f"[提示] 这个版本继承自 {inherits}，但本地还没有 {inherits} 的原版文件")
        if auto_vanilla:
            log(f"      顺手把原版 {inherits} 也下了 ...")
            reset_cancel()
            cmd_download(SimpleNamespace(version=inherits, extract_natives=False))
            if not vanilla_version_exists(inherits):
                log(f"[失败] 原版 {inherits} 没下成，{version_id} 装不完整")
                return False
        else:
            log(f"      请先下载原版 {inherits}（主界面选它下载一次即可），")
            log(f"      或者勾上「顺带下载缺失的原版」再来一次")

    # 4) 下载库
    vdir = os.path.join(GAME_DIR, "versions", version_id)
    vjson = os.path.join(vdir, f"{version_id}.json")
    tasks = collect_profile_libraries(profile)
    log(f"      共 {len(tasks)} 个库文件")
    if tasks:
        total = sum(int(t[4] or 0) for t in tasks)
        _emit({"type": "plan", "total_files": len(tasks), "total_bytes": total})
        _emit({"type": "phase", "name": f"下载 {label} 库"})
        fetch_many(tasks)

    _emit({"type": "phase", "name": "完成"})
    _emit({"type": "done"})

    # 5) 库全下齐了才写 version json。
    #    json 是启动器认出这个版本的唯一凭据，提前写 + 中途取消
    #    = 启动器里出现一个打不开的残缺版本。所以放在最后。
    if is_cancelled():
        remove_empty_version_dir(version_id)
        log(f"[已取消] {label} {version_id} 没装完，没有生成版本文件")
        return False
    new_failed = stat["failed"] - failed_at_start
    if new_failed:
        remove_empty_version_dir(version_id)
        log(f"[未完成] 有 {new_failed} 个库没下成，暂不生成版本文件，重跑一次即可")
        return False

    os.makedirs(vdir, exist_ok=True)
    with open(vjson, "w", encoding="utf-8") as f:
        json.dump(profile, f)
    log(f"      版本文件 -> {vjson}")
    log(f"[完成] {label} 版本 {version_id} 已就绪")
    if inherits:
        log(f"      依赖原版 {inherits}，启动器里选 {version_id} 就能玩")
    return True


def cmd_fabric(args):
    return 0 if install_mod_loader(
        "fabric", args.mcversion, getattr(args, "loaderversion", None),
        auto_vanilla=getattr(args, "auto_vanilla", False)) else 1


def cmd_quilt(args):
    return 0 if install_mod_loader(
        "quilt", args.mcversion, getattr(args, "loaderversion", None),
        auto_vanilla=getattr(args, "auto_vanilla", False)) else 1


# ============================================================
# 命令行下的进度显示（不装 tqdm 也能看到百分比和速度）
# ============================================================

class CliProgress:
    """把进度事件打到终端。是 tty 就用 \\r 刷同一行，不是就每 5% 打一行"""

    def __init__(self):
        self.tracker = ProgressTracker()
        self.last_pct = -1

    def __call__(self, ev):
        self.tracker.on_event(ev)
        snap = self.tracker.snapshot()
        pct = int(snap["percent"] * 100)
        step = 1 if sys.stdout.isatty() else 5
        if pct - self.last_pct < step and not snap["finished"]:
            return
        self.last_pct = pct
        line = (f"\r[{pct:3d}%] {fmt_bytes(snap['done_bytes'])}/{fmt_bytes(snap['total_bytes'])}"
                f" | 文件 {snap['done_files']}/{snap['total_files']}"
                f" | {fmt_speed(snap['speed_bps'])}"
                f" | 剩余 {fmt_eta(snap['eta_seconds'])}      ")
        sys.stdout.write(line)
        sys.stdout.flush()
        if not sys.stdout.isatty():
            sys.stdout.write("\n")


# ============================================================
# 入口
# ============================================================

def build_parser():
    """
    构造命令行解析器。

    关键点：--mirror / --dir / --jobs 这些通用选项要能在子命令后面用，
    比如 `mcget.py list --mirror`。所以单独做成一个 common 解析器，
    再用 parents=[common] 让每个子命令都继承它。
    （只挂在顶层解析器上的话，写在子命令后面会报 unrecognized arguments）
    """
    # 通用选项：同时挂在顶层和每个子命令上，两种写法都能识别
    # 用 SUPPRESS 而不是普通默认值：否则「mcget.py --mirror list」（选项写前面）时，
    # 子命令解析会用默认值把顶层已经解析出来的 True 覆盖回 False。
    # SUPPRESS 的效果是「没写就不碰这个属性」，于是顶层的值能保留下来。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mirror", action="store_true", default=argparse.SUPPRESS,
                        help="走 BMCLAPI 国内镜像（国内建议常加）")
    common.add_argument("--dir", default=argparse.SUPPRESS,
                        help="游戏目录，默认 .minecraft")
    common.add_argument("--jobs", type=int, default=argparse.SUPPRESS,
                        help="同时下载几个文件，默认 16")

    parser = argparse.ArgumentParser(
        parents=[common],
        description="mcget —— Minecraft 版本下载器（自用 / 纯下载）")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", parents=[common], help="列出所有正式版")

    p_clean = sub.add_parser("clean", parents=[common],
                             help="清理残缺版本（看得见但打不开的那种）")
    p_clean.add_argument("--dry-run", action="store_true",
                         help="只列出来看看，不真删")

    p_dl = sub.add_parser("download", parents=[common], help="下载指定版本")
    p_dl.add_argument("version", help="MC 版本号，如 1.20.1")
    p_dl.add_argument("--extract-natives", action="store_true",
                      help="额外解压 Windows 原生库（默认只下载不解压）")
    p_dl.add_argument("--no-progress", action="store_true",
                      help="不显示实时进度（脚本里调用时用）")

    p_forge = sub.add_parser("forge", parents=[common], help="下载 Forge 安装器")
    p_forge.add_argument("mcversion", help="MC 版本，如 1.20.1")
    p_forge.add_argument("forgeversion", help="Forge 版本，如 47.4.13")

    p_neo = sub.add_parser("neoforge", parents=[common], help="下载 NeoForge 安装器")
    p_neo.add_argument("mcversion", help="MC 版本，如 1.21.1")

    # Fabric / Quilt 装完就是一个可启动版本（会写 versions/<id>/<id>.json）
    for name, label in (("fabric", "Fabric"), ("quilt", "Quilt")):
        p = sub.add_parser(name, parents=[common],
                           help=f"安装 {label} 版本（直接可启动）")
        p.add_argument("mcversion", help="MC 版本，如 1.20.1")
        p.add_argument("loaderversion", nargs="?", default=None,
                       help=f"{label} 版本，留空用最新的稳定版")
        p.add_argument("--auto-vanilla", action="store_true",
                       help="原版还没下时，顺手把依赖的原版也下了")

    return parser


def main(argv=None):
    global USE_MIRROR, GAME_DIR, JOBS

    parser = build_parser()
    args = parser.parse_args(argv)

    # 兼容两种写法：`--mirror list` 和 `list --mirror`
    USE_MIRROR = bool(getattr(args, "mirror", False))
    GAME_DIR = getattr(args, "dir", None) or ".minecraft"
    JOBS = max(1, min(getattr(args, "jobs", 16) or 16, 64))

    if not args.cmd:
        parser.print_help()
        return 0

    if args.cmd == "download" and not getattr(args, "no_progress", False) \
            and _progress_hook is None:
        set_progress_hook(CliProgress())

    handlers = {
        "list": cmd_list,
        "download": cmd_download,
        "clean": cmd_clean,
        "forge": cmd_forge,
        "neoforge": cmd_neoforge,
        "fabric": cmd_fabric,
        "quilt": cmd_quilt,
    }
    rc = handlers[args.cmd](args)
    if args.cmd == "download" and sys.stdout.isatty():
        sys.stdout.write("\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
