#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py —— 在本地起一个假 HTTP 服务器，喂给 mcget.py 一份伪造的版本清单，
用来验证「解析 -> 收集任务 -> 并发下载 -> SHA1 校验 -> 跳过 -> 进度/取消」整条链路。

跑法：
    python3 selftest.py

注意：这里只测「看不见的部分」（下载逻辑 + 进度聚合）。
图形界面 mcget_gui.py 需要真 Tk 才能跑起来，本文件对它只做语法与接口引用的静态检查。
"""

import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcget  # noqa: E402

# ---------------- 伪造数据 ----------------

CLIENT_BYTES = b"FAKE-CLIENT-JAR-CONTENT"
LIB_BYTES = b"FAKE-LIBRARY-CONTENT"
NATIVE_BYTES = b"FAKE-NATIVE-CONTENT"
# 资源文件故意搞大一点（约 380KB），这样下载时会真的分好几块，
# 才能验证「分块读 -> bytes 事件 -> 进度/速度」这条路是通的
ASSET_BYTES = b"FAKE-ASSET-PNG-DATA" * 20000


def sha1(b):
    return hashlib.sha1(b).hexdigest()


CLIENT_SHA = sha1(CLIENT_BYTES)
LIB_SHA = sha1(LIB_BYTES)
ASSET_SHA = sha1(ASSET_BYTES)
NATIVE_SHA = sha1(NATIVE_BYTES)

VERSION_ID = "1.20.1"
ASSET_ID = "5"

VERSION_JSON = {
    "id": VERSION_ID,
    "assetIndex": {
        "id": ASSET_ID,
        "sha1": sha1(b"idx"),
        "url": "{BASE}assetindex.json",
    },
    "downloads": {
        "client": {
            "sha1": CLIENT_SHA,
            "size": len(CLIENT_BYTES),
            "url": "{BASE}client.jar",
        }
    },
    "libraries": [
        {
            # 全平台都要的普通库
            "name": "com.example:testlib:1.0",
            "downloads": {
                "artifact": {
                    "path": "com/example/testlib/1.0/testlib-1.0.jar",
                    "sha1": LIB_SHA,
                    "size": len(LIB_BYTES),
                    "url": "{BASE}lib/testlib.jar",
                }
            },
        },
        {
            # 带 rules：只在 windows 上要，且带 natives
            "name": "org.lwjgl:lwjgl:3.3.1",
            "rules": [{"action": "allow", "os": {"name": "windows"}}],
            "downloads": {
                "artifact": {
                    "path": "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1.jar",
                    "sha1": LIB_SHA,
                    "size": len(LIB_BYTES),
                    "url": "{BASE}lib/lwjgl.jar",
                },
                "classifiers": {
                    "natives-windows": {
                        "path": "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1-natives-windows.jar",
                        "sha1": NATIVE_SHA,
                        "size": len(NATIVE_BYTES),
                        "url": "{BASE}lib/lwjgl-native.jar",
                    },
                    "natives-linux": {
                        "path": "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1-natives-linux.jar",
                        "sha1": NATIVE_SHA,
                        "size": len(NATIVE_BYTES),
                        "url": "{BASE}lib/should-not-download.jar",
                    },
                },
            },
        },
        {
            # 明确 disallow windows —— 不应该被下载
            "name": "com.mac:only:1.0",
            "rules": [{"action": "allow"}, {"action": "disallow", "os": {"name": "windows"}}],
            "downloads": {
                "artifact": {
                    "path": "com/mac/only/1.0/only-1.0.jar",
                    "sha1": LIB_SHA,
                    "size": len(LIB_BYTES),
                    "url": "{BASE}lib/maconly.jar",
                }
            },
        },
    ],
}

ASSET_INDEX = {
    "objects": {
        "icons/icon_16x16.png": {"hash": ASSET_SHA, "size": len(ASSET_BYTES)},
    }
}

MANIFEST = {
    "latest": {"release": VERSION_ID, "snapshot": "fake-snapshot"},
    "versions": [
        {"id": VERSION_ID, "type": "release", "url": "{BASE}version.json"},
        {"id": "1.19.4", "type": "release", "url": "/BASE/other.json"},
        {"id": "26w01a", "type": "snapshot", "url": "/BASE/snap.json"},
    ],
}

# 五个文件里只有 4 个会被下载（linux 原生库被平台过滤掉、mac 库被 disallow 掉）
EXPECT_FILES = 5
EXPECT_BYTES = (len(CLIENT_BYTES) + len(LIB_BYTES) * 2
                + len(NATIVE_BYTES) + len(ASSET_BYTES))

ROUTES = {
    "/BASE/client.jar": CLIENT_BYTES,
    "/BASE/lib/testlib.jar": LIB_BYTES,
    "/BASE/lib/lwjgl.jar": LIB_BYTES,
    "/BASE/lib/lwjgl-native.jar": NATIVE_BYTES,
    "/BASE/lib/maconly.jar": LIB_BYTES,
    "/BASE/assets/" + ASSET_SHA[:2] + "/" + ASSET_SHA: ASSET_BYTES,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 这三个 JSON 在请求时才序列化，确保 _fill 注入的 base 生效
        dynamic = {
            "/BASE/manifest.json": MANIFEST,
            "/BASE/version.json": VERSION_JSON,
            "/BASE/assetindex.json": ASSET_INDEX,
        }
        if self.path in dynamic:
            body = json.dumps(dynamic[self.path]).encode()
        elif self.path in ROUTES:
            body = ROUTES[self.path]
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# ---------------- 测试主体 ----------------

def _fill(doc, base):
    """递归把字符串里的 {BASE} 占位符换成真实 base"""
    if isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(v, str):
                doc[k] = v.replace("{BASE}", base)
            else:
                _fill(v, base)
    elif isinstance(doc, list):
        for item in doc:
            _fill(item, base)


def main():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/BASE/"
    _fill(MANIFEST, base)
    _fill(VERSION_JSON, base)

    # 把 mcget 的常量指到本地假服务器
    mcget.OFFICIAL_MANIFEST = base + "manifest.json"
    mcget.OFFICIAL_LIB = base + "lib/"
    mcget.OFFICIAL_ASSET = base + "assets/"
    mcget.USE_MIRROR = False

    tmp = tempfile.mkdtemp(prefix="mcget_test_")
    mcget.GAME_DIR = os.path.join(tmp, ".minecraft")
    mcget.JOBS = 8

    fails = []

    def check(name, cond, extra=""):
        if cond:
            print(f"  [通过] {name}")
        else:
            print(f"  [失败] {name} {extra}")
            fails.append(name)

    def run_download(extract=False):
        return mcget.cmd_download(
            type("A", (), {"version": VERSION_ID, "extract_natives": extract})())

    # 进度事件收集器（GUI 里就是这么接的）
    tracker = mcget.ProgressTracker()
    mcget.set_progress_hook(tracker.on_event)

    print("\n=== 第一轮：完整下载 ===")
    rc = run_download()

    check("返回码为 0", rc == 0)
    check("client.jar 已下载",
          os.path.isfile(os.path.join(mcget.GAME_DIR, "versions", VERSION_ID, f"{VERSION_ID}.jar")))
    check("版本 json 已保存",
          os.path.isfile(os.path.join(mcget.GAME_DIR, "versions", VERSION_ID, f"{VERSION_ID}.json")))
    check("资源索引已保存",
          os.path.isfile(os.path.join(mcget.GAME_DIR, "assets", "indexes", f"{ASSET_ID}.json")))
    check("普通库已下载",
          os.path.isfile(os.path.join(mcget.GAME_DIR, "libraries",
                                      "com/example/testlib/1.0/testlib-1.0.jar")))
    check("windows 原生库已下载",
          os.path.isfile(os.path.join(mcget.GAME_DIR, "libraries",
                                      "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1-natives-windows.jar")))
    check("linux 原生库未下载（平台过滤生效）",
          not os.path.isfile(os.path.join(mcget.GAME_DIR, "libraries",
                                          "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1-natives-linux.jar")))
    check("disallow windows 的库未下载",
          not os.path.isfile(os.path.join(mcget.GAME_DIR, "libraries",
                                          "com/mac/only/1.0/only-1.0.jar")))
    check("资源文件已下载",
          os.path.isfile(os.path.join(mcget.GAME_DIR, "assets", "objects",
                                      ASSET_SHA[:2], ASSET_SHA)))

    first_round = mcget.stat["downloaded"], mcget.stat["skipped"], mcget.stat["failed"]
    print(f"  第一轮统计: 下载={first_round[0]} 跳过={first_round[1]} 失败={first_round[2]}")
    check("第一轮没有失败", first_round[2] == 0)
    check(f"第一轮下载了 {EXPECT_FILES} 个文件", first_round[0] == EXPECT_FILES, f"实际 {first_round[0]}")

    print("\n=== 第一轮进度事件（GUI 就是靠这些画进度条的）===")
    snap = tracker.snapshot()
    print(f"  总量={snap['total_bytes']} 已完成={snap['done_bytes']} "
          f"文件={snap['done_files']}/{snap['total_files']} 百分比={snap['percent']:.0%}")
    check("plan 事件报的文件数正确", snap["total_files"] == EXPECT_FILES, f"实际 {snap['total_files']}")
    check("plan 事件报的总字节数与清单一致", snap["total_bytes"] == EXPECT_BYTES,
          f"实际 {snap['total_bytes']} 期望 {EXPECT_BYTES}")
    check("下载完成后百分比到 100%", snap["percent"] >= 0.999, f"实际 {snap['percent']}")
    check("每个文件都有 file_done 事件", snap["done_files"] == EXPECT_FILES,
          f"实际 {snap['done_files']}")
    check("真实网络字节数被统计到了", snap["net_bytes"] >= len(ASSET_BYTES),
          f"实际 {snap['net_bytes']}")
    check("结束时阶段被标记为完成", snap["phase"] == "完成" and snap["finished"])

    print("\n=== 第二轮：重跑，应当全部跳过（续传）===")
    mcget.stat.update({"downloaded": 0, "skipped": 0, "failed": 0})
    tracker.reset()
    run_download()
    second = mcget.stat["downloaded"], mcget.stat["skipped"], mcget.stat["failed"]
    print(f"  第二轮统计: 下载={second[0]} 跳过={second[1]} 失败={second[2]}")
    check("第二轮全部跳过", second[0] == 0 and second[1] == EXPECT_FILES, f"实际 {second}")
    snap2 = tracker.snapshot()
    check("跳过的文件也算进进度（不会卡在 0%）", snap2["percent"] >= 0.999,
          f"实际 {snap2['percent']}")
    check("跳过不产生网络流量（速度不会被虚报）", snap2["net_bytes"] == 0,
          f"实际 {snap2['net_bytes']}")

    print("\n=== 第三轮：SHA1 故意不匹配时应报错 ===")
    bad = os.path.join(mcget.GAME_DIR, "assets", "objects", ASSET_SHA[:2], ASSET_SHA)
    os.makedirs(os.path.dirname(bad), exist_ok=True)
    with open(bad, "wb") as f:
        f.write(b"CORRUPTED")
    mcget.stat.update({"downloaded": 0, "skipped": 0, "failed": 0})
    run_download()
    third = mcget.stat["downloaded"], mcget.stat["skipped"], mcget.stat["failed"]
    print(f"  第三轮统计: 下载={third[0]} 跳过={third[1]} 失败={third[2]}")
    # 期望行为：SHA1 不匹配 -> 判定为损坏 -> 重新下载修复（而不是把坏文件留在那里）
    repaired = os.path.isfile(bad) and open(bad, "rb").read() == ASSET_BYTES
    check("损坏文件被检出并重新下载修复", third[0] >= 1 and repaired, f"实际 {third}")

    print("\n=== 规则解析单元测试 ===")
    check("无 rules -> 需要", mcget.library_applies({}) is True)
    check("allow windows -> 需要",
          mcget.library_applies({"rules": [{"action": "allow", "os": {"name": "windows"}}]}) is True)
    check("allow linux -> 不需要",
          mcget.library_applies({"rules": [{"action": "allow", "os": {"name": "linux"}}]}) is False)
    check("allow 全部 + disallow windows -> 不需要",
          mcget.library_applies({"rules": [{"action": "allow"},
                                           {"action": "disallow", "os": {"name": "windows"}}]}) is False)

    print("\n=== 镜像替换单元测试 ===")
    mcget.USE_MIRROR = True
    mcget.OFFICIAL_LIB = "https://libraries.minecraft.net/"
    mcget.BMCLAPI_LIB = "https://bmclapi2.bangbang93.com/maven/"
    mcget.OFFICIAL_ASSET = "https://resources.download.minecraft.net/"
    mcget.BMCLAPI_ASSET = "https://bmclapi2.bangbang93.com/assets/"
    got = mcget.to_mirror("https://libraries.minecraft.net/org/foo/bar.jar")
    check("库地址被换成镜像", got == "https://bmclapi2.bangbang93.com/maven/org/foo/bar.jar", got)
    mcget.USE_MIRROR = False

    print("\n=== 进度聚合单元测试（喂假事件，不依赖网络）===")
    tr = mcget.ProgressTracker(window=5)
    tr.on_event({"type": "plan", "total_files": 10, "total_bytes": 1000})
    for _ in range(5):
        tr.on_event({"type": "bytes", "delta": 100})
    s = tr.snapshot()
    check("百分比按字节算", abs(s["percent"] - 0.5) < 1e-9, f"实际 {s['percent']}")
    check("样本不足时速度不报（避免一上来就飙到几百 MB/s）", s["speed_bps"] == 0.0,
          f"实际 {s['speed_bps']}")
    time.sleep(0.4)
    for _ in range(5):
        tr.on_event({"type": "bytes", "delta": 100})
    s = tr.snapshot()
    print(f"  速度={mcget.fmt_speed(s['speed_bps'])} 剩余={mcget.fmt_eta(s['eta_seconds'])}")
    check("有时间间隔后能算出速度", s["speed_bps"] > 0, f"实际 {s['speed_bps']}")
    check("全部下完后百分比为 100%", abs(s["percent"] - 1.0) < 1e-9, f"实际 {s['percent']}")
    check("没有剩余时 ETA 为 0 而不是 None", s["eta_seconds"] == 0, f"实际 {s['eta_seconds']}")

    tr2 = mcget.ProgressTracker()
    tr2.on_event({"type": "plan", "total_files": 3, "total_bytes": 300})
    tr2.on_event({"type": "file_done", "label": "a", "size": 100, "ok": True, "skipped": False})
    tr2.on_event({"type": "file_done", "label": "b", "size": 100, "ok": False, "skipped": False})
    tr2.on_event({"type": "file_done", "label": "c", "size": 100, "ok": True, "skipped": True})
    s2 = tr2.snapshot()
    check("失败文件被单独计数", s2["failed_files"] == 1, f"实际 {s2['failed_files']}")
    check("跳过的文件计入完成数", s2["done_files"] == 3, f"实际 {s2['done_files']}")

    print("\n=== 格式化函数单元测试 ===")
    check("fmt_bytes 字节", mcget.fmt_bytes(512) == "512 B", mcget.fmt_bytes(512))
    check("fmt_bytes MB", mcget.fmt_bytes(1024 * 1024).endswith("MB"), mcget.fmt_bytes(1024 * 1024))
    check("fmt_speed 带 /s", mcget.fmt_speed(2 * 1024 * 1024).endswith("MB/s"),
          mcget.fmt_speed(2 * 1024 * 1024))
    check("fmt_speed 0", mcget.fmt_speed(0) == "0 B/s", mcget.fmt_speed(0))
    check("fmt_eta 秒", mcget.fmt_eta(45) == "45 秒", mcget.fmt_eta(45))
    check("fmt_eta 分", mcget.fmt_eta(83).startswith("1 分"), mcget.fmt_eta(83))
    check("fmt_eta 算不出来", mcget.fmt_eta(None) == "--", mcget.fmt_eta(None))

    print("\n=== 取消下载（GUI 的「停止」按钮走的就是这条路）===")
    mcget.request_cancel()
    mcget.stat.update({"downloaded": 0, "skipped": 0, "failed": 0})
    tracker.reset()
    rc_cancel = run_download()
    st = mcget.stat["downloaded"], mcget.stat["skipped"], mcget.stat["failed"]
    check("取消后返回码非 0", rc_cancel != 0, f"实际 {rc_cancel}")
    check("取消后不再下载新文件", st[0] == 0, f"实际 {st}")
    check("取消状态被上报", mcget.is_cancelled() is True)
    mcget.reset_cancel()
    check("reset 后取消状态清空", mcget.is_cancelled() is False)
    # 复位后能正常接着跑（验证取消不是「一次性毒药」）
    mcget.stat.update({"downloaded": 0, "skipped": 0, "failed": 0})
    rc_again = run_download()
    check("取消复位后能继续正常下载", rc_again == 0, f"实际 {rc_again}")

    print("\n=== Fabric / Quilt（用假 meta 服务验证整条安装链路）===")
    # 假一个 fabric-meta：列表接口 + profile 接口
    FABRIC_LIB = b"FAKE-FABRIC-ASM-JAR"
    FABRIC_LIB_SHA = sha1(FABRIC_LIB)
    FABRIC_LOADER = b"FAKE-FABRIC-LOADER-JAR"
    FABRIC_LOADER_SHA = sha1(FABRIC_LOADER)

    LOADER_VERSION = "0.15.11"
    PROFILE = {
        "id": f"fabric-loader-{LOADER_VERSION}-{VERSION_ID}",
        "inheritsFrom": VERSION_ID,
        "type": "release",
        "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
        "arguments": {"game": [], "jvm": ["-DFabricMcEmu= net.minecraft.client.main.Main "]},
        "libraries": [
            # 典型 Fabric 格式：只有 name + url，路径要自己从 Maven 坐标推
            {"name": "org.ow2.asm:asm:9.8", "url": "{BASE}fabricmaven/",
             "sha1": FABRIC_LIB_SHA, "size": len(FABRIC_LIB)},
            {"name": "net.fabricmc:fabric-loader:" + LOADER_VERSION,
             "url": "{BASE}fabricmaven/", "sha1": FABRIC_LOADER_SHA,
             "size": len(FABRIC_LOADER)},
            # 带 classifier 的坐标
            {"name": "net.fabricmc:yarn:1.20.1+build.7:v2", "url": "{BASE}fabricmaven/"},
        ],
    }
    _fill(PROFILE, base)
    LOADER_LIST = [{"loader": {"version": LOADER_VERSION, "stable": True},
                    "intermediary": {"version": VERSION_ID}}]

    fabric_routes = {
        "/BASE/fabric/v2/versions/loader/" + VERSION_ID:
            json.dumps(LOADER_LIST).encode(),
        f"/BASE/fabric/v2/versions/loader/{VERSION_ID}/{LOADER_VERSION}/profile/json":
            json.dumps(PROFILE).encode(),
        # Maven 布局：<仓库根>/org/ow2/asm/asm/9.8/asm-9.8.jar
        "/BASE/fabricmaven/org/ow2/asm/asm/9.8/asm-9.8.jar": FABRIC_LIB,
        f"/BASE/fabricmaven/net/fabricmc/fabric-loader/{LOADER_VERSION}"
        f"/fabric-loader-{LOADER_VERSION}.jar": FABRIC_LOADER,
        "/BASE/fabricmaven/net/fabricmc/yarn/1.20.1+build.7/"
        "yarn-1.20.1+build.7-v2.jar": b"FAKE-YARN-JAR",
    }

    # 直接把路由并进全局表，Handler 会从 ROUTES 里找
    ROUTES.update({k: v for k, v in fabric_routes.items()})
    # 列表/profile 是 JSON，走 dynamic 分支：塞进 MANIFEST 同级不方便，
    # 这里单独起一个 dict 通过 Handler 的 dynamic 查找 —— 用 ROUTES 也能命中，
    # 因为 ROUTES 里存的就是 bytes，Handler 会原样返回。
    mcget.FABRIC_META = base + "fabric"
    mcget.QUILT_META = base + "quilt"
    mcget.USE_MIRROR = False

    # --- Maven 坐标 -> 路径 ---
    check("maven 坐标转路径", mcget.maven_to_path("org.ow2.asm:asm:9.8")
          == "org/ow2/asm/asm/9.8/asm-9.8.jar", mcget.maven_to_path("org.ow2.asm:asm:9.8"))
    check("maven 带 classifier", mcget.maven_to_path("net.fabricmc:yarn:1.20+build.7:v2")
          == "net/fabricmc/yarn/1.20+build.7/yarn-1.20+build.7-v2.jar",
          mcget.maven_to_path("net.fabricmc:yarn:1.20+build.7:v2"))
    check("maven 坐标不合法时返回 None", mcget.maven_to_path("nonsense") is None)

    # --- 镜像替换 ---
    mcget.USE_MIRROR = True
    got_fab = mcget.to_mirror("https://maven.fabricmc.net/org/ow2/asm/asm/9.8/asm-9.8.jar")
    check("Fabric maven 走镜像",
          got_fab == "https://bmclapi2.bangbang93.com/maven/org/ow2/asm/asm/9.8/asm-9.8.jar",
          got_fab)
    got_quilt = mcget.to_mirror("https://maven.quiltmc.org/repository/release/x/y.jar")
    check("Quilt maven 走镜像",
          got_quilt == "https://bmclapi2.bangbang93.com/maven/x/y.jar", got_quilt)
    mcget.USE_MIRROR = False

    # --- 真的装一遍 ---
    mcget.USE_MIRROR = False
    mcget.stat.update({"downloaded": 0, "skipped": 0, "failed": 0})
    ok = mcget.install_mod_loader("fabric", VERSION_ID, LOADER_VERSION)
    check("Fabric 安装流程返回成功", ok is True)

    vid = PROFILE["id"]
    vjson = os.path.join(mcget.GAME_DIR, "versions", vid, f"{vid}.json")
    check("Fabric version json 已写出", os.path.isfile(vjson))
    if os.path.isfile(vjson):
        saved = json.load(open(vjson, encoding="utf-8"))
        check("version json 的 id 正确", saved.get("id") == vid, saved.get("id"))
        check("version json 保留了 mainClass",
              saved.get("mainClass", "").endswith("KnotClient"), saved.get("mainClass"))
        check("version json 保留了 inheritsFrom",
              saved.get("inheritsFrom") == VERSION_ID, saved.get("inheritsFrom"))
    check("Fabric 库按 Maven 路径落盘",
          os.path.isfile(os.path.join(mcget.GAME_DIR, "libraries",
                                      "org/ow2/asm/asm/9.8/asm-9.8.jar")))
    check("Fabric loader jar 已下载",
          os.path.isfile(os.path.join(mcget.GAME_DIR, "libraries",
                                      f"net/fabricmc/fabric-loader/{LOADER_VERSION}"
                                      f"/fabric-loader-{LOADER_VERSION}.jar")))
    check("带 classifier 的库路径正确",
          os.path.isfile(os.path.join(mcget.GAME_DIR, "libraries",
                                      "net/fabricmc/yarn/1.20.1+build.7/"
                                      "yarn-1.20.1+build.7-v2.jar")))
    # 原版已存在（前面几轮下载过），所以不应该触发补下载
    check("原版已存在时不重复下载", mcget.vanilla_version_exists(VERSION_ID) is True)
    check("不存在的原版能被正确判定为缺失", mcget.vanilla_version_exists("9.9.9") is False)

    print("\n=== 取消后不留残缺版本（用户反馈的那个问题）===")
    # 关键：version json 是启动器认出版本的唯一凭据。
    # 只要它先落盘、下载再被取消，启动器里就会出现一个「看得见但打不开」的版本。
    mcget.OFFICIAL_MANIFEST = base + "manifest.json"
    mcget.OFFICIAL_LIB = base + "lib/"
    mcget.OFFICIAL_ASSET = base + "assets/"
    mcget.USE_MIRROR = False
    mcget.reset_cancel()

    # 造一个干净目录，故意先下到一半再取消
    tmp2 = tempfile.mkdtemp(prefix="mcget_cancel_")
    old_dir = mcget.GAME_DIR
    mcget.GAME_DIR = os.path.join(tmp2, ".minecraft")

    mcget.request_cancel()          # 一开始就取消 -> 一个文件都下不成
    mcget.stat.update({"downloaded": 0, "skipped": 0, "failed": 0})
    run_download()
    vdir = os.path.join(mcget.GAME_DIR, "versions", VERSION_ID)
    check("取消后没有生成 version json",
          not os.path.isfile(os.path.join(vdir, f"{VERSION_ID}.json")))
    mcget.reset_cancel()

    # 正常下完后 json 应该出现
    mcget.stat.update({"downloaded": 0, "skipped": 0, "failed": 0})
    run_download()
    check("完整下载后 version json 才出现",
          os.path.isfile(os.path.join(vdir, f"{VERSION_ID}.json")))

    print("\n=== 清理残缺版本 ===")
    # 手工造一个「有 json 但没 jar」的残缺版本，模拟老版本留下的烂摊子
    broken = "1.99.9-broken"
    bdir = os.path.join(mcget.GAME_DIR, "versions", broken)
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, f"{broken}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": broken, "downloads": {"client": {"url": base + "client.jar"}}}, f)

    found = mcget.find_incomplete_versions()
    names = [n for n, _ in found]
    check("缺主 jar 的版本被识别为残缺", broken in names, f"实际 {found}")
    check("完整的版本不会被误判", VERSION_ID not in names, f"实际 {found}")

    dry, notes = mcget.cleanup_incomplete_versions(dry_run=True)
    check("--dry-run 不真删", dry == 0 and os.path.isdir(bdir))

    removed, _ = mcget.cleanup_incomplete_versions(dry_run=False)
    check("真的清理掉了", removed == 1 and not os.path.isdir(bdir))

    # 再清一次应当什么都不报
    check("清理后再扫描是干净的", mcget.find_incomplete_versions() == [])

    # 缺依赖原版的 Fabric 版本也要能被识别
    fab = "fabric-loader-0.1-1.99.9"
    fdir = os.path.join(mcget.GAME_DIR, "versions", fab)
    os.makedirs(fdir, exist_ok=True)
    with open(os.path.join(fdir, f"{fab}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": fab, "inheritsFrom": "1.99.9-notexist"}, f)
    found2 = mcget.find_incomplete_versions()
    check("缺依赖原版的加载器版本被识别",
          any(n == fab for n, _ in found2), f"实际 {found2}")
    mcget.cleanup_incomplete_versions(dry_run=False)

    shutil.rmtree(tmp2, ignore_errors=True)
    mcget.GAME_DIR = old_dir

    print("\n=== 图形界面静态检查（跑不了 Tk，至少保证不会一打开就报错）===")
    gui_path = os.path.join(HERE, "mcget_gui.py")
    if not os.path.isfile(gui_path):
        print("  [跳过] 没找到 mcget_gui.py")
    else:
        src = open(gui_path, "r", encoding="utf-8").read()
        try:
            compile(src, gui_path, "exec")
            check("mcget_gui.py 语法正确", True)
        except SyntaxError as e:
            check("mcget_gui.py 语法正确", False, str(e))

        tree = ast.parse(src)
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "mcget":
                used.add(node.attr)
        missing = sorted(n for n in used if not hasattr(mcget, n))
        check(f"GUI 引用的 {len(used)} 个 mcget 接口都存在", not missing, f"缺失 {missing}")

    server.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 40)
    if fails:
        print(f"有 {len(fails)} 项未通过: {fails}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
