![演示](https://raw.githubusercontent.com/Sixawns/MCget/main/images/demo.png)
# mcget

一个 Minecraft 游戏文件下载器。把某个版本的完整文件拉到本地，交给 PCL / HMCL 启动。

**它只下载，不启动**——不碰账号、不破解、不绕过任何验证。

- 核心 `mcget.py` 只用 Python 标准库，零依赖
- 图形界面 `mcget_gui.py` 需要 `customtkinter`
- 支持原版，以及 Forge / NeoForge / Fabric / Quilt 四个加载器

---

## 快速开始

### 命令行

```bash
python mcget.py list --mirror                 # 看看有哪些版本
python mcget.py download 1.20.1 --mirror      # 下载原版
python mcget.py download 1.20.1 --mirror --dir D:/Minecraft/.minecraft

python mcget.py fabric 1.20.1                 # Fabric，版本留空 = 最新稳定版
python mcget.py quilt  1.20.1
python mcget.py forge  1.20.1 47.4.13         # Forge 必须指定版本
python mcget.py neoforge 1.21.1
```

命令行下也会显示实时进度：

```
[ 42%] 74.3 MB / 180.5 MB | 文件 512/1234 | 3.2 MB/s | 剩余 33 秒
```

### 图形界面

```bash
pip install customtkinter
python mcget_gui.py
```

界面上能做的事：

| | |
|---|---|
| MC 版本 | 下拉框选，勾「显示测试版」会多出快照版本 |
| 游戏目录 | 填你的 `.minecraft`，一般就是 PCL / HMCL 在用的那个 |
| 国内加速镜像 | 国内开着快很多，人在国外可以关 |
| 同时下载 | 同时下几个文件，16 左右比较稳 |
| 加载器 | Fabric / Quilt / Forge / NeoForge 四选一 |
| 进度 | 百分比、已下/总量、实时速度、剩余时间 |
| 停止 | 随时打断，下好的文件留着，再点一次接着下 |

MC 版本和加载器版本都**留空就自动选最新的**，不用查版本号。
鼠标停在任意选项上，界面底部会显示它是干什么的。

### 打包成 exe

Windows 上双击 `build_exe.bat`，自动建虚拟环境、装依赖、打包：

```
dist\mcget.exe        图形界面，双击即用
dist\mcget-cli.exe    命令行版
```

其他系统手动打包：

```bash
pip install customtkinter pyinstaller
pyinstaller --noconfirm --clean mcget_gui.spec
pyinstaller --noconfirm --clean mcget_cli.spec
```

想换图标，放一个 `icon.ico` 在项目目录，spec 会自动用上。

> 别用裸命令 `pyinstaller -F -w mcget_gui.py`。customtkinter 没有官方 hook，
> 主题 json 和字体会漏掉，打出来的 exe 双击直接崩。走 spec 才对。

---

## 代码结构

```
mcget.py            下载核心（只用标准库）
mcget_gui.py        图形界面（CustomTkinter）
build_exe.bat       Windows 一键打包
mcget_gui.spec      → dist\mcget.exe
mcget_cli.spec      → dist\mcget-cli.exe
LICENSE             MIT
DISCLAIMER.md       免责声明
```

### 分层

```
mcget.py  ──(两个回调)──>  mcget_gui.py
  下载逻辑                    界面
  不认识界面                  不碰下载细节
```

界面只通过两个 hook 订阅下载侧的事件，**两边互不依赖**：

```python
mcget.set_log_hook(fn)        # 日志
mcget.set_progress_hook(fn)   # 进度事件
```

所以想换界面（比如换成 Qt / 网页套壳），只需要重写 `mcget_gui.py`，
`mcget.py` 一行都不用动。

### 线程模型（改 GUI 前务必看这段）

下载跑在**工作线程**，日志和进度事件先丢进 `queue.Queue`；
界面主线程用 `after(100ms)` 取队列再刷控件。

**工作线程里绝对不能直接操作界面控件**——Tk 不是线程安全的，会随机崩。

---

## 想改某个功能，动哪里

| 想改的东西 | 去哪改 |
|---|---|
| 换下载源 / 加镜像 | `mcget.py` 顶部的常量区 + `to_mirror()` |
| 下载单文件的行为（重试、校验、限速） | `fetch()` |
| 并发策略 | `fetch_many()` |
| 进度怎么算、速度怎么平滑 | `ProgressTracker` |
| 下载哪些文件 | `collect_all()` / `collect_libraries()` / `collect_assets()` |
| 平台过滤规则（rules） | `library_applies()` |
| 加新的 mod 加载器 | 照 `install_mod_loader()` 的写法加一个 |
| 界面布局 / 术语 | `mcget_gui.py` 的 `_build_ui()` |
| 悬停提示文案 | `mcget_gui.py` 的 `HINTS` 字典 |

改完核心逻辑，跑一下 `selftest.py` 能确认没把老功能搞坏
（它会在本地起假 HTTP 服务器，不联网）。

---

## 几个已知的设计选择

**为什么 Fabric/Quilt 和 Forge 是两种玩法**
Forge/NeoForge 只发「安装器 jar」，得双击跑一遍装。
Fabric/Quilt 的 meta 服务直接给 launcher profile（自带库清单和 `mainClass`），
写进 `versions/` 就是标准可启动版本，启动器刷新一下就能选——比跑安装器干净。

**为什么速度不会虚高**
百分比按字节算，总量来自清单里记录的 size；速度只统计**真实网络字节**，
跳过的文件不计入。所以第二次运行时大量跳过，不会显示几百 MB/s。

**为什么不用 tqdm**
标准库就能在终端刷同一行，不值得为一个进度条多一个依赖。

---

## 声明

完整声明见 [DISCLAIMER.md](DISCLAIMER.md)。要点：

- 不分发任何游戏内容，只从 Mojang 官方或 BMCLAPI 公开镜像获取
- 与 Mojang Studios / Microsoft / BMCLAPI 无从属关系
- 代码按 MIT 授权（见 [LICENSE](LICENSE)），但**游戏文件不在此列**——
  那些文件的权利归 Mojang，本工具不主张也不转移任何权利

仅供已通过官方渠道购买 Minecraft 的用户下载自己有授权的文件。
