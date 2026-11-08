# 浏览器集成（Native Messaging）

边界：只做**互操作**——把用户主动触发的下载交给 PDM，并只为**目标 URL**读取 Cookie，以便下载已登录的资源。不读取其他站点/其他 URL 的 Cookie，不做 DRM/授权绕过。支持 Chromium 系（Chrome / Edge / Chromium）与 **Firefox**（同一套 JS，Firefox 使用事件页清单）。

## 1. 组成

| 部分 | 位置 |
|---|---|
| 扩展（MV3，Chromium） | `browser/extension/` |
| 扩展（MV3，Firefox） | `browser/firefox/`（由 `python browser/build_firefox.py` 从共享源码生成） |
| 原生消息宿主 | `src/personal_download_manager/browser_host.py` → `dist/pdm-host/pdm-host.exe` |
| 宿主注册（开发） | `packaging/install_native_host.py` |
| 宿主注册（安装包） | `packaging/pdm.iss` 的 `browserintegration` 任务 |
| 捕获目录 | `%APPDATA%\PersonalDownloadManager\capture\` |
| 扩展 ID | `hfiihggbmdmoofcadgmkingenjgcaend`（由 `browser/extension/manifest.json` 的 `key` 固定） |

## 2. 安装

### 2.1 源码 / 便携版

> 便携版 zip（`dist\pdm-*-win64.zip`）**不包含**扩展目录；扩展只在源码树的
> `browser\extension`（以及 Setup 安装包的 `{app}\browser-extension`）。使用便携版时，
> 请从源码目录加载扩展，或改用安装包。

```bat
cd D:\studyfile\claude_code\PDM
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\pdm-host.spec
python packaging\install_native_host.py --browsers chrome,edge
```

然后：

1. 打开 `chrome://extensions`（或 `edge://extensions`），开启“开发者模式”；
2. “加载已解压的扩展程序”，选择 `browser\extension`；
3. 启动 PDM GUI（它每 1.5 秒扫描捕获目录）。

`install_native_host.py` 会：

- 把整个 `pdm-host` 目录复制到 `%LOCALAPPDATA%\PersonalDownloadManager\native-host\`；
- 生成 `com.pdm.downloader.host.json`（`path` 指向复制后的 exe，`allowed_origins` 只允许上面的扩展 ID）；
- 写入 `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.pdm.downloader.host` 和 Edge 对应键；
- 指定 `--browsers firefox` 时另外生成 `com.pdm.downloader.host.firefox.json`（`allowed_extensions: ["pdm@local"]`）并写入 `HKCU\Software\Mozilla\NativeMessagingHosts\com.pdm.downloader.host`。

卸载：`python packaging\install_native_host.py --uninstall`。

### 2.2 Firefox

```bat
python browser\build_firefox.py
python packaging\install_native_host.py --browsers firefox
```

- `build_firefox.py` 把共享的 `background.js`、`popup.*` 和图标复制到 `browser\firefox\`，并校验清单（`browser_specific_settings.gecko.id`、`background.scripts`，禁止 `service_worker`）。
- Firefox 打开 `about:debugging` → “此 Firefox” → “临时载入附加组件”，选择 `browser\firefox\manifest.json`。
- 接管下载使用 `downloads.onCreated`（Firefox 没有 `onDeterminingFilename`）；右键菜单、Cookie、媒体嗅探逻辑相同。
- 临时载入的扩展在重启后消失；长期使用需关闭 `xpinstall.signatures.required` 后安装打包的 xpi，或使用 Firefox 开发者版。

### 2.3 安装包

在 Setup 里勾选“注册浏览器集成”，安装程序会把宿主、扩展目录和清单一起装到 `{app}`，并写入同样的 HKCU 注册表键（卸载时自动删除）。之后仍需在浏览器里手动加载 `{app}\browser-extension`。

> 静默安装时任务名要**不加引号**：`setup.exe /VERYSILENT /TASKS=browserintegration`。写成 `/TASKS="browserintegration"` 在部分环境会被当成普通参数而忽略。

## 3. 使用

- 右键任意链接 → **“用 PDM 下载此链接”**；
- 点击扩展图标 → **“用 PDM 下载当前页面”**；
- 弹窗里可开启 **“接管浏览器下载”**：Chrome 触发的下载会被取消并转给 PDM（默认关闭，因为它会影响所有下载）；
- GUI 收到捕获后会立即建任务并开始下载，托盘提示“浏览器下载”。

## 3.1 媒体嗅探（视频）

扩展会用 `webRequest` **只观察**页面已经发出的请求（不注入页面、不修改请求），识别：

| 类型 | 判定 | 处理 |
|---|---|---|
| HLS 播放列表 | URL 路径/查询含 `.m3u8`，或 `Content-Type: *mpegurl` | 交给 HLS 引擎（自动选最佳码率，支持标准 AES-128） |
| HLS master | 扩展会 fetch 清单并解析 `EXT-X-STREAM-INF` | 每个码率变体单独列为候选（分辨率/码率），点击即下载该变体 |
| DASH 清单 | `.mpd` / `application/dash+xml` | 一键交给 DASH 引擎（未加密 VOD；画质仍由引擎选 best） |
| 普通视频 | `.mp4/.webm/.mkv/.mov/.flv`，或 `video/*` | 走普通多线程下载 |

媒体条目会标注来源（`network`、`dom`、`script`、`iframe/*`、`shadow/*`）与 HLS 变体的
分辨率/码率；同一 URL 的多个来源会合并显示。

另外，打开扩展弹窗时会**主动扫描当前页面**（`chrome.scripting.executeScript`，`allFrames: true`）：

- `<video>/<audio>/<source>` 的 `src/currentSrc`；
- `<a href>`、`<link href>` 中的 `.m3u8/.mpd`；
- 内联脚本里引号包裹的绝对/协议相对/根相对 `.m3u8/.mpd` URL；
- 同一文档内**开放的 shadow DOM**（递归 `shadowRoot`）；
- 顶层与 iframe（`allFrames`，需要各 frame 的 host 权限）。

扫描结果与 `webRequest` 嗅探结果合并去重。弹窗里也有“扫描页面媒体”按钮可手动重扫；扫描失败（如 `chrome://` 页面）不会影响已发现的媒体。

如果页面视频的 `src/currentSrc` 是 `blob:`，弹窗会显示“检测到 MSE/blob 视频流；可尝试下方多轨捕获”的提示，而不是静默漏掉。MSE 多轨实验捕获见 3.3；仍不支持 DRM 与 Blob URL 直接解析，但会捕获 appendBuffer 字节并在有 ffmpeg 时合并视频+音频。

### 3.2 手动验证页

仓库提供 `browser/test-page.html` 与 `browser/frame.html`，覆盖 DOM 链接、内联脚本、iframe、shadow DOM 和 `blob:` 提示：

```bat
python -m http.server 8000
:: 浏览器打开 http://127.0.0.1:8000/browser/test-page.html
:: 点击 PDM 扩展图标，应看到 m3u8/mpd 列表与 MSE 提示
```

页面包含 `main.m3u8`、`master.m3u8`（两个码率变体）、`other.mpd`、`plain.mp4`、
iframe/Shadow DOM/内联脚本链接，以及接管测试链接。`/media/*.m3u8` 由演示服务器真实提供；
master 会展开为 `1280x720 · 2400 kbps` 与 `640x360 · 800 kbps` 两个候选，分别可下载
3 段/1 段。

使用方式：

1. 打开视频页面；弹窗会自动扫描页面内链接，**开始播放**后 `webRequest` 还会捕获动态请求；
2. 工具栏图标出现蓝色角标（本页发现 N 个媒体）；
3. 点击图标 → “本页媒体”列表 → 点 **下载**；
4. HLS（含标准 AES-128）会生成 `.ts` 文件；DASH 走未加密轨道（单/多 Period）；普通视频按原文件名保存。

列表按标签页隔离，切换页面/关闭标签页时自动清空。每个标签页最多保留 40 项。

**不能覆盖的情况**（技术上拿不到明文 URL）：

- `blob:` / MSE 流无法直接解析 URL（YouTube、B 站等大量现代播放器）；未加密 fMP4/WebM 可尝试 3.3 的多轨捕获，DRM 流仍拒绝；
- DRM（Widevine/PlayReady，付费流媒体）；
- 需要特殊加密协商或自定义协议的播放器；
- closed shadow root、没有 host 权限的页面（如 `chrome://`、部分扩展页），以及只能由页面脚本运行时生成的 URL（扫描不执行页面变量）。开放 shadow DOM 与 http(s) iframe 已经覆盖。

这些不做绕过；扩展会安静地发现不到，或发现了也无法下载。HLS 的 SAMPLE-AES/DRM 同样会被引擎拒绝；标准 AES-128 可以下载。

## 3.3 MSE/blob 多轨实验捕获（Chrome/Edge/Firefox 128+）

边界：只捕获页面通过 `SourceBuffer.appendBuffer()` 喂给 MSE 的**未加密 fMP4/WebM**
字节；按 SourceBuffer 分轨（视频/音频，最多 4 轨）；不解析 Blob URL、不做 MediaRecorder
重编码、不尝试 EME/DRM 解密。

使用：

1. 打开扩展弹窗，点“开始 MSE 捕获”（会向当前标签页 MAIN world 注入 hook，
   并注入 isolated-world 的桥接脚本）；
2. 在页面播放视频；hook 把每个 SourceBuffer 的 MIME（缺失时从 init 字节嗅探
   fMP4 `hdlr` / WebM `TrackType`）与每次 `appendBuffer` 的字节连同 `track` 交给桥接脚本；
3. 再点“停止并保存”。

限制与安全：

- 多轨：每个 SourceBuffer 独立 track；视频默认 `.mp4`/`.webm`，音频默认
  `.m4a`/`.webm`；最多 4 个会话；
- 4 MiB / Native Messaging 分块；默认 **8 GiB / 轨、16 GiB / 总会话**（`PDM_MSE_MAX_TRACK_BYTES`/`PDM_MSE_MAX_TOTAL_BYTES` 可调），`PDM_MSE_MIN_FREE_BYTES`（默认 1 GiB）保留磁盘余量；超过上限/空间不足时停止并保存已有数据；
- 可选合并：配置 ffmpeg（`PDM_DOWNLOADER_FFMPEG`、GUI `ffmpeg_path` 或 PATH）时，
  停止会对最大的视频+音频执行固定 `-map 0:v:0 -map 1:a:0 -c copy`，输出
  `<stem>.merged.mp4/.webm`，源轨保留；ffmpeg 缺失/失败时返回 `merge_error` 并保留各轨；
- 检测到 `encv`/`enca`、Widevine/PlayReady/FairPlay/ClearKey 或
  `requestMediaKeySystemAccess` 时立即停止，不生成输出；
- 输出直接写到 `%APPDATA%\PersonalDownloadManager\capture\mse\`，不进入 GUI 队列；
  文件名做 Windows 净化；宿主只对自己创建的路径执行 ffmpeg，不经过 shell；
- Web 页面仍可伪造 `postMessage`，因此只应捕获自己有权保存的内容；该功能默认不自动运行。
- **Firefox 128+**：MAIN world 注入已用 WebDriver BiDi 探针在 Firefox 159 验证（本地
  WebM 与严格 CSP 页均 2 轨/4 chunks/48,321 B）。Gecko 的 content script 不能访问页面
  principal 传来的 `ArrayBuffer`（`Permission denied to access property "constructor"`），
  因此 `mse_capture.js` 在 MAIN world 先把 chunk 转 base64，`mse_bridge.js` 直接转发
  字符串；Chromium 仍走 ArrayBuffer + transfer 的零拷贝路径。更低版本 Firefox 会在
  开始捕获时明确报错。完整 background→Native Host→GUI 端到端已验证（Firefox 159：视频
  WebM 27,804 B + 20,510 B → 合并 47,971 B；本地 fMP4 14,100 B + 18,030 B →
  合并 31,589 B；DASH-IF/Akamai BBB 4,028,948 B + 167,793 B → 合并 4,189,997 B，
  GUI 归档 SHA-256 一致）；本地 hls.js 1.6.15 第二播放器（muxed 242,020 B，GUI 归档
  SHA-256 一致）已通过，公网 hls.js demo 默认因 jQuery/googleapis 不可达 blocked，
  测试专用 jQuery shim 下已捕获 5,506,934 B（webRequest 重定向 2,730,802 B）；
  测试副本用 iframe 加载真实 popup.html 点击开始/停止（2 轨/47,971 B），工具栏 action
  popup 未点击；Shaka Player demo（appspot）不可达；商业 DRM/直播未测，见
  [`firefox-mse-mainworld-evaluation-2026-11-08.md`](firefox-mse-mainworld-evaluation-2026-11-08.md)。

手动测试页：`browser/mse-test.html`（先启动 `python browser/serve_demo.py --port 8000`，
并先点弹窗“开始 MSE 捕获”再点页面“播放多轨演示（视频+音频）”）。本地视频+音频夹具
`browser/mse-fixture.mp4` + `browser/mse-audio.mp4` 已用真实 ffmpeg 验证合并为 h264+aac。

## 4. 协议与安全

- 传输格式：4 字节小端长度 + UTF-8 JSON（Chrome 原生消息协议）。
- 动作：`ping`、`download`，以及实验性的 `mse_start` / `mse_chunk` / `mse_end` / `mse_end_all` / `mse_abort` / `mse_abort_all`；`download` 只接受 `http(s)`，其余直接拒绝。
- `download` 只写一个 JSON 捕获文件（原子替换），不自己下载、不访问网络。MSE 动作把分块写入 `capture/mse/`；`mse_end_all` 可能执行固定参数的 ffmpeg 合并（仅限自己创建的文件，不经过 shell）。
- 单条消息上限 8 MB，文件名/Cookie/头都有长度上限；文件名在 GUI 侧再做 Windows 净化。
- 宿主清单的 `allowed_origins` 固定为扩展 ID，其他扩展无法调用它。
- 扩展的权限：`nativeMessaging`、`webRequest`（仅观察）、`downloads`、`cookies`、`storage`、`notifications`、`contextMenus`、`scripting`（仅在用户打开弹窗时扫描当前页）、`activeTab`，以及 http/https 的 host 权限。

## 5. 已验证 / 未验证

已验证（自动化 + 手工）：

- 宿主协议往返：`ping` → `{ok, version}`；`download` → 捕获文件内容正确；
- 非 http(s)、超长消息、坏 JSON 会被拒绝；
- 扩展 ID 与 `manifest.key` 一致；
- 捕获文件 → GUI 入队 → 下载完成（GUI 计时器路径有集成测试）；
- 安装包：静默安装（含浏览器任务）→ 清单是合法 JSON（无 BOM、路径正确转义）→ 注册表指向正确 → 宿主可运行 → 静默卸载后目录与注册表键都被清理；
- **真实 Chrome 156 实机（2026-11-01）**：未打包加载扩展、Native Host 连接、页面扫描发现 6 个媒体（普通链接 / 内联脚本 / iframe / Shadow DOM）、HLS（含 AES-128）/DASH/MP4 全部经捕获入队并完成，`capture\` 正常清空。详见 [`browser-verification-2026-11-01.md`](browser-verification-2026-11-01.md)。
- **接管下载（2026-11-01）**：Chrome 下载历史为 `CANCELLED`、0 字节、无 target path；PDM 任务完成，131,072 字节，SHA-256 与服务器一致；没有浏览器残留副本。
- **HLS master 画质候选（2026-11-01）**：master 展开出 `1280x720 · 2400 kbps` 与 `640x360 · 800 kbps`；两个候选分别下载 786,432 / 262,144 字节，SHA-256 与服务器解密内容一致。
- **MSE 单轨捕获（2026-11-01）**：测试页输出 14,100 字节，与 fixture SHA-256 完全一致；证据 `bench/results/browser-verification-2026-11-01-mse-capture.json`。
- **MSE 多轨（2026-11-03）**：多轨会话/分块/可配置上限与磁盘余量/WebM 命名/ffmpeg 合并/DRM 拒绝/文件名净化有自动化测试；本地视频+音频夹具经真实 ffmpeg 合并为 h264+aac。**Edge 156 实机通过（2026-11-04）**：页面 2 个 SourceBuffer，视频 14,100 B + 音频 18,030 B，合并 31,589 B h264+aac；报告 [`browser-verification-2026-11-04-mse-multitrack.md`](browser-verification-2026-11-04-mse-multitrack.md)。**DASH-IF/Akamai 真实站点通过**：视频 1,383,397 B + 音频 201,301 B，合并 1,578,521 B h264 480×270 + aac；报告 [`browser-verification-2026-11-04-mse-real-site.md`](browser-verification-2026-11-04-mse-real-site.md)。**停止健壮性（2026-11-04）**：请求/链/end_all/弹窗超时保护；受控 MediaSource 重置后 `mseStop` 0.102s 返回并合并；报告 [`browser-verification-2026-11-04-mse-reset-stop.md`](browser-verification-2026-11-04-mse-reset-stop.md)。**Chrome 157 复验（2026-11-07）**：官方 Chrome for Testing 157.0.8103.38，headless 隔离配置；正常捕获 2,782,126 B + 167,793 B → 2,944,027 B，重置后 3,623,658 B + 234,608 B → 3,852,588 B，停止 0.093s；报告 [`browser-verification-2026-11-07-mse-chrome.md`](browser-verification-2026-11-07-mse-chrome.md)。
- **MSE→GUI 队列真机（2026-11-07）**：Edge 156.0.4314.58 隔离 `--user-data-dir` + `--headless=new` 加载扩展，DASH-IF/Akamai 实际捕获 2 轨并合并 1,153,622 B（`mseStop` 0.519 s）；打包 GUI 自动导入 1 条“已捕获”任务（源 URL/标题/2 轨/合并文件），属性完整，删除清理合并文件与两条源轨；报告 [`browser-verification-2026-11-07-mse-queue.md`](browser-verification-2026-11-07-mse-queue.md)。
- **MSE 公开站点矩阵（2026-11-08）**：`bench/mse_site_matrix.mjs`；DASH-IF/Akamai、Shaka angel-one、Unified Tears of Steel 三个公开 MP4 素材 + 本地 WebM 控制页，4/4 通过；hls.js demo 因 jQuery 的 `ajax.googleapis.com` 在本网络 TCP 超时默认 blocked（`hls-demo.js` 报 `$ is not defined`）；测试专用 jQuery shim 下已跑通（content 注入 2 轨 → 5,506,934 B；webRequest 重定向 → 2,730,802 B）。报告 [`mse-site-matrix-2026-11-08.md`](mse-site-matrix-2026-11-08.md)。
- **MSE WebM 实机（2026-11-08）**：`browser/mse-webm-test.html` 使用 VP9 视频 + Opus 音频两个 SourceBuffer；Edge 156 隔离 headless 实际播放，捕获 27,804 B + 20,510 B，ffmpeg 合并为 47,971 B `.merged.webm`，ffprobe 为 VP9 320×180 + Opus；GUI 归档/源轨清理通过；报告 [`browser-verification-2026-11-08-mse-webm.md`](browser-verification-2026-11-08-mse-webm.md)。
- **右键菜单 + Firefox 159 实机（2026-11-01）**：Chrome 156 / Firefox 159 右键测试页 `plain.mp4`，菜单出现“用 PDM 下载此链接”；两边均完成 262,144 字节下载，SHA-256 与服务端一致。证据见 [`browser-verification-2026-11-01.md`](browser-verification-2026-11-01.md) 与 `bench/results/browser-verification-2026-11-01-rightclick-firefox.json`。

**Cookie 转发与 Firefox 接管已验证**：Chrome 156 的 `cookie-check.bin` 任务带 `pdmcookie=verified` 完成 65,536 字节；Firefox 159 的 `takeover-demo.bin` 任务完成 131,072 字节，SHA-256 与服务器一致，详见 [`browser-verification-2026-11-02.md`](browser-verification-2026-11-02.md)。branded Chrome 156/157 仍忽略命令行 `--load-extension`；Edge 156 在隔离 profile + headless 下本轮可正常加载（2026-11-07 的 MSE→GUI 真机验证即用此方式）。普通页面/扩展手动验证仍可用 `browser/test-page.html` + `browser/serve_demo.py`。

已知限制：

- 接管对 `blob:`/`data:` 等非 http(s) 下载不生效（窗口会继续用浏览器下载）；
- 嗅探只识别 HTTP 层可见的地址；MSE/DRM 流不可见；`.ts`/`.m4s` 分片不单独列出（由清单覆盖）；页面扫描覆盖 iframe 与开放 shadow DOM，但不执行页面脚本变量、不进入 closed shadow root、不捕获 MSE 字节；
- HLS master 变体由扩展 fetch 后解析；如果清单需要特殊认证/CORS 或 fetch 失败，只会保留 master 条目，由引擎按 best 选择。DASH 的 Representation 暂不在扩展里展开，仍由引擎选 best；
- MSE 捕获已支持多轨未加密 fMP4/WebM、默认 8 GiB/轨、16 GiB/会话（可配置）+ 1 GiB 磁盘余量、最多 4 轨、可选 ffmpeg 合并；fMP4 与 WebM 均已实机验证（WebM 为本地 VP9/Opus MSE 页），多轨已在 Edge 156、DASH-IF/Akamai 真实站点与 Chrome for Testing 157（headless）通过；停止路径有超时保护并在受控 MediaSource 重置下验证；branded Chrome 仍不支持命令行加载未打包扩展；不处理 Blob URL、DRM；捕获完成后 Host 写完成记录，GUI 会作为“已捕获”任务导入队列，并在后台把合并文件（无合并时为主轨）归档到下载目录/分类（源轨仍留在 capture/mse）；
- 扩展未上架商店，每次浏览器更新后可能提示“开发者模式扩展”；Firefox 的临时载入重启后失效；
- Cookie 只转发目标 URL 的当前值，不导入浏览器 Cookie 数据库；
- 扩展加载失败时先看 `chrome://extensions` 的错误，再确认注册表键与清单路径存在。
