# Firefox MSE MAIN-world 评估（2026-11-08）

> 目标：确认 Firefox 能否把现有的 `mse_capture.js` 注入页面 MAIN world，并打通
> MAIN world → isolated content script → background 的分块链路。此前只有
> Chrome/Edge 验证过；Firefox 的 MAIN-world 注入一直标为“未保证”。

## 1. 环境

| 项 | 值 |
|---|---|
| 浏览器 | Mozilla **Firefox 159.0.1**（build ID `20261102144711`） |
| 模式 | `--headless`、独立 `--profile build/firefox-eval/profile`、`--no-remote` |
| 自动化 | `--remote-debugging-port 9334` + **WebDriver BiDi**（`ws://127.0.0.1:9334/session`） |
| 扩展 | 临时探针扩展 `pdm-eval@local`，用 BiDi `webExtension.install`（`type: "path"`）安装 |
| 页面 | `browser/mse-webm-test.html`（VP9 27,804 B + Opus 20,510 B，两个 `SourceBuffer`） |
| CSP 变体 | 同页 + `<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; media-src 'self' blob:; object-src 'none'">`，内联脚本外置为 `play.js` |
| Node | v24.22.1（全局 `WebSocket` / `fetch`） |
| 隔离 | profile、报告文件、CSP 站点全部在 `build/firefox-eval/`，验证后删除/停止 |

## 2. 方法

探针扩展在目标页加载完成后依次执行：

1. `scripting.executeScript({target, files: ["mse_bridge.js"]})`（默认 ISOLATED）；
2. `scripting.executeScript({target, files: ["mse_capture.js"], world: "MAIN"})`；
3. 从 MAIN world 查询 `Boolean(window.__pdmMseHooked)`，再从 isolated world 查一次；
4. `window.postMessage({__pdmMseStart:true})`；
5. 点击测试页的“播放 WebM 多轨演示”，等待 7 秒；
6. 发送 `__pdmMseStop`，等待 1.2 秒；
7. 统计 `mseEvent`：轨道、chunk 数、base64 字节数、错误。

探针只用于评估，未改用生产扩展的 popup/Native Host 路径；分块消息格式与生产
`background.js` 完全一致。

## 3. 结论

### 3.1 MAIN world 注入可用

- `mainInjected: true`；
- MAIN world 读到 `__pdmMseHooked: true`，isolated world 读到 `false`；
- 说明 Firefox 159 的 `scripting.executeScript({world:"MAIN"})` 确实把 hook 放进了
  页面 realm，而不是 isolated world。

### 3.2 ArrayBuffer 不能跨 principal 传递

第一次验证时轨道能识别（`track-1` 视频、`track-2` 音频），但 chunk 数为 0。诊断结果：

```
MAIN: chunkPosts=4, postErrors=[]
ISOLATED: seen=9, chunks=4,
  data = [object ArrayBuffer] (16384 / 11420 / 16384 / 4126 B),
  errors = ["Permission denied to access property \"constructor\"", ...]
```

- 无论 `postMessage` 是否带 transfer list，content script 都能“看到”消息，但访问
  ArrayBuffer 时被 Firefox 的跨 compartment 包装拒绝；
- 这是 Gecko 的 principal 边界行为，不是 `mse_capture.js` 的 hook 失效。

### 3.3 修复：MAIN world 内先 base64 编码

`mse_capture.js` 检测到 Gecko 时，在 MAIN world 把字节编码为 base64 字符串再
`postMessage`；`mse_bridge.js` 收到字符串时直接转发，不再尝试 `new Uint8Array(pageArrayBuffer)`。
Chromium 继续走原来的 ArrayBuffer + transfer zero-copy 路径，行为不变。

修复后：

| 页面 | 轨道 | chunk | 字节（base64 估算） | 错误 |
|---|---|---:|---:|---|
| 本地 WebM MSE 页 | `track-1` video/webm (VP9), `track-2` audio/webm (Opus) | 4（视频 2 + 音频 2） | 48,321（实际 27,804 + 20,510 = 48,314） | 无 |
| 同页 + 严格 CSP | 同上 | 4 | 48,321 | 无 |

（base64 估算按 `len*3/4`，与真实字节数相差 7 B，属填充位取整。）

### 3.4 CSP 不阻断 MAIN-world 文件注入

页面只允许 `script-src 'self'`、没有 `unsafe-inline`/`unsafe-eval`，扩展仍能把
`mse_capture.js` 注入 MAIN world 并捕获 4 个 chunk。页面自身的 `play.js` 是外置同源
脚本，正常执行。

### 3.5 `chrome.scripting` 命名空间可用

生产 `background.js` 优先取 `chrome.scripting`。探针分别用 `browser.scripting` 与
`chrome.scripting` 跑过：两者都返回 Promise，`await executeScript(...)` 正常，
`world: "MAIN"` 与 `func` 注入都成功，无需把生产代码改成 `browser.*`。

### 3.6 完整端到端：真实扩展 → Native Host → GUI（2026-11-08）

为验证生产路径，使用 `browser/firefox` 的**测试副本**（除新增 `e2e.html`/`e2e.js`
驱动页与本机报告权限外，`background.js`、`mse_capture.js`、`mse_bridge.js`、popup
与生产完全一致），并用一个只含 `management`/`tabs` 权限的辅助扩展解析
`moz-extension://<uuid>`（`management.getAll()` 的 icons URL 自带 UUID）。
BiDi 直接导航 `moz-extension://` 会返回 `unsupported operation`，因此由辅助扩展
用 `tabs.create` 打开 `e2e.html`，该页调用生产 `mseStart`/`mseStop` 并用
`scripting.executeScript` 点击页面的播放按钮。

环境：Firefox 159.0.1 隔离 headless；Native Host 已注册（`allowed_extensions:
["pdm@local"]`）；`PDM_DOWNLOADER_CAPTURE_DIR=build/firefox-e2e/capture`；
`PDM_DOWNLOADER_FFMPEG=D:\Application\tools\ffmpeg\bin\ffmpeg.exe`。

结果：

| 阶段 | 结果 |
|---|---|
| `mseStart` | `{ok: true}`，Injection 成功 |
| 播放 | `currentTime=1.90 s`，`readyState=3`；页面日志 `appendBuffer 27804 B / 2 calls` + `20510 B / 2 calls` |
| `mseStop` | `{ok: true}`，2 轨 + 合并 |
| 视频轨 | 27,804 B，SHA-256 `4a4003d0…6cd5` |
| 音频轨 | 20,510 B，SHA-256 `c428418b…d5d3` |
| 合并 | 47,971 B，SHA-256 `98dcefa2…5490`；ffprobe VP9 320×180 + Opus，2.008 s |
| 完成记录 | `capture/mse/mse-session-c4ffda83…json` |

打包 GUI 验证（`dist/pdm-gui/pdm-gui.exe`，临时 `APPDATA`、`QT_QPA_PLATFORM=offscreen`）：

- `downloads.json` 中 1 条 `completed` 的“已捕获”任务，`options.capture=mse`；
- 合并文件归档到 `downloads/视频/pdm-mse.merged.webm`（47,971 B，SHA-256 与 host
  合并文件一致），manifest 已消费；
- `mse_delete_sources=false` 时两条源轨保留在 `capture/mse`，符合预期。

说明：popup 按钮本身没有被点击——WebDriver BiDi 不允许在 `moz-extension://` 页面
执行脚本；`e2e.html` 发送的是与 popup 完全相同的 `mseStart`/`mseStop` 消息。popup UI
在 Chrome/Edge 上已实机验证，Firefox 侧未单独点击。

### 3.7 fMP4 与 DASH-IF/Akamai（2026-11-08）

同一套 Firefox E2E 驱动（`e2e.html#fmp4` / `#dashif`）再跑两个场景：

| 场景 | 页面/素材 | 轨道 | 合并 | ffprobe |
|---|---|---|---|---|
| fMP4 | 本地 `browser/mse-test.html` + `mse-fixture.mp4`/`mse-audio.mp4` | video/mp4 `avc1.42C00A` 14,100 B + audio/mp4 `mp4a.40.2` 18,030 B | **31,589 B**，SHA-256 `93020e31…f70d` | h264 160×90 + aac，2.02 s |
| DASH-IF/Akamai | DASH-IF Reference Client + Akamai BBB MPD | video/mp4 `avc1.640015` 4,028,948 B + audio/mp4 `mp4a.40.5` 167,793 B | **4,189,997 B**，SHA-256 `520bc608…9bcf` | h264 480×270 + aac，20.05 s |

- 两个场景 `mseStart`/`mseStop` 均 `{ok: true}`，`track_count=2`，`merge_error=null`；
- fMP4 的合并大小与 Chrome/Edge 本地夹具结果一致（31,589 B）；
- DASH-IF 页面为公开未加密 BBB，播放到 `currentTime≈1.98 s`、`readyState=4`；
- DASH-IF 捕获也通过打包 GUI 导入/归档：1 条 `completed` 任务，归档到
  `downloads/视频/pdm-mse.merged.mp4`（4,189,997 B，SHA-256 与 host 合并文件一致），
  两条源轨保留（`mse_delete_sources=false`）。

### 3.8 第二播放器：hls.js 本地镜像（2026-11-08）

**公网 hls.js demo 仍然 blocked**：Firefox 159 打开了 `hls-js.netlify.app/demo/`
（重定向到 `hlsjs.video-dev.org`），页面标题为 “hls.js demo”，但 `video.readyState=0`、
`currentTime=0`、`video.src=""`、没有 `appendBuffer`。

页面侧诊断（Firefox BiDi，2026-11-08）：

- 页面从 `https://ajax.googleapis.com/ajax/libs/jquery/2.1.3/jquery.min.js` 加载 jQuery；
  本机 DNS 将其解析到 `174.132.167.252`，TCP 443 **超时**（`curl` 返回 000，20 s 超时）；
  资源计时条目为 `duration=0`、`transferSize=0`、`responseStatus=0`。
- 因此 `window.jQuery` / `window.$` 为 `undefined`；控制台错误：
  - `Bootstrap's JavaScript requires jQuery`（bootstrap.min.js）
  - `ReferenceError: $ is not defined`（metrics.js）
  - `ReferenceError: $ is not defined`（hls-demo.js）
- `hls.js`（1,597,080 B）与 `hls-demo.js`（800,058 B）本身均以 200 加载；
  但 `hls-demo.js` 在初始化阶段因 `$` 缺失中止，播放器没有绑定视频源，`video.src` 保持空。
- 页面静态 HTML/控件仍在，所以看起来“已加载”，实际没有创建 MSE/appendBuffer。

结论：这是**站点依赖的 Google CDN 在本网络不可达**，不是 PDM 捕获故障。同版本 jQuery
在 `cdnjs.cloudflare.com`、`code.jquery.com`、`libs.baidu.com` 均可达（curl 200）；
本地 hls.js 镜像已绕开该依赖。诊断证据
`bench/results/browser-verification-2026-11-08-firefox-hlsjs-demo-diag.json`。

**测试专用 jQuery shim（2026-11-08）**：为确认唯一阻塞点，在测试副本中加入
`content_scripts`（匹配 `hls-js.netlify.app` / `hlsjs.video-dev.org`，
`js: jquery-2.1.3.min.js`，`run_at: document_start`，`world: MAIN`），jQuery 2.1.3
来自可达镜像 cdnjs（84,355 B，SHA-256 `2051d614…5515`）；生产扩展未改动。结果：

- demo 页面正常初始化，日志为 `Media element attached`、`5 quality levels found`、
  `Manifest successfully loaded`、`No of audio tracks found: 6`、`Audio track switched`；
- 播放到 `currentTime=1.13 s`、`readyState=4`、`duration=60 s`；
- 捕获 2 轨并合并：video 4,866,655 B（`avc1.4D401F`）+ audio 639,826 B
  （`mp4a.40.2`）→ merged **5,506,934 B**，SHA-256 `3202dbb8…f12f`；
- ffprobe：h264 480×360 + aac，40.00 s。

结论：公网 demo 的唯一阻塞点就是 `jQuery/googleapis`；加测试专用 shim 后可完整捕获，
且不修改生产扩展。证据
`bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4-shim.json`。

**网络层重定向（2026-11-08）**：另一种更接近真实页面的做法是在测试副本中加入
`webRequest` + `webRequestBlocking`，把
`https://ajax.googleapis.com/.../jquery.min.js` 302 到
`https://cdnjs.cloudflare.com/.../jquery.min.js`。Firefox 159 接受该权限
（`shim: {"mode":"redirect","ok":true}`）；demo 同样正常初始化（5 码率、6 音轨），
捕获 2 轨 → 合并 **2,730,802 B**（video 2,154,865 B + audio 574,715 B；ffprobe
h264 480×360 + aac，40 s）。证据
`bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4-redirect.json`。
（两次运行的分块大小随播放/缓冲时长变化，不影响正确性。）

其他可选替代（未逐一实测）：

- **本地页面镜像**：下载 demo HTML，把 jQuery URL 改为 cdnjs/本地文件，并把
  `../dist/*` 重写为绝对 URL 或一并镜像，用本地静态服务器提供；不需要扩展改动。
- **CDP `Fetch` / BiDi network interception**：由驱动拦截 jQuery 请求并返回本地文件；
  Chromium 侧最干净，Firefox 的 BiDi network 支持需按版本确认。
- **本地代理/MITM 或 hosts + 本地 HTTPS 证书**：最接近真实网络，但证书/管理员配置较重。
- **换用不依赖 Google CDN 的播放器/页面**（Shaka demo、video.js 本地页等）。

为验证第二个播放器，改用**本地 hls.js 镜像**：

- hls.js **1.6.15**（jsDelivr `hls.min.js`，541,870 B，SHA-256 `413a83e2…8938`）；
- ffmpeg 9.0.2 生成本地 **fMP4 HLS**（`init.mp4` + 6×1 s `segNNN.m4s`，320×180 h264 + aac）；
- 页面 `build/firefox-e2e/hlsjs-site/hlsjs-test.html` 在点击播放时才创建 `Hls`/
  `MediaSource`，确保 PDM hook 先注入；
- Firefox 159 隔离 headless + 真实扩展 E2E。

结果：

| 项 | 值 |
|---|---|
| 播放 | `currentTime=1.87 s`，`readyState=4`，`duration=6.08 s`，日志 `manifest parsed` |
| 捕获 | 1 条 **muxed** 轨 `video/mp4;codecs=mp4a.40.2,avc1.64000C`，242,020 B，SHA-256 `e5e1b2ac…24c6` |
| 合并 | 无（`merged=null`；单条 muxed 轨不需要合并，符合设计） |
| ffprobe | h264 320×180 + aac，6.10 s |
| GUI | 1 条 `completed` 任务，归档到 `downloads/视频/pdm-mse.mp4`（242,020 B，SHA-256 一致，`mse_merged=false`），capture 目录清空 |

证据：`bench/results/browser-verification-2026-11-08-firefox-hlsjs-local.json`、
`...-hlsjs-local-gui.json`。

## 4. 代码改动

| 文件 | 改动 |
|---|---|
| `browser/extension/mse_capture.js` | 新增 `GECKO = /firefox/i.test(navigator.userAgent)` 与 `toBase64()`；Gecko 分支 post `{data: base64, encoding: "base64"}`，Chromium 分支保持 `data: bytes.buffer` + transfer |
| `browser/extension/mse_bridge.js` | chunk 数据为字符串时直接转发；ArrayBuffer 仍走原 `encode()` |
| `browser/extension/background.js` | `mseStartCapture()` 在 Firefox < 128 时返回明确错误（MAIN world 不支持；旧版可能静默注入 isolated world，导致“无数据”而非报错） |
| `browser/firefox/*` | `python browser/build_firefox.py` 重新同步共享 JS |
| `tests/test_browser_host.py` | 在 MSE 接线测试中断言 Gecko 分支、bridge 字符串路径与 Firefox 128 门槛 |

`browser/firefox/manifest.json` 的 `strict_min_version` 仍为 `115.0`：非 MSE 功能
（接管下载、右键菜单、页面扫描）在 115–127 仍可用；只有 MSE 捕获需要 128+，由
`background.js` 给出明确提示。

## 5. 限制与后续

- 完整 background → Native Host → GUI 已用 Firefox 159 复测（3.6），并覆盖本地
  WebM、fMP4 与公网 DASH-IF/Akamai BBB（3.7）；仍未点击 popup UI 本身（BiDi 不能在
  `moz-extension://` 页面执行脚本）。
- 公网覆盖 DASH-IF/Akamai BBB；hls.js 公网 demo 本身因 jQuery 的
  `ajax.googleapis.com` 不可达而 blocked；**测试专用 jQuery shim（content_scripts
  注入或 webRequest 重定向）下均已完整捕获**（5,506,934 B / 2,730,802 B，2 轨，
  h264 480×360 + aac），生产扩展未改动；商业 DRM 站点、直播与长时长 soak 未测。
- Firefox 128 的最低版本来自 MDN 兼容性表；本机只在 159.0.1 实测。
- 未测试 `scripting.registerContentScripts({world:"MAIN"})`；当前设计是用户点“开始
  捕获”时用 `executeScript` 注入，不需要常驻 MAIN-world content script。
- 临时扩展在 Firefox 重启后失效；持久安装需要签名（AMO）或开发者版/ESR 的签名豁免。
- 未覆盖公网站点、DRM/EME（按设计拒绝）、直播与长时长 soak。

## 6. 证据

- [`bench/results/mse-firefox-mainworld-2026-11-08/report.json`](../bench/results/mse-firefox-mainworld-2026-11-08/report.json)
  （本地 WebM 页，真实源码）
- [`bench/results/mse-firefox-mainworld-2026-11-08/report-csp.json`](../bench/results/mse-firefox-mainworld-2026-11-08/report-csp.json)
  （严格 CSP 变体）
- [`bench/results/browser-verification-2026-11-08-firefox-e2e.json`](../bench/results/browser-verification-2026-11-08-firefox-e2e.json)
  （完整 background → Native Host → 落盘）
- [`bench/results/browser-verification-2026-11-08-firefox-e2e-gui.json`](../bench/results/browser-verification-2026-11-08-firefox-e2e-gui.json)
  （打包 GUI 导入/归档）
- [`bench/results/browser-verification-2026-11-08-firefox-fmp4.json`](../bench/results/browser-verification-2026-11-08-firefox-fmp4.json)
  （本地 fMP4 多轨）
- [`bench/results/browser-verification-2026-11-08-firefox-dashif-akamai-bbb.json`](../bench/results/browser-verification-2026-11-08-firefox-dashif-akamai-bbb.json)
  （DASH-IF/Akamai 公网）
- [`bench/results/browser-verification-2026-11-08-firefox-dashif-akamai-bbb-gui.json`](../bench/results/browser-verification-2026-11-08-firefox-dashif-akamai-bbb-gui.json)
  （DASH-IF 打包 GUI 归档）
- [`bench/results/browser-verification-2026-11-08-firefox-hlsjs-local.json`](../bench/results/browser-verification-2026-11-08-firefox-hlsjs-local.json)
  （本地 hls.js 第二播放器）
- [`bench/results/browser-verification-2026-11-08-firefox-hlsjs-local-gui.json`](../bench/results/browser-verification-2026-11-08-firefox-hlsjs-local-gui.json)
  （hls.js 单轨 GUI 归档）
- [`bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4.json`](../bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4.json)
  （公网 hls.js demo blocked）
- [`bench/results/browser-verification-2026-11-08-firefox-hlsjs-demo-diag.json`](../bench/results/browser-verification-2026-11-08-firefox-hlsjs-demo-diag.json)
  （jQuery/googleapis 超时、`$ is not defined` 诊断）
- [`bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4-shim.json`](../bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4-shim.json)
  （测试专用 jQuery shim 下公网 hls.js demo 捕获）
- [`bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4-redirect.json`](../bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4-redirect.json)
  （webRequest 重定向 jQuery 到 cdnjs）
- 探针/驱动（临时）：`build/firefox-eval/probe-ext/`、`build/firefox-eval/bidi_driver.mjs`、
  `build/firefox-eval/report_server.py`、`build/firefox-eval/csp-site/`
- 回归：`python -m pytest -q` → **392 passed in 132.17s**（2026-11-08，MSE 大内容/P2 之后；本文 Firefox 实验执行时为 390）

## 7. 复现（临时 E2E 驱动）

```bat
python browser/serve_demo.py --port 8000
python -I build/firefox-e2e/report_server.py 8765
python -I build/firefox-e2e/make_autotest.py
:: 以隔离 profile 启动 Firefox；环境变量：
::   PDM_DOWNLOADER_CAPTURE_DIR=<capture-dir>
::   PDM_DOWNLOADER_FFMPEG=D:\Application\tools\ffmpeg\bin\ffmpeg.exe
::   --headless --profile <profile> --remote-debugging-port 9334 --no-remote about:blank
set PDM_FIREFOX_SCENARIO=fmp4
node build/firefox-e2e/e2e_driver.mjs fmp4
set PDM_FIREFOX_SCENARIO=dashif
node build/firefox-e2e/e2e_driver.mjs dashif
:: 本地 hls.js 第二播放器（另开静态服务器）
cd build/firefox-e2e/hlsjs-site && python -m http.server 8002 --bind 127.0.0.1
set PDM_FIREFOX_SCENARIO=hlsjs-local
node build/firefox-e2e/e2e_driver.mjs hlsjs-local
set PDM_FIREFOX_SCENARIO=hlsjs
node build/firefox-e2e/e2e_driver.mjs hlsjs
:: 公网 hls.js demo 的依赖/错误诊断
node build/firefox-e2e/hlsjs_diag.mjs
```

本地 hls.js 素材：`vendor/hls.min.js`（hls.js 1.6.15）与 ffmpeg 生成的 fMP4 HLS；
ffmpeg 使用 `-f lavfi -i testsrc2=... -f lavfi -i sine=... -t 6 -c:v libx264
-c:a aac -f hls -hls_time 1 -hls_segment_type fmp4 ...`。`hlsjs` 场景用于复现公网
demo 的 blocked 结论，不依赖本地素材。

`e2e_driver.mjs` 会写入 `scenario.txt`、安装测试副本与 UUID 辅助扩展，并把结果写到
`bench/results/browser-verification-2026-11-08-firefox-<scenario>.json`。该驱动是临时
验证工具（位于 `build/`），不属于产品代码；MSE 代码全部来自 `browser/firefox`。
