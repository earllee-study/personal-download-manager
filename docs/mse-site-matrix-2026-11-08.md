# MSE 公开站点矩阵（2026-11-08）

> 目标：把 MSE 捕获从单一参考站点扩展为可复现的站点/素材矩阵，同时覆盖
> **MP4（fMP4）** 与 **WebM** 两种容器。矩阵驱动为
> [`bench/mse_site_matrix.mjs`](../bench/mse_site_matrix.mjs)。

## 1. 环境

| 项 | 值 |
|---|---|
| 浏览器 | Microsoft **Edge 156.0.4314.58**（Chromium 156），隔离 `--user-data-dir` |
| 模式 | `--headless=new`、`--no-sandbox`（仅测试实例） |
| 扩展 | `browser/extension`，`--load-extension`（ID `hfiihggbmdmoofcadgmkingenjgcaend`） |
| Native Host | `dist/pdm-host/pdm-host.exe`（已注册） |
| ffmpeg | 9.0.2（`PDM_DOWNLOADER_FFMPEG`） |
| 隔离 | 独立 profile、临时 `APPDATA`/捕获目录；测试结束关闭实例 |

## 2. 矩阵结果

| 用例 | 站点/素材 | 容器 | 轨道 | 合并文件 | 停止 |
|---|---|---|---|---:|---:|
| `dashif-akamai-bbb` | DASH-IF reference player + Akamai BBB | MP4 | h264 480×270 + aac | 4,406,871 B | 0.116 s |
| `dashif-shaka-angel-one` | DASH-IF reference player + Shaka angel-one | MP4 | h264 1920×1080 + aac | 11,931,662 B | 0.45 s |
| `dashif-unified-tears` | DASH-IF reference player + Unified Tears of Steel | MP4 | h264 480×270 + aac | 1,222,009 B | 1.199 s |
| `local-webm` | 本地 WebM MSE 控制页（VP9/Opus） | WebM | vp9 320×180 + opus | 47,971 B | 0.070 s |
| `hlsjs-shaka-angel-fmp4` | hls.js demo + Shaka fMP4 HLS | MP4 | — | — | blocked |

**结果：4/4 可执行用例通过；hls.js 站点在隔离环境中被外部依赖阻塞（非 PDM 故障）。**

### 2.1 捕获明细

- **Akamai BBB**：视频 4,180,153 B（`avc1.640015`）+ 音频 234,608 B（`mp4a.40.5`）；
  ffmpeg `-c copy` 合并为 4,406,871 B；ffprobe h264 480×270 + aac（28.07 s）。
- **Shaka angel-one**：视频 11,769,404 B（`avc1.640028`）+ 音频 167,793 B；
  合并 11,931,662 B；ffprobe h264 1920×1080 + aac（20.05 s）。
- **Unified Tears of Steel**：视频 1,123,756 B + 音频 101,079 B；合并 1,222,009 B；
  ffprobe h264 480×270 + aac（12.03 s）。
- **WebM 控制页**：视频 27,804 B（VP9）+ 音频 20,510 B（Opus）；合并 47,971 B；
  ffprobe vp9 320×180 + opus（2.008 s）。

Akamai/Shaka 两个用例由打包 GUI 自动归档到 `downloads/视频/`；Unified/WebM 在干净
Edge 单例中完成捕获与合并（GUI 导入/归档/清理链路已由其他报告和测试覆盖）。

### 2.2 hls.js 阻塞说明

`https://hls-js.netlify.app/demo/` 会重定向到 `https://hlsjs.video-dev.org/demo/`；
在隔离 headless Edge 中该页面停留在 `document.readyState=loading`，外部
jQuery/Bootstrap 依赖未完成加载，页面没有创建 MSE/播放器实例。扩展的
`mseStart`/`mseStop` 本身正常，但页面没有 `appendBuffer` 数据。该用例记为
**blocked（环境/站点）**，不是 PDM 捕获失败；可在网络/站点恢复后用同一驱动重试。

2026-11-08 的 Firefox BiDi 诊断进一步确认了具体原因：页面从
`https://ajax.googleapis.com/ajax/libs/jquery/2.1.3/jquery.min.js` 加载 jQuery；
本机 DNS 解析到 `174.132.167.252` 后 TCP 443 超时，资源计时为 `duration=0`、
`responseStatus=0`。因此 `window.$` 未定义，`bootstrap.min.js` 报
“requires jQuery”，`metrics.js` 与 `hls-demo.js` 均抛 `$ is not defined`，播放器
没有绑定视频源（`video.src=""`）。`hls.js` 与 `hls-demo.js` 自身均以 200 加载。
诊断证据 `bench/results/browser-verification-2026-11-08-firefox-hlsjs-demo-diag.json`。

2026-11-08 进一步用**测试专用 jQuery shim** 验证：测试副本通过 `content_scripts`
（`run_at: document_start`、`world: MAIN`）注入 jQuery 2.1.3，demo 正常初始化
（5 个码率、6 条音轨），捕获 2 轨并合并 **5,506,934 B**（h264 480×360 + aac，40 s）；
生产扩展未改动。证据
`bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4-shim.json`。

另验证**网络层重定向**：测试副本用 `webRequestBlocking` 把 jQuery 请求 302 到
cdnjs（Firefox 159 接受该权限），demo 同样正常初始化，捕获 2 轨 → 合并
**2,730,802 B**（h264 480×360 + aac）；证据
`bench/results/browser-verification-2026-11-08-firefox-hlsjs-shaka-angel-fmp4-redirect.json`。

## 3. 复现

```bat
python browser/serve_demo.py --port 8000
:: 以隔离 profile + --load-extension 启动 Edge/Chrome for Testing，开放 CDP 端口
set PDM_DOWNLOADER_CAPTURE_DIR=<临时目录>\capture
set PDM_DOWNLOADER_FFMPEG=D:\Application\tools\ffmpeg\bin\ffmpeg.exe
node bench/mse_site_matrix.mjs http://127.0.0.1:9335 build/site-matrix/run
:: 只跑某个用例：
set PDM_MATRIX_ONLY=dashif-unified-tears,local-webm
node bench/mse_site_matrix.mjs http://127.0.0.1:9335 build/site-matrix/run-subset
```

每个用例输出 `<out-dir>/<id>.json`，汇总为 `summary.json`。驱动会：
打开独立 tab → 等待页面 `complete` 并激活 → `mseStart` → 触发播放 →
等待 `currentTime > 1` → `mseStop`；不读取/修改站点数据。

## 4. 限制

- “多个公开站点”目前实际是 **DASH-IF 一个站点 + 三个公开 CDN 素材**，加上本地
  WebM 控制页；hls.js 作为第二个公网站点被外部依赖阻塞。
- 未覆盖 DRM/商业站点、直播、长时长 soak；Chrome 内核使用本机 Edge 156。
- 不同 CDN 的码率/时长不同，矩阵验证的是**容器/轨道/合并正确性**，不是吞吐基准。

## 5. 后续：hls.js 第二播放器（2026-11-08）

公网 hls.js demo 在 Firefox 159 隔离环境中默认仍 blocked：页面标题加载，但
`video.readyState=0`、`currentTime=0`，没有 `appendBuffer`；与本文 Edge 结论一致。
根因是 jQuery 的 `ajax.googleapis.com` 在本网络 TCP 超时，`hls-demo.js` 抛
`$ is not defined`（见 §2.2）。使用**测试专用 jQuery shim**（content_scripts 注入或 webRequestBlocking 重定向到
cdnjs）后 demo 正常初始化，捕获 2 轨并合并 **5,506,934 B / 2,730,802 B**
（h264 480×360 + aac）；生产扩展未改动。

为验证第二个播放器，新增**本地 hls.js 镜像**（hls.js 1.6.15 + ffmpeg 生成的 6 s
fMP4 HLS）。Firefox 159 真实扩展 E2E 捕获 1 条 **muxed** 轨 242,020 B
（`video/mp4;codecs=mp4a.40.2,avc1.64000C`，h264 320×180 + aac），单轨无需合并；
打包 GUI 导入并归档到 `downloads/视频/pdm-mse.mp4`，SHA-256 一致。详见
[`firefox-mse-mainworld-evaluation-2026-11-08.md`](firefox-mse-mainworld-evaluation-2026-11-08.md)
§3.8；证据 `bench/results/browser-verification-2026-11-08-firefox-hlsjs-local.json`、
`...-hlsjs-local-gui.json`、`...-hlsjs-shaka-angel-fmp4.json`（blocked）。

## 6. 证据

- [`bench/results/mse-site-matrix-2026-11-08/summary.json`](../bench/results/mse-site-matrix-2026-11-08/summary.json)
- 各用例 JSON（同目录）：`dashif-akamai-bbb.json`、`dashif-shaka-angel-one.json`、
  `dashif-unified-tears.json`、`local-webm.json`、`hlsjs-shaka-angel-fmp4.json`
- 驱动：`bench/mse_site_matrix.mjs`
