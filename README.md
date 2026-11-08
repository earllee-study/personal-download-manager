# Personal Download Manager (PDM)

一个从零实现的多连接断点续传下载管理器，用于测试与互操作性研究。

项目包含：

1. 多连接下载、断点续传、HLS/DASH、浏览器集成、MSE 捕获等下载能力；
2. 本地可控服务器、自动化测试与可复现基准。

> **测试项目声明**：这是一个使用 **deepseek-v4.1-flash-1017** 开发的个人学习/测试项目，不是生产级软件。仅用于测试与互操作性研究；请只下载你有权下载的内容。原始基准数据与内部证据不随公开仓库发布。

> 当前状态：**引擎 + CLI + macOS 风格 GUI/队列 + 界面多语言（中/英） + 调度/重试/重复计划（每天/按星期/每隔 N 天）+ 多队列/带宽时段 + 下载完成后关机 + 主机策略/代理分组 + 站点认证 + 分类目录 + 通配批量 + AIMD/HTTP/2 + HTTP/3（QUIC）+ FTP/FTPS/SFTP + 多源镜像 + HLS + DASH（多 Period/SIDX + 双轨 ffmpeg 合并）+ 浏览器集成（Chromium/Firefox）+ MSE 多轨实验捕获（fMP4/WebM，Chrome/Edge + Firefox 128+ MAIN-world）、GUI 队列导入、后台归档与源轨清理 + 安装包已完成**，392 项自动化测试（Node 可用时 392 passed；无 Node 时 391 passed、1 skipped），公网与本地基准（含 2 万行列表压力测试、HTTP/2/3 与丢包、FTP/SFTP 吞吐）均已跑通。
> HLS（含 AES-128）、DASH、浏览器媒体嗅探/页面扫描已完成；MSE/blob 多轨实验捕获已实现（未加密 fMP4/WebM、默认 8 GiB/轨、16 GiB/会话、可配置、可选 ffmpeg 合并），**fMP4 与 WebM 均已实机验证**，并已建立公开站点矩阵（DASH-IF/Akamai、Shaka angel-one、Unified Tears of Steel + 本地 WebM 控制；见 `docs/mse-site-matrix-2026-11-08.md`），捕获结束后会作为“已捕获”任务进入 GUI 队列。单轨 Chrome/Edge 手动验证通过；多轨已在 Edge 156 本地夹具、DASH-IF/Akamai 真实站点，以及 **Chrome for Testing 157**（同版本内核，headless 隔离配置）的正常/重置复验中通过（见 [`docs/browser-verification-2026-11-04-mse-multitrack.md`](docs/browser-verification-2026-11-04-mse-multitrack.md)、[`docs/browser-verification-2026-11-04-mse-real-site.md`](docs/browser-verification-2026-11-04-mse-real-site.md)、[`docs/browser-verification-2026-11-07-mse-chrome.md`](docs/browser-verification-2026-11-07-mse-chrome.md)）；**Firefox 159 的 MAIN-world 注入已用 WebDriver BiDi 探针 + 本地 WebM/严格 CSP 页验证**（2 轨/4 chunks/48,321 B；Gecko 下 ArrayBuffer 需在 MAIN world 先转 base64），完整 background→Native Host→GUI 导入/归档已验证（WebM 合并 47,971 B、fMP4 合并 31,589 B、DASH-IF/Akamai BBB 合并 4,189,997 B，GUI 归档 SHA-256 一致）；本地 hls.js 第二播放器已验证（muxed 242,020 B，GUI 归档一致），公网 hls.js demo 因 jQuery/googleapis 不可达默认 blocked（测试专用 jQuery shim 下已捕获 5,506,934 B；webRequest 重定向到 cdnjs 亦通过（2,730,802 B））；popup 页面按钮已通过 iframe 点击验证、工具栏 action popup 未点击；Shaka Player demo（appspot）不可达；商业 DRM/直播未测，见 [`docs/firefox-mse-mainworld-evaluation-2026-11-08.md`](docs/firefox-mse-mainworld-evaluation-2026-11-08.md)；完整视频抓取器仍在 Roadmap。

> 内部开发状态、交接文档与原始证据不随公开仓库发布。

![PDM GUI](docs/gui.png)

---

## 1. 它已经能做什么

- **桌面 GUI（macOS 风格）**：macOS 风格的浅色/深色外观：系统字体、圆角卡片、胶囊化搜索框/输入框、细滚动条、单色线性图标、系统蓝/绿/橙/红状态色、右对齐表单标签；保留原生窗口框架以保证缩放/贴边/辅助功能。下载列表、实时进度/速度/ETA、队列并发（1–10）、暂停/继续/删除、批量/拖放添加、系统托盘、任务状态持久化。列表是虚拟化的（2 万行只绘制可见行），进度更新的开销不随列表长度增长（见 4.9）。
- **定时开始与重复计划**：每个任务可设开始时间（`Ctrl+T` 或添加对话框），到点自动进入队列；程序重启后未来计划仍生效，过期计划转为“已暂停”，不会在启动时突然下载。计划还可以**每天**、**按星期**或**每隔 N 天**重复：任务完成（或重试耗尽）后自动排到下一个匹配时间，重启后会滚动到下一个未来时刻。每隔 N 天以创建规则时所选日期为锚点（`anchor + k*N` 天，时间按本地钟表），N=1 等价于每天。
- **代理分组**：把常用代理命名保存（`us`、`eu`…），主机规则只填写组名即可复用；改一个分组会同时作用于所有引用它的站点。直接写在规则里的代理优先于分组，分组优先于默认代理。
- **失败自动重试队列**：超时、连接中断、429/5xx 等瞬时错误按指数退避自动重新入队（默认 3 次，首次间隔 30s，之后翻倍）；404/401/校验失败/磁盘错误等永久失败不重试，避免无意义循环。
- **多队列与带宽时段**：任务可分到不同队列（每队列独立并发数，默认“主队列”），右键即可移动；可按时间段设置全局限速（如 09:00–18:00 限 1M，22:00–06:00 不限），跨午夜窗口同样支持。
- **下载完成后关机**：“下载 → 所有下载完成后关机”一次性开启（不写入配置，重启后默认关闭）。只有队列空闲、所有任务都已结束（完成/失败/取消）且至少一个完成时才进入倒计时；等待、下载中、暂停、计划与待重试任务都会阻止关机，避免半途退出。倒计时默认 60 秒（设置 →“关机倒计时”，5–3600 秒），主窗口显示剩余秒数并弹出可取消的对话框，托盘同步提示。倒计时期间新增或暂停任务会取消本次倒计时，但保持开关开启；关机命令失败会提示并自动关闭开关。
- **按主机策略 + 站点认证**：同一主机的连接数上限在所有任务间共享；每主机可单独设连接数、限速、代理，以及 **User-Agent / Cookie / Basic 或 Digest 认证**；另有跨任务的全局限速。规则用 fnmatch 模式（`*.cdn.example.com`），首个匹配生效，规则值覆盖任务自身的同类设置。
- **分类目录**：按扩展名自动把文件保存到 `下载目录/音乐`、`视频`、`程序`、`文档`、`压缩文件` 等子目录（按类型分类）；分类表和子目录名可在设置中编辑，也可以对单个任务选择“自动 / 不分类 / 指定分类”。
- **通配批量 URL**：一行一个模式，支持 `[1-10]`、`[001-010]`、`[1-10:2]`、`[a-z]` 与 `{a,b,c}`；GUI 批量对话框实时显示展开数量，CLI 新增 `batch` 子命令。
- **自适应并发（AIMD，实验）**：`--adaptive` 从少量连接起步，健康时每个周期 +1，遇到 429/503 立即减半并冷却；上限仍是 `-n`。服务器不欢迎高并发时比固定 8 连接更快、更少被拒。
- **HTTP/2 多路复用**：`--http2` 时所有分段共用一个客户端；服务器协商到 h2 时在一条连接上并发多路复用（本地 TLS+h2 服务器已自动化验证），否则退化为普通连接池。
- **HTTP/3（QUIC，实验）**：`--http3` 用 aioquic 在一条 UDP/QUIC 连接上并发所有分段（Range、重定向、Cookie、Basic/Digest 认证、主机策略全部照常）；本地真实 QUIC 测试服务器覆盖 ALPN、单连接多流与丢包重传，丢包 5% 仍能字节级正确完成。不支持代理（CONNECT-UDP/MASQUE 未实现），也没有 Alt-Svc 自动升级。
- **FTP / FTPS / SFTP**：`ftp://`、`ftps://`（显式 TLS）与 `sftp://` 都可以直接下载。FTP 在服务器支持 SIZE + `REST` 时用**多连接分段**（每连接 `REST`+`RETR`，按偏移直接写一个预分配 `.part`，动态拆分，无合并阶段；`-n` 生效）；不支持 REST/SIZE、文件太小或 `-n 1` 时自动退回单流。数据连接默认**被动模式**，`--ftp-active` 可切换为**主动模式**（PORT/EPRT，由服务器连接回本机；明文 FTP 与显式 FTPS、多连接分段均支持）。SFTP 用远程 `seek` 单流。两者共享 `.pdm.part`/`.pdm.json` 续传、指数退避、全局限速/主机策略、SHA-256 与完成改名；SFTP 支持密码、私钥（含口令）、known_hosts 主机校验（默认拒绝未知主机，可显式 `--sftp-auto-host-key`）。
- **多源镜像**：`--mirror URL`（可重复）把分段轮询分配到多个镜像；启动时用大小 + ETag/Last-Modified 校验一致性，不匹配的镜像直接丢弃；镜像中途失效会切到其他镜像。
- **浏览器集成（Chromium + Firefox）**：MV3 扩展 + Native Messaging；右键/弹窗把链接交给 PDM，可选接管浏览器下载；**媒体嗅探**在播放视频后列出本页 HLS/DASH/普通视频，弹窗打开时还会**扫描页面 DOM、内联脚本、iframe 与开放 shadow DOM 中的 m3u8/mpd 链接**；HLS master 会展开为带分辨率/码率的独立候选，条目标注来源（network/dom/script/iframe/shadow），可一键下载（HLS 含标准 AES-128；不碰 DRM；MSE/blob 捕获见下一条实验功能）。提供 `browser/test-page.html` 手动验证页。Cookie 只按目标 URL 读取。Chromium 加载 `browser/extension`，Firefox 先运行 `python browser/build_firefox.py` 再临时载入 `browser/firefox`，见 [`docs/browser-integration.md`](docs/browser-integration.md)。
- **MSE/blob 捕获（实验，Chrome/Edge/Firefox 128+）**：扩展弹窗“开始 MSE 捕获”向页面 MAIN world 注入 hook，拦截 `MediaSource.addSourceBuffer` / `SourceBuffer.appendBuffer`，按 `SourceBuffer` 分轨（视频/音频，最多 4 轨）通过 Native Messaging 分块写入 `%APPDATA%\PersonalDownloadManager\capture\mse\`。**默认每轨 8 GiB、会话 16 GiB，可用 `PDM_MSE_MAX_TRACK_BYTES`/`PDM_MSE_MAX_TOTAL_BYTES` 调整；保留 1 GiB 磁盘余量（`PDM_MSE_MIN_FREE_BYTES`）**；支持未加密 **fMP4 与 WebM**（WebM 已用 VP9+Opus 实机验证），MIME 缺失时从 init 字节嗅探 `hdlr`/`TrackType`，并 hook `changeType`。检测到 EME/DRM 立即停止，不做解密。配置 ffmpeg（`PDM_DOWNLOADER_FFMPEG`、GUI 的 `ffmpeg_path` 或 PATH）时，`mse_end_all` 会用 `-c copy` 把最大的视频+音频合并为 `.merged.mp4/.webm`，同时保留单轨文件；没有 ffmpeg 时保留各轨。**捕获结束时 Host 写出原子完成记录，GUI 轮询后把整个会话作为一条“已捕获”任务加入队列**（优先指向合并文件，否则最大视频/音频轨；源页面/标题作为 URL/属性；删除任务并勾选删除文件会一并清理所有轨道；Edge 156 + DASH-IF/Akamai 真机复验通过）。默认在后台把合并文件（无合并时为主轨）移动到下载目录/分类：同卷 `os.replace` 即时完成，跨卷分块复制到 `.pdm-archive.part`、校验大小/SHA-256 后原子改名；归档期间状态显示“归档中”，失败保留源文件并提示。源轨默认仍留在 `capture\mse\`；可开启“归档成功后删除已合并的源轨”（默认关闭），或在右键菜单“清理捕获源轨…”手动清理（只删 `capture/mse` 内的源轨，保留归档/主文件）。GUI 线程不做大文件搬运。停止路径已加请求/链/合并/弹窗超时；受控 MediaSource 重置后 `mseStop` 0.10s 返回并完成合并，见 [`docs/browser-verification-2026-11-04-mse-reset-stop.md`](docs/browser-verification-2026-11-04-mse-reset-stop.md)。**Firefox 159** 的 MAIN-world 注入已验证：Gecko 的 content script 不能直接读页面 principal 的 `ArrayBuffer`，所以 `mse_capture.js` 在 MAIN world 先转 base64、`mse_bridge.js` 直接转发字符串；Chromium 仍走 ArrayBuffer + transfer 的零拷贝路径。Firefox 需要 128+，更低版本会在开始捕获时明确报错。**完整 background→Native Host→GUI 已验证**：WebM 27,804 B + 20,510 B → 合并 47,971 B；本地 fMP4 14,100 B + 18,030 B → 合并 31,589 B；DASH-IF/Akamai BBB 4,028,948 B + 167,793 B → 合并 4,189,997 B（h264 480×270 + aac）；打包 GUI 归档 SHA-256 一致。本地 **hls.js 1.6.15** 第二播放器（ffmpeg 生成的 fMP4 HLS）捕获 1 条 muxed 轨 242,020 B（h264 320×180 + aac），GUI 归档 SHA-256 一致；公网 hls.js demo 因 jQuery 的 ajax.googleapis.com 在本网络 TCP 超时默认 blocked；测试专用 jQuery shim 下 demo 正常初始化，捕获 2 轨 → 5,506,934 B；webRequestBlocking 重定向到 cdnjs 同样通过（2,730,802 B）。popup 页面按钮已通过 iframe 点击验证（开始/停止，状态“多轨捕获中”→“已合并”，2 轨/47,971 B）；工具栏 action popup 未点击；Shaka Player demo（appspot）不可达；商业 DRM/直播未测。评估与限制见 [`docs/firefox-mse-mainworld-evaluation-2026-11-08.md`](docs/firefox-mse-mainworld-evaluation-2026-11-08.md)。
- **HLS（VOD）**：`pdm hls <m3u8> -o out.ts`，支持 master 播放列表选码率、相对路径、`EXT-X-BYTERANGE`、`EXT-X-MAP` 初始化分片，分段并发下载后按序拼接；支持标准 **AES-128**（`EXT-X-KEY` 的密钥 URI 可获取，AES-CBC + PKCS#7；IV 显式或按 media sequence 推导），SAMPLE-AES/DRM 与直播列表明确拒绝。GUI 里粘贴 `.m3u8` 会自动识别（可取消勾选）。
- **DASH（VOD，未加密）**：`pdm dash <manifest.mpd> -o out.mp4`，支持 `SegmentTemplate`（`$Number$`/`$Time$`/零填充）、`SegmentTimeline`（含 `r=-1`）、`SegmentList`、嵌套 `BaseURL`、单文件 Representation、**多 Period** 与 **SegmentBase + indexRange（SIDX）**；按带宽/高度/索引选轨道。`--track video|audio` 下载单轨；`--track both` 同时下载视频和音频，配置了 ffmpeg（`--ffmpeg PATH` 或 PATH 中的 `ffmpeg`）时用 `-c copy` 合并为可播放 MP4，没有 ffmpeg 时保留两条轨道并明确提示。GUI 粘贴 `.mpd` 自动识别，轨道下拉框含“视频+音频（合并）”；直播与 `ContentProtection` 明确拒绝；层级化 SIDX（reference_type=1）暂不支持。
- **界面多语言（中文 / English）**：GUI 支持“跟随系统 / 简体中文 / English”，覆盖主窗口、添加/批量/设置/定时/更新对话框以及主机规则、代理分组、分类、队列、带宽时段编辑器；语言保存在 `config.json`，切换后重启生效。英文模式下未翻译字符串会回退为中文，分类名/队列名等用户数据保持原样。CLI 帮助本身为英文。
- **自动更新（用户主动检查）**：`update` 子命令或 GUI“帮助 → 检查更新…”。读取 HTTPS JSON 清单、比较数字版本，把安装包流式下载到 `.part`，校验大小与 SHA-256 后原子改名；只有显式 `--launch` 或 GUI 二次确认才会运行安装器。启动时**不会**自动联网，也不会静默安装。HTTPS 清单/安装包（回环 HTTP 仅用于测试）、2 GiB 上限、失败/取消自动清理。详见 [`docs/updating.md`](docs/updating.md)。
- **多连接分段下载**：默认 8 连接，可配置 1–64；连接数与文件大小自动匹配。
- **动态分段（核心加速策略）**：某个连接提前完成后，把剩余最大的分段从当前游标处一分为二，交给空闲连接继续；不会重复下载已写字节。
- **断点续传**：原子 JSON 日志记录每个分段的游标；崩溃或取消后可从原位置继续。
- **校验一致性**：`ETag`/`Last-Modified` + `If-Range`；内容变化时自动重新下载，避免拼出损坏文件。
- **重试**：连接断开、超时、5xx、429、`Retry-After`、服务端提前关流；指数退避，可配置次数。
- **服务器兼容性**：
  - `206 Partial Content` 正常分段；
  - 服务器忽略 `Range` → 自动退回单连接；
  - `Transfer-Encoding: chunked`、长度未知 → 流式单连接；
  - `Content-Disposition`（含 RFC 5987 `filename*`）→ 自动命名；
  - 302/307 重定向；Basic/Digest 认证；Cookie/Referer/自定义头。
- **代理**：HTTP 代理、SOCKS5（需 `socksio`）。
- **限速**：全局令牌桶，`--limit 5M` 是整次下载的总速率，不是每连接。
- **SHA-256 校验**：下载完成后校验，失败不发布最终文件。
- **进度输出**：人读进度行 / `--json` 机器可读快照 / 最终结果 JSON。
- **HTTP/2 多路复用（实验开关）**：`--http2`（需要 `h2`）；服务器支持 h2 时 8 个分段共用一条 TLS 连接，本地 h2 测试服务器与基准均覆盖。

---

## 2. 安装与运行

环境：Windows 10/11，Python 3.11+（本机验证于 **3.14.7**）。

```bat
cd D:\studyfile\claude_code\PDM
python -m pip install -e ".[dev]"
:: 只装引擎/CLI：python -m pip install -e .
:: 只补 GUI：python -m pip install -e ".[gui]"
:: 只补 HTTP/3：python -m pip install -e ".[http3]"   （aioquic）
:: 只补 SFTP：python -m pip install -e ".[sftp]"      （paramiko；FTP 用标准库）
:: 全功能：python -m pip install -e ".[full]"
```

GUI 依赖 PySide6-Essentials（QtWidgets），不安装 130 MB 的 WebEngine。

DASH 双轨合并需要**外部 ffmpeg**（不随项目安装）：可用 `--ffmpeg PATH`、`PDM_DOWNLOADER_FFMPEG` 环境变量，或把 ffmpeg 放进 PATH；没有 ffmpeg 时 `--track both` 会保留视频与音频两个文件。

如果不想安装，可直接用项目根目录的 `pdm.cmd`（内部设置 `PYTHONPATH=src`）：

```bat
pdm.cmd probe "https://proof.ovh.net/files/10Mb.dat"
pdm.cmd download "https://proof.ovh.net/files/10Mb.dat" -o 10Mb.dat -n 8
```

或：

```bat
python -m personal_download_manager download URL -o file.bin -n 16 --limit 20M
```

### 常用命令

```bat
:: 只看服务器元数据
pdm.cmd probe URL --json

:: 8 连接下载，校验 SHA-256
pdm.cmd download URL -o out.bin -n 8 --sha256 <64位hex>

:: 断点续传（默认开启）
pdm.cmd download URL -o out.bin

:: 丢弃已有分片，从头开始
pdm.cmd download URL -o out.bin --restart

:: 关闭动态分段，固定初始分段数
pdm.cmd download URL -o out.bin -n 8 --no-dynamic

:: 自适应连接数（AIMD，上限 8）
pdm.cmd download URL -o out.bin -n 8 --adaptive

:: HTTP/2（服务器支持时多路复用）
pdm.cmd download URL -o out.bin -n 8 --http2

:: HTTP/3（QUIC；需要 aioquic，不能与 --proxy 同时使用）
pdm.cmd download URL -o out.bin -n 8 --http3
pdm.cmd probe URL --http3 --json

:: 多源镜像（同一文件，可重复 --mirror）
pdm.cmd download URL -o out.bin -n 8 --mirror https://mirror1/file.bin --mirror https://mirror2/file.bin

:: HLS VOD：最高码率、4 个分段并发、限速 5M
pdm.cmd hls https://example.com/vod/master.m3u8 -o movie.ts -n 4 --quality best --limit 5M
pdm.cmd hls https://example.com/vod/media.m3u8 -o movie.ts --quality 0

:: DASH：最高码率视频轨 / 音频轨 / 双轨合并（需要 ffmpeg）
pdm.cmd dash https://example.com/vod/manifest.mpd -o movie.mp4 -n 4 --quality best
pdm.cmd dash https://example.com/vod/manifest.mpd -o audio.m4a --track audio
pdm.cmd dash https://example.com/vod/manifest.mpd -o movie.mp4 -n 4 --track both --ffmpeg "D:\Application\tools\ffmpeg\bin\ffmpeg.exe"

:: 限速 5 MiB/s
pdm.cmd download URL -o out.bin --limit 5M

:: 跨任务全局限速 + 每主机最多 4 连接
pdm.cmd download URL -o out.bin --global-limit 10M --max-host-connections 4

:: 主机策略文件（连接数 / 限速 / 代理 / 站点 UA、Cookie、认证）
pdm.cmd download URL -o out.bin --policy hosts.json

:: 通配批量：img01.jpg … img10.jpg
pdm.cmd batch "https://cdn.example.com/img[01-10].jpg" -o D:\downloads -n 8
pdm.cmd batch "https://mirror/{a,b}/file.bin" "https://other/[a-c].zip" -o D:\dl

:: 按分类子目录保存（内置分类表）
pdm.cmd download URL -o D:\downloads --category auto
pdm.cmd download URL -o D:\downloads --category 视频

:: 自定义分类表（JSON：name / folder / extensions）
pdm.cmd download URL -o D:\downloads --category auto --categories cats.json

:: 带 Cookie、Referer、认证
pdm.cmd download URL -o out.bin --cookie "a=b; c=d" --referer "https://example.com/" --auth user:pass

:: HTTP/SOCKS5 代理
pdm.cmd download URL -o out.bin --proxy http://127.0.0.1:8080
pdm.cmd download URL -o out.bin --proxy socks5://127.0.0.1:1080

:: FTP / 显式 FTPS（续传用 REST；FTPS 自签名测试证书用 --insecure）
pdm.cmd download "ftp://user:password@host/pub/file.bin" -o out.bin
pdm.cmd download "ftps://user:password@host/pub/file.bin" -o out.bin --insecure

:: FTP 主动模式（PORT/EPRT；默认被动模式）
pdm.cmd download "ftp://user:password@host/pub/file.bin" -o out.bin --ftp-active

:: SFTP：密码 / 私钥 + known_hosts；首次连接也可显式接受主机密钥
pdm.cmd download "sftp://user:password@host/home/user/file.bin" -o out.bin --sftp-auto-host-key
pdm.cmd download "sftp://user@host/home/user/file.bin" -o out.bin --sftp-key "%USERPROFILE%\.ssh\id_ed25519" --sftp-known-hosts "%USERPROFILE%\.ssh\known_hosts"

:: 机器可读进度 + 结果
pdm.cmd download URL -o out.bin --json

:: 主动检查更新（不自动联网；HTTPS 清单）
pdm.cmd update --manifest https://downloads.example.com/personal-download-manager/latest.json
pdm.cmd update --manifest https://…/latest.json --download D:\updates
pdm.cmd update --manifest https://…/latest.json --launch   :: 校验后启动安装器
```

`--help` 查看全部参数。主机策略文件格式：

```json
{
  "defaults": {"connections": 4, "speed_limit": null, "proxy": null},
  "proxy_groups": {
    "us": "socks5://127.0.0.1:1080",
    "office": "http://proxy.example.com:8080"
  },
  "hosts": [
    {"pattern": "*.cdn.example.com", "connections": 8, "speed_limit": "20M"},
    {
      "pattern": "members.example.com",
      "connections": 4,
      "user_agent": "SiteBot/1.0",
      "cookie": "session=abc123",
      "auth": "user:password",
      "auth_type": "basic"
    },
    {"pattern": "slow.example.com", "connections": 2, "proxy": "socks5://127.0.0.1:1080"},
    {"pattern": "*.overseas.example", "proxy_group": "us"},
    {"pattern": "*.intranet.example", "proxy_group": "office"}
  ]
}
```

- `connections` 是该主机**所有任务合计**的并发连接上限；`--max-host-connections` 是它的全局默认值。
- `speed_limit` 与任务自身的 `--limit` 同时生效时取较小值；全局限速再叠加在两者之上。
- `proxy` / `user_agent` / `cookie` / `auth` 覆盖任务级同类设置；规则留空则沿用任务自身设置。
- `proxy_groups` 可用上面的映射形式，也可写成 `[{"name": "us", "proxy": "socks5://…"}]`；规则用 `proxy_group` 引用组名。优先级：规则里的 `proxy` > `proxy_group` > `defaults.proxy`。引用不存在的组会记录警告并退回默认代理（GUI 会在保存时直接拒绝）。
- 规则按重定向后的最终主机重新匹配；镜像主机与 HLS 分片所在主机也各自按自己的规则取 UA/Cookie/认证。

### 桌面 GUI

```bat
pdm.cmd gui
pdm-gui.cmd          :: 优先用 pythonw 启动，无控制台窗口
python -m personal_download_manager.gui
```

- 工具栏：添加下载、继续、暂停、删除、打开目录、设置。
- 队列：设置里可调“同时下载任务数”（1–10）；完成后自动启动下一个。
- “下载 → 所有下载完成后关机”是会话开关：队列空闲且所有任务结束时倒计时关机，倒计时可在状态栏/对话框中取消；设置里只保存倒计时长度（默认 60 秒）。
- 浏览器 MSE 捕获结束时，GUI 把整个捕获会话导入为一条状态为“已捕获”的任务（合并文件优先），并在后台把合并文件归档到下载目录/分类；可正常打开/打开目录/删除，属性里显示源页面、轨道数、合并与原始捕获路径。设置 → “MSE 捕获自动保存到下载目录”可关闭归档；右侧“归档成功后删除已合并的源轨”（默认关闭）或右键“清理捕获源轨…”可回收源轨空间。
- 添加对话框：基础项 + 高级项（Referer、Cookie、认证、代理、User-Agent、SHA-256、额外请求头、镜像 URL、HTTP/2/HTTP/3、TLS 校验、AIMD、FTP 主动模式、SFTP 私钥/known_hosts）；粘贴 `.m3u8`/`.mpd` 会自动识别为流媒体。
- 支持批量粘贴 URL、拖放链接到窗口、双击打开文件、右键菜单、系统托盘通知。
- 剪贴板监视默认关闭，可在设置中开启。
- 配置与任务列表保存在 `%APPDATA%\PersonalDownloadManager\config.json` 和 `downloads.json`；关闭窗口默认最小化到托盘，可在设置中关闭。
- 设置 → “语言”可选“跟随系统 / 简体中文 / English”，切换后重启生效；分类名、队列名等用户数据不会被自动翻译。
- 主题：跟随系统 / 浅色 / 深色，均为 macOS 风格（浅色 `#F5F5F7` 窗口 + 白色圆角卡片，深色 `#1C1C1E` + `#2C2C2E` 卡片，系统蓝 `#007AFF`/`#0A84FF`）。工具栏图标为按主题着色的单色线性图标。
- 暂停会保留 `.part` 与 `.json`，继续时从断点恢复；删除时可选择同时删除文件。
- 更细的交互与线程模型见 [`docs/gui.md`](docs/gui.md)。

### 浏览器集成

```bat
packaging\build.cmd
python packaging\install_native_host.py --browsers chrome,edge
:: Firefox:
python browser\build_firefox.py
python packaging\install_native_host.py --browsers firefox
```

然后在 `chrome://extensions`（或 `edge://extensions`）开启开发者模式，加载 `browser\extension` 目录；Firefox 在 `about:debugging` 临时载入 `browser\firefox\manifest.json`。右键菜单与扩展弹窗即可把下载交给 PDM；详见 [`docs/browser-integration.md`](docs/browser-integration.md)。

### 打包与安装

```bat
python -m pip install pyinstaller
packaging\build.cmd
```

产物：`dist\pdm-gui\pdm-gui.exe`（便携版，onedir，约 159 MB）、
`dist\pdm-cli\pdm.exe`（约 77 MB）、`dist\pdm-host\pdm-host.exe`
（浏览器 Native Messaging 宿主，约 37 MB）。2026-11-08 全量重建了 GUI/CLI/Host 与安装包（含 MSE 大内容、Firefox popup UI、更新后的 Chromium 扩展）。

需要带开始菜单/卸载项的安装包时，安装 Inno Setup 6 后执行：

```bat
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\pdm.iss
:: -> dist\pdm-setup-0.1.0.exe
:: 然后重打三个 zip、SHA256SUMS.txt 与 latest.json：
python packaging\make_release_assets.py --version 0.1.0 --base-url https://downloads.example.com/personal-download-manager/
```

安装包按用户安装（无需管理员），带开始菜单/卸载项；2026-11-08 静默安装/卸载到临时目录通过（`/TASKS=` 未选浏览器集成，现有 Host 注册未改）；随后已用 `install_native_host.py --browsers chrome,edge,firefox` 将注册 Host 刷新为新 dist 版本（哈希一致）。

2026-11-08 发布产物（全量重建，含 MSE 大内容、Firefox popup UI、更新后的 Chromium 扩展）：

| 文件 | 大小（B） | SHA-256 |
|---|---:|---|
| `dist/pdm-setup-0.1.0.exe` | 92,149,633 | `d9f56ff4cacadcedbb5cec7e847db1308cda6c31878982a7a9bcbe04300e921e` |
| `dist/pdm-gui-0.1.0-win64.zip` | 70,024,916 | `38d3cb24bfc11d5256239a9b16dc722df1e2f46cfd1aade4157c049ae88188d3` |
| `dist/pdm-cli-0.1.0-win64.zip` | 36,871,093 | `26a4676d5025886f9bf5d81b1bfd7708c4c7b5a0c36ce2a8c118a856b1cb674f` |
| `dist/pdm-host-0.1.0-win64.zip` | 17,279,190 | `7827e7ceb22356d69765b741a20782effc409cceefebc73db1d2b184411986a1` |
| `dist/latest.json` | 393 | 安装包同哈希（size 92,149,633） |

细节与注意事项见 [`packaging/README.md`](packaging/README.md)。

### 自动更新

更新是**用户主动触发**的：启动时不检查，下载后必须通过 SHA-256，运行安装器前还有一次确认。

```bat
:: 只检查
pdm.cmd update --manifest https://downloads.example.com/personal-download-manager/latest.json

:: 下载并校验到指定目录
pdm.cmd update --manifest https://…/latest.json --download D:\updates

:: 下载、校验并启动安装器（显式 opt-in）
pdm.cmd update --manifest https://…/latest.json --launch
```

GUI 在“设置 → 更新清单”填写 HTTPS 清单 URL，然后“帮助 → 检查更新…”。清单格式、发布流程与安全边界见 [`docs/updating.md`](docs/updating.md)；`packaging\make_update_manifest.py` 可从构建好的安装包生成并校验清单。

### CI 与发布

- `.github/workflows/ci.yml`：Windows + Python 3.13/3.14 矩阵，安装 `.[dev]`、校验版本一致性、编译、校验 PowerShell 签名脚本语法、运行全部 392 项测试并上传 JUnit。
- `.github/workflows/release.yml`：push tag `vX.Y.Z`（或手动指定版本）→ 构建三个 onedir 便携版 → Inno Setup 安装包 → 可选 Authenticode 签名 → zip/SHA256SUMS → 生成 `latest.json` → 发布 GitHub Release 资产。
- 版本由 `packaging/check_version.py` 强制三处一致；签名通过 `WINDOWS_CERT_PFX_BASE64` / `WINDOWS_CERT_PASSWORD` secrets 启用，没有证书时发布未签名产物并给出 warning，不伪造签名。
- 当前目录尚不是 git 仓库，workflow 已就绪但未在远端执行；本地已跑通 dry-run：PyInstaller 构建、打包 CLI/GUI 冒烟、Inno `/DAppVersion`、`latest.json`、zip + SHA256SUMS，以及打包 CLI 的更新下载校验。无代码签名证书，签名步骤只做了语法验证。本地复现见 [`docs/ci.md`](docs/ci.md)。

---

## 3. 架构

```
                     ┌──────────────────────────────┐
   URL ─────────────▶│ probe.py                     │
                     │ GET Range: bytes=0-0         │
                     │ 206 / 200 / 416 / 3xx        │
                     └──────────────┬───────────────┘
                                    │ total, ranges, ETag, filename
                                    ▼
                     ┌──────────────────────────────┐
                     │ downloader.py                │
                     │ 初始分段 / 恢复日志           │
                     │ DownloadState（锁 + 动态拆分）│
                     └───────┬───────────┬──────────┘
                             │           │
              ┌──────────────▼──┐   ┌────▼──────────────┐
              │ worker 0        │   │ worker N-1        │
              │ 独立 httpx.Client│   │ 独立 httpx.Client │
              │ Range: bytes=a-b│   │ Range: bytes=c-d  │
              └───────┬─────────┘   └────────┬──────────┘
                      │  os.lseek + os.write（各自 fd，按绝对偏移）
                      ▼                      ▼
              ┌─────────────────────────────────────────┐
              │ 预分配 .pdm.part（单个文件，无合并）  │
              │ 原子 .pdm.json（每个分段 start/end/pos）│
              └──────────────────┬──────────────────────┘
                                 │ 完成 + 校验
                                 ▼
                          rename 为最终文件
```

关键设计：

| 方面 | PDM 实现 |
|---|---|
| 分片落盘 | 预分配单文件，按绝对偏移写入，无合并阶段 |
| 续传状态 | 单文件旁 `.pdm.json`，原子替换 |
| 动态拆分 | 连接空闲时重分剩余段；拆分点钳制到存活游标，避免重复下载 |
| 请求编码 | `Accept-Encoding: identity` |
| 默认连接 | 默认 8，可配置 1–64 |
| 校验 | 强 `ETag` 的 `If-Range`；无 ETag 时退回 `Last-Modified` |
| 配置 | CLI 参数；GUI 使用 `%APPDATA%\PersonalDownloadManager\config.json`（不写注册表） |

---

## 4. 测试与验证

### 4.1 自动化测试

```bat
cd D:\studyfile\claude_code\PDM
python -m pytest -q
```

本机结果（2026-11-08，MSE WebM 实机验证之后）：

```
392 passed in 132.17s
```

Node 位于 `D:\Application\tools\node-v24.22.1`；把它加入 Python 的 PATH 后，扩展 JS 的
`node --check` 会实际执行。不加入 PATH 时为 `389 passed, 1 skipped`（跳过 Node 语法检查）。

覆盖内容：

- `tests/test_headers.py`：`Content-Range`、`Retry-After`、RFC 5987 文件名、Windows 文件名净化、大小解析。
- `tests/test_probe.py`：206/200/416、重定向、Content-Disposition、HEAD 回退、认证、404。
- `tests/test_download.py`：8 段并发字节级一致、无 Range 回退、chunked、自动命名、重定向、Basic Auth、空文件、SHA-256、206 缺 `Content-Range` 拒绝、非 identity 编码拒绝。
- `tests/test_resume.py`：取消后续传、`--restart`、ETag 变化强制重下；检查续传请求确实从非零偏移开始。
- `tests/test_retry.py`：503 重试、连接中断后从断点重试。
- `tests/test_dynamic.py`：动态分段确实产生更多分段；关闭后严格保持初始分段数。
- `tests/test_limit.py`：全局限速的实际耗时下界。
- `tests/test_cli.py`：probe/download CLI、非法头、`--version`。
- `tests/test_gui.py`：配置钳制与读写、模型过滤/中断状态、GUI 队列端到端下载、暂停后续传。
- `tests/test_scheduler.py`：错误可重试性分级、退避与上限、定时开始（到点才下载）、继续/暂停清理计划、瞬时错误自动重试后完成、404 不重试、重试预算耗尽、调度状态持久化。
- `tests/test_schedule_repeat.py`：每天/每周/每隔 N 天规则的下一次时刻（含边界、周一=0、锚点对齐、未来锚点、N=1）、非法规则与天数/锚点拒绝、任务序列化、完成后与失败后的自动重排、重启后过期重复计划向前滚动、重复任务覆盖上一轮文件、添加/定时对话框。
- `tests/test_queues.py`：队列配置清理、按队列调度、任务移动、未知队列回退、带宽时段解析/跨午夜与主窗口应用。
- `tests/test_adaptive.py`：AIMD 加性增/乘性减、冷却、RAM 上限、503 退避与共享客户端下载。
- `tests/test_mirror.py`：镜像兼容性、轮询分配、失效切换、旧日志重映射与双镜像下载。
- `tests/test_large_list.py`：2 万行模型的 id→行索引在批量更新/删除后仍正确、代理过滤与排序、视图只绘制可见行、进度更新不随列表长度线性变慢。
- `tests/test_policy.py`：代理分组解析（映射/列表两种形式、去重）、`proxy` > `proxy_group` > 默认代理的优先级、未知组回退、JSON 往返、CLI `--policy` 与 `--max-host-connections` 组合。
- `tests/test_categories.py`：默认扩展名分类、未知扩展名回退、自动/不分类/指定分类、关闭分类、子目录净化（不能逃出下载根目录）、分类表去重。
- `tests/test_urlbatch.py`：数字/字符/步长/零填充、`{a,b}` 备选、多行与注释、去重、展开上限、非法范围。
- `tests/test_site_policy.py`：站点 UA/Cookie/Basic 认证确实出现在探测与每个分段请求上、规则覆盖任务值、未设置项沿用任务值、CLI `--policy` 端到端。
- `tests/test_batch_cli.py`：`batch` 展开并逐个下载、分类子目录、非法模式/未知分类的退出码。
- `tests/test_http2.py`：本地 TLS+h2 服务器的 ALPN 协商、8 个分段共用一条连接（单连接多流）、字节级完整、`--http2` 在明文 HTTP/1.1 服务器上的退化、HTTP/1.1 TLS 基线每 worker 一条连接，以及基准所需的连接号/ALPN/请求结束时间元数据。
- `tests/test_hls.py`：master/媒体列表、`EXT-X-BYTERANGE`/`EXT-X-MAP`、相对 URI、标准 AES-128（显式 IV、media-sequence 推导 IV、密钥轮换、METHOD=NONE、错误密钥长度）、SAMPLE-AES/DRM 拒绝、直播拒绝、并发/取消/进度/SHA-256。
- `tests/test_dash.py`：ISO-8601 时长、`SegmentTemplate` 编号/时间线/重复/继承、`SegmentList`、嵌套 BaseURL、轨道选择、直播/加密拒绝、端到端拼接与站点凭据、CLI、双轨下载、ffmpeg 路径解析/合并/无 ffmpeg 回退、**多 Period 选择与逐 Period 初始化分片拼接**、**SegmentBase SIDX 解析与 Range 下载**、无 indexRange 整文件回退。
- `tests/test_browser_host.py`：原生消息分帧、捕获文件、扩展清单与 ID、Firefox MV3 清单/同步、页面内 m3u8/mpd 扫描（iframe/shadow/MSE 提示）、DASH 交接、手动验证页、Node 语法检查（无 Node 时跳过）、**MSE 多轨会话/分块/可配置上限与磁盘余量/DRM 拒绝/WebM 命名/ffmpeg 合并/文件名净化/完成记录 manifest/扩展接线与 fMP4 音频+视频 fixture/Firefox Gecko base64 分支与 128 版本门槛**。
- `tests/test_capture_archive.py`：归档纯函数（同卷原子移动、唯一命名、大小/SHA 校验、跨卷复制失败清理、取消）、设置与对话框、管理器成功/失败/删除等待/重启续传/关闭开关、主窗口自动归档。
- `tests/test_capture_cleanup.py`：合并归档后自动删源轨（默认关闭）、未合并不删、手动清理保留主文件、越界路径保护、归档中拒绝、主窗口菜单动作。
- `tests/test_mse_capture.py`：MSE 完成记录解析（合并优先、最大视频回退、越界/缺失文件拒绝）、完成任务的字段与“已捕获”标签、队列插入不启动 worker、删除任务清理所有轨道、主窗口导入去重。另做打包级端到端冒烟：`pdm-host.exe` 写入两轨完成记录，`pdm-gui.exe`（临时 APPDATA）导入为 1 条“已捕获”任务并消费记录；**真机复验（2026-11-07）**：Edge 156 隔离 headless + DASH-IF/Akamai 捕获 2 轨并合并（1,153,622 B），GUI 自动导入、属性/删除均通过。
- `tests/test_http3.py`：真实 TLS+QUIC 测试服务器的 ALPN `h3`、8 段共用一条连接、字节级完整、探测元数据、站点 UA/Cookie/Referer、HLS 清单+分片共用一条连接、2% 丢包重传后仍完整、代理/明文 URL 拒绝、CLI `--http3` 与互斥参数。
- `tests/test_ftp.py`：pyftpdlib 服务器上的字节级下载、SIZE/MDTM 探测、取消后 `REST` 续传、错误密码/缺失文件、显式 FTPS、SHA-256、代理拒绝、FTP/FTPS 主动模式（PORT/EPRT、多连接）、CLI `--ftp-active` 与非法组合拒绝。
- `tests/test_sftp.py`：paramiko SFTP 服务器上的下载/探测、取消后 `seek` 续传、密码与私钥认证、未知主机拒绝 / known_hosts 接受、缺失文件、SHA-256、代理拒绝、CLI。
- `tests/test_update.py`：版本比较、清单字段/HTTPS/同源校验、检查更新、流式下载与 SHA-256/大小校验、取消清理、安装器启动校验、CLI `update` 的 JSON/下载/启动、清单生成工具；GUI 侧覆盖设置持久化与更新对话框状态。
- `tests/test_release.py`：`pyproject.toml` / `__init__.py` / Inno Setup 三处版本一致性、tag 期望版本、缺失文件、CLI `--version` 与包版本同源。
- `tests/test_i18n.py`：英文目录覆盖所有 GUI 中文串、语言解析/持久化、重复计划英文描述、状态列/表头/工具提示、更新对话框、添加/设置对话框、主窗口菜单/标题，以及 Show 事件自动翻译。
- `tests/test_theme.py`：macOS 浅/深色调色板与样式、表单右对齐/无边框表格、状态色 tone、全部线性图标有实际像素、主窗口圆角卡片与表格参数。
- `tests/test_shutdown.py`：关机策略（空列表/全部完成/失败/暂停/计划/重试/未知状态）、平台命令与无 shell 执行、倒计时长度设置、队列管理器的一次性倒计时/取消/新增任务取消/失败上报/真实下载后触发、主窗口开关与倒计时标签。关机命令在测试中始终注入记录器，不会真的关机。

测试服务器 `tests/server.py` 只依赖标准库，支持：单区间 206、`If-Range`、忽略 Range、chunked、每连接限速、丢连接、503 风暴、认证、重定向、`Content-Disposition`。

### 4.2 公网一致性验证

DASH 公网验证（2026-10-30）：`https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd` 的最低码率视频轨（320x180）共 160 个分片，4 连接下载完成 15.3 MiB、0 重试；MPD 解析出 1 个 Period、11 条轨道。

DASH 双轨合并（2026-11-01）：用外部 ffmpeg 生成 4 秒本地 DASH（视频 mpeg4 + 音频 AAC），`pdm dash --track both --ffmpeg <ffmpeg>` 完成后输出可播放 MP4；ffmpeg 4.3.1 输出 84,308 字节，ffmpeg 9.0.2 输出 84,392 字节，`ffprobe`/`ffmpeg -i` 均显示 mpeg4 视频流 + AAC 音频流且解码通过。同一夹具在 PATH 无 ffmpeg 时输出 `fallback.mp4` + `fallback.audio.m4a` 两个文件并给出提示。

对 `https://proof.ovh.net/files/10Mb.dat` 分别用 1、8、16 连接下载，SHA-256 完全一致：

```
e5b844cc57f57094ea4585e235f36c78c1cd222262bb89d53c94dcb4d6b3e55d
```

该链路服务器/运营商总带宽约 1.5 MiB/s，多连接没有加速（16 连接因握手开销略慢到 1.2 MiB/s）。这说明引擎没有制造虚假性能：**当瓶颈是总带宽时，多连接不会变快**。

### 4.3 本地可复现基准

```bat
python bench/local_bench.py
```

本机结果：

```
Scaling: 32 MiB file, 4 MiB/s per connection
 conns   elapsed        speed  peak  parts
     1     8.19s      3.91 MiB/s     1      1
     2     4.10s      7.80 MiB/s     2      2
     4     2.10s     15.21 MiB/s     4      4
     8     1.10s     29.21 MiB/s     8      8
    16     1.62s     19.76 MiB/s    16     16

Dynamic segmentation: 16 MiB, 4 connections, one fast + three 2 MiB/s
        mode   elapsed        speed  parts
      static     2.08s      7.69 MiB/s      4
     dynamic     1.63s      9.84 MiB/s      7
```

解读：

- 1→8 连接接近线性，证明分段与并行写入正确；
- 16 连接在“Python 测试服务器 + 回环 + 每连接限速”的组合下出现回退（GIL/线程调度/握手），不是引擎并发上限问题；
- 动态分段在连接速度不均时把 2.08s 降到 1.63s（约 **22%**），并把分段数从 4 提升到 7——这正是动态分段要解决的场景。

### 4.4 外部工具对照

外部工具的对照数据与原始结果不随公开仓库发布；本项目只保留自身的本地/公网基准。

### 4.5 自适应并发（AIMD）基准

`python bench/adaptive_bench.py`：本地服务器每连接 4 MiB/s，**同时最多接受 3 个分段请求**，超出的返回 503 + `Retry-After: 1`；16 MiB，3 次中位数：

```
          mode  elapsed  retries       503s  peak  final
    fixed -n 8    3.28s       10         10     8      8
      adaptive    2.70s        1          1     4      2
```

- 固定 8 连接：5 条连接持续被拒并重试，耗时 3.28s、10 次 503；
- AIMD：从 2 条起，涨到 4 时被拒一次，立即减半到 2 并完成，2.70s、1 次 503；
- 结论只针对“服务器有明确并发上限”的场景；服务器不限并发时 AIMD 会在起步阶段略慢于固定 8。

HTTP/2 用公网验证：`proof.ovh.net` 协商到 `HTTP/2`，8 段下载 SHA-256 与单连接一致（`e5b844cc…`）。本地 `tests/h2server.py` 现在提供真实 TLS+h2 服务器，`tests/test_http2.py` 覆盖 ALPN 协商、单连接多流与完整性；基准见 4.8。

### 4.6 多源镜像基准

`python bench/mirror_bench.py`：两个本地服务器各托管同一个 32 MiB 文件、每连接 2 MiB/s，并用主机策略把每个主机的连接数限制为 4（第二个服务器用 `localhost` 访问，以便被视为不同主机）。3 次中位数：

```
          mode   elapsed        speed
 single source     4.10s      7.80 MiB/s
   two mirrors     2.13s     15.04 MiB/s
```

单源受“每主机 4 连接 × 2 MiB/s”限制在约 8 MiB/s；分段轮询到两个主机后接近线性翻倍。这正对应现实里的镜像/多 CDN 场景。没有主机级限制时（只有每连接限速），镜像本身不会让回环更快。

### 4.7 HLS 分段并发基准

`python bench/hls_bench.py`：8 个 512 KiB 分片、每连接 512 KiB/s，3 次中位数：

```
 connections   elapsed
           1     8.16s
           4     2.17s
```

顺序下载每个分片 1s，共约 8s；4 个分段并发后约 2.2s。测试服务器只提供普通 HTTP，所以基准覆盖的是“分片并发 + 拼接”这条链路，不涉及真实编码器。

AES-128 解密在分片落盘后由 `cryptography` 的 AES-CBC/PKCS#7 完成，属于 CPU 工作；本地测试覆盖显式 IV、media-sequence IV、密钥轮换与 `METHOD=NONE`，没有单独的吞吐基准（正确性由 `tests/test_hls.py` 断言）。

### 4.8 HTTP/2 多路复用基准

`python bench/http2_bench.py`：本地 TLS 服务器，16 MiB、每请求 50 ms 延迟、每流 2 MiB/s，3 次中位数。两个服务器使用同一张自签名测试证书；h1 侧是 `TestServer(tls=True)`，h2 侧是 `tests/h2server.py`（真实 TLS + h2，可统计连接与并发流）：

```
      mode   elapsed        speed  conns  streams
   h1 -n 1     8.17s      1.96 MiB/s      1        1
   h1 -n 8     1.49s     10.71 MiB/s      8        1
   h2 -n 8     1.22s     13.11 MiB/s      2        8
```

- `conns` 是传输阶段实际使用的 TCP/TLS 连接数（h2 的探测与传输各一条）；`streams` 是单连接上的并发流数。
- 同样的 8 段并发，h1 需要 8 条连接，h2 只需 1 条（加探测 1 条），耗时少约 18%。
- 不加限速时（`--throttle none --size 32M --latency 0.02`）：h1 -n 8 46.70 MiB/s，h2 -n 8 71.50 MiB/s，h1 -n 1 140.61 MiB/s。单连接（h1 -n 1）在回环上最快；h2 的价值在于用更少的连接换取更高并发。
- 绝对值受 Python 测试服务器（线程模型/GIL）限制；可复现的结论是同一套服务器下 h2 的连接数与并发流行为，以及延迟/限速场景下的相对差异。

自动化测试 `tests/test_http2.py` 现在用本地 h2 服务器验证：ALPN 协商到 h2、8 段共用一个连接（`max_streams_per_connection >= 4`）、完整性一致，以及 `--http2` 在明文 HTTP/1.1 服务器上自动退化。服务器还会记录每条请求的连接号、ALPN 和结束时间，便于自动化核对。

### 4.9 大列表（虚拟化）压力测试

`python bench/list_bench.py`：offscreen Qt、`QTableView` + 进度委托、每档 5 次取中位数。列表行是 70% 完成 / 20% 暂停 / 10% 下载中，进度更新每次改 200 个随机任务：

```
   rows   build   paint  update  sorted  sort  filter  remove100  save      MB
   1000     2.7     8.8     1.7     8.0  42.5     5.4        0.7   2.8
   5000    13.5     9.1     1.9    11.9 247.0    19.2        2.0  16.4
  20000    60.6     8.9     1.9    15.9 1229.6    71.8        7.3  70.9
```

（单位 ms；`save` 是 `downloads.json` 的序列化耗时，右列是文件大小；Python 堆峰值 1k/5k/20k 分别为 0.7/3.4/13.7 MiB。）

- **绘制与行数无关**（约 9 ms）：表格只委托绘制可见的 ~18 行，20 万行也不会逐行建控件——这正是虚拟化的意义。
- **进度更新与行数无关**（200 次 1.7–1.9 ms）：模型维护 `任务 id → 行号` 索引，并且只对受影响的列发 `dataChanged`。优化前同样 200 次更新在 1k 行时 4.1 ms、20k 行时 64.8 ms（内部基准结果（未公开）），是典型的 O(行数) 扫描。
- **批量删除**走一次 `remove_rows`：20k 行删 100 行 7.3 ms（优化前逐行线性删除为 5.5 ms，量级相同）。索引维护让“零散单行删除”约为 1.6 ms/行，所以 UI 的批量删除与“清除已完成”都改走一次批量路径，避免 N 次索引重建。
- **排序**由 `QSortFilterProxyModel` 完成，是一次性的 O(n log n) 操作：20k 行按状态排序约 1.2 s。这里保留 Qt 的动态排序以保证状态变化时行会移动；进度更新在已排序状态下每次约 0.08 ms（200 次 15.9 ms），不影响下载。
- **持久化**：20k 行 `downloads.json` 约 9.8 MiB、写入约 71 ms（防抖 0.7 s，不在进度回调里触发）。

自动化回归：`tests/test_large_list.py` 用 2 万行验证索引正确性、过滤/排序、视图绘制，以及“20k 行的 1000 次更新不得比 1k 行慢 5 倍以上”的宽松上限。

### 4.10 HTTP/3（QUIC）与丢包

`python bench/http3_bench.py`：本地 TLS 的 h1 / h2 / h3 三套服务器，16 MiB、每请求 50 ms 延迟、每流 2 MiB/s，5 次中位数（内部基准结果（未公开））：

```
      mode   elapsed        speed  conns  streams
   h1 -n 8     1.49s     10.73 MiB/s      8        1
   h2 -n 8     1.22s     13.17 MiB/s      2        8
   h3 -n 8     4.31s      3.71 MiB/s      2        8
  h3 -n 16     4.57s      3.50 MiB/s      2       16
```

- `conns` 为探测 + 传输实际使用的连接数：h3 的 8/16 个分段都在**一条** QUIC 连接上（另加探测一条），h2 同理共享一条 TCP/TLS 连接，h1 需要 8 条。
- 干净回环上 h3 明显慢于 h2（约 3.7 vs 13 MiB/s），16 流也不比 8 流快：**瓶颈是 aioquic 的纯 Python 包处理（加解密与事件循环），不是并发或引擎**。不计限速时（`--throttle none`，3 次中位数）h1 21.9、h2 34.2、h3 3.7 MiB/s；h3 单流约 6 MiB/s、8 流降到约 3.7（事件循环 CPU 争用），结论相同。不同机器/负载下绝对值会波动，连接数与流数是可复现的部分。
- QUIC 的价值在丢包/弱网，而不在无丢包回环。用 `tests/udp_proxy.py` 在客户端与 QUIC 服务器之间按比例丢 UDP 包（双向），`python bench/h3_loss_bench.py`（4 MiB、4 段、3 次中位数，内部基准结果（未公开））：

```
  loss   elapsed        speed  dropped  dropped %   sha
  0.0%     1.10s      3.64 MiB/s        0      0.00%    ok
  1.0%     1.92s      2.08 MiB/s      143      1.03%    ok
  3.0%     4.11s      0.97 MiB/s      470      3.09%    ok
  5.0%     6.20s      0.65 MiB/s      816      5.04%    ok
```

丢包升高时吞吐平滑下降但 **SHA-256 始终正确**（QUIC 重传、引擎 0 次整段重试）。没有做“TCP 同等丢包”的对照：用户态无法公平地给 TCP 字节流注入丢包；这里证明的是 HTTP/3 路径在弱网下的**正确性与可用性**，不是回环速度。

### 4.11 FTP / SFTP 吞吐

`python bench/remote_bench.py`：本地 pyftpdlib / paramiko 服务器、64 MiB 文件、3 次中位数，比较“原始客户端”（`ftplib.retrbinary` / `paramiko.get`）与 PDM 的驱动（`.pdm.part`/journal、进度采样、限速桶、SHA 校验），每次都校验 SHA-256：

```
                mode   elapsed        speed   sha
      ftp raw ftplib     0.06s   1004.46 MiB/s    ok
   PDM ftp -n 1     0.07s    962.41 MiB/s    ok
   PDM ftp -n 8     0.70s     91.07 MiB/s    ok
   sftp raw paramiko     0.53s    120.88 MiB/s    ok
       PDM sftp     0.66s     97.62 MiB/s    ok
```

- 这里是**无丢包、无每连接限速的回环**：`-n 8` 的 FTP 要付 8 条控制连接/线程与分段的固定成本，反而比单流慢；FTP 多连接的价值在“服务器按连接限速”的场景。
- 单流开销：FTP `-n 1` 接近原始 `ftplib`；SFTP 约 19%（journal/进度/限速/哈希/paramiko 调用），绝对值随机器波动，SHA 全对。
- SFTP 只做本地吞吐基准。
- FTP 主动/被动模式只改变数据连接的建立方向（PORT/EPRT vs PASV/EPSV），数据读写路径相同，因此没有单独的性能基准；正确性与实际协商模式由 `tests/test_ftp.py` 覆盖。

---

## 5. 互操作性说明

本项目只实现公开协议并用于互操作性研究；不包含第三方软件的二进制、注册/授权逻辑、私有服务协议或 DRM 绕过。

## 6. 已知限制

- GUI 已具备队列并发、托盘、剪贴板监听（默认关闭）、单任务定时开始、每天/按星期/每隔 N 天重复计划、失败重试队列、多队列、带宽时段与分类目录；浏览器接管依赖 Chromium/Firefox 扩展（需手动加载）。2026-11-01 已在真实 Chrome 156 验证：页面/iframe/shadow DOM 扫描、HLS（含 AES-128）/DASH/MP4 经 Native Host 入队并完成，接管下载也已验证（Chrome 下载 CANCELLED、0 字节，PDM 完成且 SHA-256 一致）；HLS master 双码率候选也已验证（分别 786,432 / 262,144 字节，SHA-256 与服务器一致）；右键菜单与 Firefox 159 实机已验证（Chrome/Firefox 右键 `plain.mp4` 均完成、262,144 字节、SHA-256 一致）。见 [`docs/browser-verification-2026-11-01.md`](docs/browser-verification-2026-11-01.md)、[`docs/browser-verification-2026-11-02.md`](docs/browser-verification-2026-11-02.md) 与对应 内部基准结果（未公开）*.json`。Cookie 转发（Chrome 156）与 Firefox 159 接管下载也已验证；页面扫描仍不捕获 Blob URL。MSE 实验捕获已支持多轨未加密 fMP4/WebM、默认每轨 8 GiB、会话 16 GiB（可配置）+ 1 GiB 磁盘余量保护、可选 ffmpeg 合并，fMP4 与 WebM 均已实机验证；公开站点矩阵覆盖 DASH-IF 的 3 个公开 CDN 素材 + 本地 WebM 控制，hls.js demo 因外部依赖在隔离环境 blocked；单轨 Chrome/Edge 手动验证通过，多轨已在 Edge 156 本地夹具、DASH-IF/Akamai 真实站点以及 Chrome for Testing 157（headless）的正常/重置复验中通过；Firefox 159 的 MAIN-world 注入已用 WebDriver BiDi 探针 + 本地 WebM/严格 CSP 页验证（2 轨/4 chunks/48,321 B），完整 background→Native Host→GUI 端到端已验证（WebM 合并 47,971 B、fMP4 合并 31,589 B、DASH-IF/Akamai BBB 合并 4,189,997 B，GUI 归档 SHA-256 一致）；本地 hls.js 第二播放器已验证（muxed 242,020 B，GUI 归档一致），公网 hls.js demo 因 jQuery/googleapis 不可达默认 blocked（测试专用 jQuery shim 下已跑通 5,506,934 B；webRequest 重定向亦通过（2,730,802 B））；popup 页面按钮已验证；工具栏 action popup 与商业 DRM/直播未复测；Shaka Player demo（appspot）不可达；停止路径已加超时并在受控 MediaSource 重置下验证（Chrome 0.093s、Edge 0.102s 返回），检测到 EME/DRM 立即停止且不尝试解密。branded Chrome 157 仍不支持命令行加载未打包扩展，自动化复验使用官方 Chrome for Testing。闭 shadow root、DRM、私有播放器仍无法处理。
- 重复计划支持“每天”“按星期几”“每隔 N 天”（N=1–365），不支持“每月/每年/一天多个时间点”；重复时间按本地钟表时间计算。“每隔 N 天”以创建规则时所选日期为锚点（`anchor + k*N`），编辑时间不会改变锚点；要重新锚定请“取消计划”后重设。完成后或重试耗尽后都会排到下一次，失败原因保留在提示里。普通“暂停”保留重复规则（继续后下次完成仍会重排），在“定时开始…”里点“取消计划”才会同时关闭重复。
- 自动重试只覆盖瞬时错误；认证/代理/URL 改好后点“继续”即可，手动继续会重置重试预算。
- “所有下载完成后关机”是**会话开关**，不写入配置：重启程序后必须重新开启。只有全部任务处于终态（完成/失败/取消）且至少一个完成时才倒计时；暂停、等待、下载中、计划和待重试任务都会阻止关机。倒计时期间新增或暂停任务会取消本次倒计时（开关保持开启），关机命令执行失败会提示并自动关闭开关。直接结束进程（包括“退出”）不会触发关机。
- MSE 捕获默认在后台归档：合并文件（无合并时为主轨）移动到 `下载目录/分类`，源轨默认仍留在 `%APPDATA%\PersonalDownloadManager\capture\mse\`；归档期间状态显示“归档中”，失败会保留源文件并在悬停/属性/托盘提示。关闭“MSE 捕获自动保存到下载目录”后，主文件也留在捕获目录。只有带 `mse-session-*.json` 完成记录的新捕获会被导入，旧版本已经落盘的文件不会回填；中断的归档（源文件仍在时）会在下次启动续传。任务状态内部是 `completed`，不参与“下载完成后关机”的判定。跨卷复制会校验大小与 SHA-256，期间退出程序会取消复制并保留源文件（`.pdm-archive.part` 会在下次尝试时覆盖）。
- 源轨清理：自动清理仅对**合并成功且已归档**的捕获生效，且默认关闭；未合并、归档失败或关闭归档时不会自动删除。手动“清理捕获源轨…”只删除 `capture/mse` 内的源轨（保留当前主文件/归档文件），删除前显示数量与体积并需确认；其他任务正在引用的文件不会被删除。清理是不可恢复操作。
- 分类按**最终文件名**的扩展名判断；HLS 输出为 `.ts` 时会进入“视频”子目录。没有匹配扩展名的文件留在下载根目录。CLI 的 `--category` 默认用内置表，可传 `--categories FILE` 使用自定义表。
- 通配批量展开器上限 10000 个 URL（GUI 批量对话框单次 5000）；只支持 `[数字范围]`、`[字母范围]`、`{备选}`，不支持裸 `*`。`batch` 逐个子下载，不并行排队。
- 站点规则按请求主机生效（主地址、重定向后的主机、镜像主机、HLS 分片主机各自匹配）；同一任务跨主机时可能出现多套凭据，属预期行为。
- 代理分组只保存“名称 + 代理 URL”；没有按分组统计流量或健康检查。规则同时填 `proxy` 与 `proxy_group` 时 `proxy` 优先（GUI 会提示）。
- 大列表（2 万行）已做压力测试：绘制与进度更新是常数开销，但**按列排序是一次性 O(n log n)**（20k 行约 1.2 s，Qt 代理排序）；日常几十到几百个任务无感。
- 便携版是 **onedir**（非单文件）：启动快、误报少，但目录较大（GUI 约 159 MB）。安装包 `dist\pdm-setup-0.1.0.exe`（92,149,633 B，SHA-256 `d9f56ff4…921e`）已于 2026-11-08 重新构建（含 MSE 大内容、Firefox popup UI、更新后的 Chromium 扩展）；2026-11-08 静默安装/卸载到临时目录通过（Host/browser-integration 任务未选，现有 Host 注册未改）。本机无 `signtool`/代码签名证书、不是 git 仓库/无 `gh`，更新清单 URL 仍是示例地址。
- 未做代码签名，首次运行会有 SmartScreen 警告。更新器只做“用户主动检查 + SHA-256 + 二次确认”，不会绕过 SmartScreen；清单托管方若被攻破，仍可发布自己的文件与哈希，发布者身份需要后续代码签名来证明。启动时不会自动检查更新。
- 界面语言支持中文/English，切换后需重启；分类名、队列名等用户数据按原样显示，不随语言翻译。原生文件选择框按钮与系统级对话框跟随操作系统，不跟随应用内语言。
- 默认不读取 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量（保证基准可复现）；需要代理时显式传 `--proxy`。
- 没有完整的浏览器视频抓取器/站点抓取；HLS 支持未加密与标准 AES-128 VOD，DASH 仍只支持未加密内容（已支持单/多 Period 与 SegmentBase/SIDX）；`--track both` 需要外部 ffmpeg 才能合并，未安装时保留视频/音频两个文件。
- FTP 多连接需要服务器同时支持 `SIZE` 与 `REST`（metadata 阶段会探测）；否则自动退回单流，续传仍可用。SFTP 始终是单流（远程 `seek`）。数据连接默认被动模式；`--ftp-active` 使用 PORT/EPRT，要求服务器能主动连接回本机的高位端口（NAT/防火墙可能阻断，且不适用于所有服务器）。FTPS 只支持显式 TLS（`AUTH TLS` + `PROT P`，端口 21），不支持隐式 FTPS 990。普通 FTP 是明文，敏感环境请用 FTPS/SFTP。
- FTP 多连接每个分段会打开一条新的控制/数据连接，并在读完该段后直接关闭（服务器日志会显示 `completed=0`），这是为了避免 RETR 必须读到 EOF 的限制；不支持 REST 的服务器会在状态/错误里提示改用 `-n 1`。
- FTP/SFTP 不支持 HTTP 代理、镜像、HTTP/2/HTTP/3；CLI 对这些组合直接报错，GUI 不会把默认 HTTP 代理套到远程任务上。
- SFTP 依赖 paramiko；默认用系统 known_hosts 严格校验主机密钥，没有记录时任务失败并提示 `--sftp-known-hosts` / `--sftp-auto-host-key`（后者不安全）。私钥口令来自 `--sftp-key-pass` 或 GUI 字段，不读取 ssh-agent。SFTP 读超时（`read_timeout`）会重连并从已写偏移续传。
- 远程协议的 journal 每 1.5 秒保存一次；强制结束进程最多重下约 1.5 秒的数据（不会跳过字节）。
- 单连接且服务器不支持 Range 时无法续传（只能重下）。
- 服务器在下载中途突然忽略 Range：会明确报错并提示 `--connections 1 --restart`，不会静默写入错误数据。
- HTTP/2：`--http2` 时共用一个客户端；只有服务器协商到 h2 才真正多路复用，HTTP/1.1 服务器上等价于普通连接池。本地 TLS+h2 测试服务器已覆盖多路复用；h2 仍是显式开关（保证基准可复现），不自动协商。
- HTTP/2 可信证书对照使用仓库内的测试 CA；`bench/certstore.py` 只改当前用户 Root，且对照结束后已移除。复现时请务必在测量后执行 `remove`，不要长期保留测试私钥的信任。
- HTTP/3：`--http3` 需要可选依赖 aioquic；按主机建立一条 QUIC 连接并承载所有分段。不支持代理（CONNECT-UDP/MASQUE 未实现），也不做 Alt-Svc 自动升级或在 h3 失败时回退到 h2/h1——显式开关、显式失败。aioquic 是纯 Python 实现，无丢包回环吞吐约 3.7–6 MiB/s（8 流/单流，见 4.10）；丢包下仍按 SHA-256 完整完成。HLS/DASH 的 `--http3` 也可用（按分片/清单主机各建一条连接），但没有专门基准。
- `dist\` 中的 GUI/CLI/Host 与安装包已于 2026-11-08 全量重建，包含 MSE 大内容、Firefox popup UI、更新后的 Chromium 扩展。GUI/CLI spec 的 `hiddenimports` 已包含 `aioquic`、`paramiko`（Host 只做 Native Messaging，不打包这些依赖）。源码运行 HTTP/3 需 `pip install -e ".[http3]"`，SFTP 需 `.[sftp]`（或 `.[full]`）；FTP/FTPS 只用标准库。
- 2026-11-08 全量重建：Host exe 4,722,994 B / SHA-256 `83cca27e…da69`；Host zip 17,279,190 B / `7827e7ce…86a1`；安装包 92,149,633 B / `d9f56ff4…921e`；GUI zip 70,024,916 B / `38d3cb24…88d3`；CLI zip 36,871,093 B / `26a4676d…674f`。安装/卸载临时目录冒烟通过；Host/browser-integration 任务未选。
- AIMD 默认关闭（保证基准可复现）；开启后起步阶段会比固定连接数慢。
- 多源镜像要求镜像与主地址返回**相同大小**，且强 ETag 或 Last-Modified 一致；不满足的镜像会被静默丢弃（日志里有原因）。
- 每个镜像主机按自己的站点规则建立客户端（UA/Cookie/认证/代理）；镜像需要不同代理时用主机规则表达，任务级 `--proxy` 只作为所有主机的默认值。
- HLS 支持未加密 VOD 与标准 AES-128（密钥可获取、AES-CBC/PKCS#7，IV 显式或由 media sequence 推导）；SAMPLE-AES / 非 identity `KEYFORMAT` / DRM 与无 `EXT-X-ENDLIST` 的直播列表会直接报错，不做 DRM 绕过。加密 fMP4 的初始化分片只有在 `EXT-X-MAP` 带 IV 时才解密，否则按明文处理。
- HLS 的进度按“已下载字节/分段完成数”显示，列表里的百分比在拼接完成前无法计算（播放列表不含总字节数）。
- DASH 仅支持未加密内容；已支持单/多 Period、`SegmentTemplate`/`SegmentTimeline`/`SegmentList`、单文件 Representation 与 `SegmentBase`+`indexRange`（SIDX）。直播与 `ContentProtection` 会明确报错；层级化 SIDX（`reference_type=1`）暂未实现；`SegmentBase` 没有 `indexRange` 时按整文件下载。单轨可直接下载；双轨用 `--track both` + 外部 ffmpeg 合并，没有 ffmpeg 时保留两个文件，不做内置 mux。
- 主机策略在 GUI 里修改后只对**新开始**的任务生效；正在下载的任务沿用启动时的策略。
- 队列并发数按队列独立（1–10），所有队列合计的运行任务数 = 各队列并发之和。
- 带宽时段按**每天**生效，不支持按星期几区分；“开始晚于结束”表示跨午夜。
- 全局限速与每下载限速是两层独立的令牌桶，叠加后遵循较严格的一层。
- 16 连接在 Python 测试服务器上会回退（见基准），不是引擎上限。
- 不使用 Windows 注册表保存配置：CLI 只读命令行参数（不持久化），GUI 的配置保存在 `%APPDATA%\PersonalDownloadManager\config.json`。

---

## 7. Roadmap（后续方向）

已完成（V1 → V2 第一版）：

1. **引擎 + CLI**：多连接、动态分段、断点续传、重试、代理、限速、校验。
2. **GUI + 队列**：下载列表、实时进度、暂停/继续/删除、队列并发、托盘、主题、状态持久化。
3. **调度器 + 失败队列**：单任务定时开始、瞬时错误指数退避自动重试、永久错误快速失败。
4. **按主机策略**：每主机连接上限（跨任务共享）、每主机限速/代理、跨任务全局限速。
5. **本地可复现基准**：限速服务器 + 独立进程 + 中位数 + 启动/传输/收尾分段。
6. **自适应并发（AIMD）+ HTTP/2 多路复用**：429/503 退避、按吞吐爬升；h2 单连接多流（本地 h2 服务器 + 基准验证）。
7. **多源镜像**：同 ETag/大小的多个镜像轮询分段、失效切换（aria2 式）。
8. **HLS VOD**：master/媒体列表、BYTERANGE、EXT-X-MAP、标准 AES-128 解密、并发分片与拼接；CLI + GUI，可取消。
9. **打包**：PyInstaller onedir 便携版 + Inno Setup 安装包（含浏览器集成任务）。
10. **多队列 + 带宽时段**：每队列并发数、右键移动队列、按时间段自动调整全局限速。
11. **浏览器集成**：MV3 扩展 + Native Messaging + 目标 URL Cookie 转发 + 可选下载接管。
12. **分类目录 + 站点认证 + 通配批量**：按扩展名自动分类子目录（GUI 可编辑表，CLI 可用 `--categories` 文件）；主机规则支持 UA/Cookie/Basic/Digest，覆盖主地址/镜像/HLS 分片；`[1-10]`/`[a-z]`/`{a,b}` 批量展开（GUI + CLI `batch`）。
13. **技术债 T2**：每天/按星期重复计划（完成后自动重排、重启滚动到下一时刻）；命名代理分组（主机规则用组名复用、三档优先级）；大列表虚拟化压力测试 + 模型 id→行索引、按列 `dataChanged`、批量删除优化。
14. **P5 HTTP/3（QUIC）**：aioquic 传输层、单连接多流、Range/认证/主机策略复用、真实 QUIC 测试服务器、丢包重传基准与自动化。
15. **P6 FTP / FTPS / SFTP**：`REST`/`seek` 续传、共享驱动（journal/重试/限速/SHA）、SFTP 密码/私钥/known_hosts、真实 pyftpdlib/paramiko 测试服务器与吞吐基准。
16. **P0 多连接 FTP**：SIZE/REST 探测、多控制/数据连接分段、动态拆分、按偏移写预分配 `.part`（无合并）、失败/不支持时回退单流；同参数本地基准验收。
17. **P1 调度器“每隔 N 天”**：规则 `{"kind":"interval","days":N,"anchor":"YYYY-MM-DD"}`，按锚点做 `anchor + k*N` 天对齐（本地钟表），GUI 新增“每隔 N 天”+ 天数，持久化/重启滚动/完成与失败后重排全部复用现有队列逻辑。
18. **P1 HTTP/2 本地验证**：本地 TLS+h2 测试服务器、测试证书与自动化基准。
19. **P2 FTP 主动模式**：`--ftp-active`（`DownloadConfig.ftp_active`）通过 PORT/EPRT 让服务器连接回本机数据端口；明文 FTP 与显式 FTPS、单流与多连接分段均支持。默认仍为被动模式；CLI 对 HTTP/SFTP 组合明确报错，GUI 新增对应复选框。
20. **P7 自动更新（用户主动）**：HTTPS 清单 + 数字版本比较、流式 `.part` 下载、大小/SHA-256 校验、原子改名、取消清理、CLI `update` 与 GUI 更新对话框；`packaging/make_update_manifest.py` 生成并验证清单。不做自动检查/静默安装。
21. **P7 CI 构建**：GitHub Actions（Windows + Python 3.13/3.14、版本一致性、测试矩阵）与 tag 发布流水线（PyInstaller、Inno Setup、可选签名、zip/校验和/更新清单、GitHub Release）；本地 `check_version.py`、`sign_windows.ps1` 与 `docs/ci.md`。
22. **P7 界面多语言**：GUI 中/英切换（跟随系统/简体中文/English）、语言持久化、控件树自动翻译、动态文案 `tr()` 模板、重复计划英文描述；覆盖主窗口与全部常用/高级对话框，并新增目录完整性回归测试。
23. **macOS 风格 UI**：浅/深色调色板与 QSS、系统字体、圆角卡片/控件、细滚动条、单色线性图标、圆角细进度条、右对齐表单与标准按钮本地化；保留原生窗口框架。新增主题/图标/布局回归测试。
24. **网页视频补齐**：HLS 标准 AES-128（密钥 URI/IV/轮换/PKCS#7，SAMPLE-AES/DRM 拒绝）；浏览器扩展增加 `scripting` 页面扫描（DOM/内联脚本中的 m3u8/mpd）、手动扫描按钮与 DASH 一键交接；扩展弹窗浅/深色 macOS 风格。
25. **DASH 双轨合并**：`--track both`、GUI“视频+音频（合并）”、`--ffmpeg`/`PDM_DOWNLOADER_FFMPEG`/PATH 探测；有 ffmpeg 时 `-c copy` 输出单个可播放 MP4，无 ffmpeg 时保留两轨并提示。
26. **DASH 多 Period + SegmentBase/SIDX**：逐 Period 选择轨道并插入各自的初始化分片；解析单文件 MP4 的 `sidx`，按字节范围并发下载媒体子段；无 `indexRange` 时整文件下载。
27. **MSE/blob 多轨实验捕获**：MAIN-world `MediaSource`/`SourceBuffer` hook；按 SourceBuffer 分轨（最多 4 轨）、未加密 fMP4/WebM、默认 8 GiB/轨、16 GiB/会话（env 可调）、`changeType` 与 init 嗅探、EME/DRM 拒绝；Native Messaging 分块写入 `capture/mse/`，可选 ffmpeg `-c copy` 合并视频+音频。
28. **下载完成后关机**：`shutdown.py` 纯策略（全部终态 + 至少一个完成；暂停/等待/计划/重试阻止）与平台命令；`QueueManager` 会话级开关、可取消的 1 秒倒计时、可注入关机命令；GUI 菜单开关、状态栏倒计时、非模态确认框与托盘提示；设置持久化倒计时长度（默认 60 秒）。19 项新测试，关机命令从不真实执行。
29. **MSE 捕获接入 GUI 队列**：`mse_end`/`mse_end_all` 写出原子完成记录（轨道、合并文件、源页面/标题）；GUI 轮询后按会话去重，把整个捕获作为一条“已捕获”任务加入队列（合并文件优先，否则最大视频/音频轨）；路径必须位于 `capture/mse` 内；删除任务可清理全部轨道；导入阶段不搬动文件（避免 GUI 线程搬运大文件）。12 项新测试。
30. **MSE 捕获后台归档**：默认开启（设置可关）；合并文件（无合并时为主轨）由 `QThread` 后台移动到下载目录/分类，同卷 `os.replace`、跨卷分块复制 `.pdm-archive.part` + 大小/SHA-256 校验 + 原子改名；归档中状态、失败保留源文件、删除等待线程、退出/重启续传。15 项新测试；打包 Host+GUI 冒烟：两轨（各 5,000 B）导入后主轨归档到 `downloads/视频`，源主轨移走、音频源轨保留、manifest 消费。
31. **MSE 源轨清理**：默认关闭的“归档成功后删除已合并的源轨”仅处理合并成功并归档的捕获；右键“清理捕获源轨…”可对选中捕获手动清理（保留归档/主文件，只删 `capture/mse` 内路径），带数量/体积确认，归档中拒绝执行。8 项新测试；打包冒烟：真实视频/音频 fixture 合并归档后源轨两条删除（释放 32,130 B），主文件保留。
32. ~~**MSE WebM 实机验证**~~：`browser/mse-webm-test.html` + VP9/Opus WebM 素材 + `bench/mse_webm_verify.mjs`；Edge 156 隔离 headless 实机捕获 27,804 B 视频 + 20,510 B 音频，ffmpeg 合并为 47,971 B `.merged.webm`，ffprobe 为 VP9 320×180 + Opus；GUI 归档/源轨清理通过。1 项素材/接线测试。
33. **MSE 公开站点矩阵**：`bench/mse_site_matrix.mjs` 驱动 DASH-IF 参考播放器 3 个公开 CDN 素材（Akamai BBB、Shaka angel-one、Unified Tears of Steel）与本地 WebM 控制页；4/4 可执行用例通过，MP4 合并为 h264+aac、WebM 为 vp9+opus；hls.js demo 因 jQuery 的 ajax.googleapis.com 在本网络 TCP 超时阻塞（`$ is not defined`），记为 blocked。报告 `docs/mse-site-matrix-2026-11-08.md`。
34. **第二播放器 hls.js 本地镜像**：公网 hls.js demo 因 jQuery/googleapis 不可达默认 blocked（测试专用 jQuery shim 下已捕获 5,506,934 B；webRequest 重定向到 cdnjs 亦通过（2,730,802 B））；本地 hls.js 1.6.15 + ffmpeg fMP4 HLS 在 Firefox 159 真实扩展 E2E 中捕获 1 条 muxed 轨（242,020 B，h264 320×180 + aac），打包 GUI 归档 SHA-256 一致；报告 `docs/firefox-mse-mainworld-evaluation-2026-11-08.md`。
35. **公网 hls.js demo + 测试专用 jQuery shim**：测试副本 `content_scripts`/MAIN/document_start 注入 jQuery 2.1.3，绕过 `ajax.googleapis.com` 不可达；demo 正常初始化（5 码率/6 音轨），捕获 2 轨 → 合并 **5,506,934 B**（h264 480×360 + aac，40 s）；另用 `webRequestBlocking` 把 jQuery 302 到 cdnjs，同样通过 → **2,730,802 B**。生产扩展未改动；证据 内部基准结果（未公开）、`...-hlsjs-shaka-angel-fmp4-redirect.json`。
36. **Firefox popup UI / 公网站点**：测试副本用 iframe 加载真实 `popup.html`，点击“开始 MSE 捕获”/“停止并保存”，状态“多轨捕获中”→“已合并”，捕获 2 轨 → 47,971 B；公网覆盖 DASH-IF/Akamai（dash.js）与 hls.js demo（jQuery shim/redirect）；Shaka Player demo（`*.appspot.com`）在本网络不可达。证据 内部基准结果（未公开）。
37. **内部对照**：详细报告与原始数据不随公开仓库发布。

下一步（2026-11-07 更新）：

1. ~~**DASH 音视频双轨 + 可选 ffmpeg 合并**~~（完成：`--track both`、GUI 选项、`--ffmpeg`/`PDM_DOWNLOADER_FFMPEG`/PATH 探测、无 ffmpeg 保留两轨）。
2. ~~有边界的 MSE/blob 多轨实验捕获~~（实现完成：MAIN-world hook、按 SourceBuffer 分轨、未加密 fMP4/WebM、默认 8 GiB/轨、16 GiB/会话（可配置）、DRM 拒绝、Native Messaging 分块落盘、可选 ffmpeg 合并；单轨 Chrome/Edge 手动验证通过，多轨本地夹具 + 真实 ffmpeg 合并验证通过）。Cookie 转发（Chrome 156）与 Firefox 159 接管下载均已验证，见 `docs/browser-verification-2026-11-02.md`。
3. ~~更完整的 DASH 覆盖（多 Period、SegmentBase/SIDX）~~（完成：多 Period 逐段选择/拼接、SegmentBase+SIDX、无 indexRange 整文件回退；层级化 SIDX 与 DRM 仍不支持）。
4. **发布工程收尾**：CI、自动更新、界面多语言均已完成；代码签名按个人使用挂起（需要真实 PFX 证书）。CI 实跑不是签名的硬前提；只有免费 OSS 签名路线才要求公开仓库 + CI。
5. ~~下载完成后关机~~（完成 2026-11-07：会话级开关、可取消倒计时、暂停/计划/重试阻止、可注入关机命令；当时 354 passed）。个人使用的 P1 清单已清空。
6. ~~MSE 输出接入 GUI 队列~~（完成 2026-11-07：完成记录 manifest、GUI 按会话导入为“已捕获”任务、去重/删除/属性、路径越界拒绝；当轮 366 passed）。
7. ~~MSE 捕获后台归档~~（完成 2026-11-07：后台线程、跨卷校验、归档中状态、失败保留源、退出续传；当轮 381 passed）。
8. ~~MSE 源轨清理~~（完成 2026-11-07：默认关闭的归档后清理 + 手动“清理捕获源轨…”，越界保护/归档中拒绝；当轮 389 passed）。
9. ~~MSE WebM 实机验证~~（完成 2026-11-08：本地 VP9/Opus WebM MSE 页，Edge 156 实机捕获、ffmpeg 合并、GUI 归档/清理；390 passed）。
10. ~~MSE 公开站点矩阵~~（完成 2026-11-08：DASH-IF ×3 公开素材 + 本地 WebM 控制，4/4 通过；hls.js demo 因 jQuery/googleapis 不可达 blocked）。下一方向可从 P2/P3 选择，公开发布前处理代码签名/CI 实跑。

---

## 8. 使用边界

- 只下载你有权下载的内容；遵守目标站点服务条款与 robots 策略。
- 本项目不包含、也不会实现任何第三方软件的注册/授权绕过、DRM 规避或私有服务逆向。
- 本项目只实现公开协议，用于学习、测试与互操作性研究。

---

## 9. 目录

```
PDM/
├─ src/personal_download_manager/
│  ├─ cli.py            # 命令行
│  ├─ downloader.py     # 调度、动态分段、重试、单连接回退
│  ├─ probe.py          # 初始探测
│  ├─ client.py         # httpx 客户端与请求头策略
│  ├─ headers.py        # Content-Range/Disposition/Retry-After/文件名
│  ├─ journal.py        # 原子续传日志
│  ├─ categories.py     # 分类目录（扩展名 → 子目录）
│  ├─ hls.py            # HLS/播放列表、BYTERANGE、AES-128
│  ├─ dash.py           # DASH/MPD 解析、多 Period、SegmentBase/SIDX、双轨合并
│  ├─ streaming.py      # HLS/DASH 共用的分片并发/重试/进度
│  ├─ urlbatch.py       # 通配批量 URL 展开
│  ├─ adaptive.py       # AIMD 自适应并发
│  ├─ mirror.py         # 多源镜像校验与分段分配
│  ├─ policy.py         # 主机规则/代理组/连接配额/全局限速
│  ├─ browser_host.py   # 浏览器 Native Messaging 宿主
│  ├─ models.py         # 配置、分段、状态、结果
│  ├─ progress.py       # 进度采样与输出
│  ├─ ratelimit.py      # 全局令牌桶
│  ├─ schedule.py       # 每天/按星期/每隔 N 天的重复计划
│  ├─ http3.py          # HTTP/3（QUIC）httpx 传输层
│  ├─ remote.py         # FTP/SFTP 共用驱动（journal/重试/限速/SHA/收敛）
│  ├─ ftp.py            # FTP / 显式 FTPS（REST 续传）
│  ├─ sftp.py           # SFTP（paramiko；密码/私钥/known_hosts）
│  ├─ update.py         # 用户主动更新：清单/版本/校验下载/启动安装器
│  ├─ shutdown.py       # 下载完成后关机：终态判断、平台命令、无 shell 执行
│  └─ gui/              # PySide6：窗口、模型、队列、工作线程、设置/主题、更新对话框、i18n
├─ browser/             # Chromium MV3 扩展、Firefox 构建、手动验证页
├─ packaging/           # PyInstaller spec、Inno Setup 脚本、签名/清单工具
├─ .github/workflows/   # CI（测试矩阵）与 Release（构建/签名/发布）
├─ tests/               # 392 项测试 + 可控 HTTP/1.1/h2/h3/FTP/SFTP 服务器
├─ bench/               # 本地/公网基准、列表/HTTP/3/丢包/远程协议基准
├─ docs/                # 逆向报告、测试说明、浏览器验证、CI、GUI 截图
├─ pdm.cmd         # CLI 免安装入口
└─ pdm-gui.cmd     # GUI 免安装入口
```
