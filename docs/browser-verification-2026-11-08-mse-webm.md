# MSE WebM 实机验证（2026-11-08）

> 目标：关闭“代码支持 WebM，但只有单测”的验证缺口——在真实浏览器 MSE 中捕获
> VP9 视频 + Opus 音频，确认轨道识别、`.webm` 命名、ffmpeg 合并、GUI 导入/归档/
> 源轨清理全链路。

## 1. 环境

| 项 | 值 |
|---|---|
| 浏览器 | Microsoft **Edge 156.0.4314.58**（Chromium 156），隔离 `--user-data-dir` |
| 模式 | `--headless=new`、`--no-sandbox`（仅测试实例） |
| 扩展 | `browser/extension`，`--load-extension`（ID `hfiihggbmdmoofcadgmkingenjgcaend`） |
| Native Host | `dist/pdm-host/pdm-host.exe`（已注册） |
| GUI | `dist/pdm-gui/pdm-gui.exe`，临时 `APPDATA`，`QT_QPA_PLATFORM=offscreen` |
| ffmpeg | 9.0.2（`PDM_DOWNLOADER_FFMPEG` 显式指定） |
| 页面 | `http://127.0.0.1:8000/browser/mse-webm-test.html`（本地 `serve_demo.py`） |
| 素材 | `browser/mse-webm-video.webm`（VP9 320×180, 27,804 B）、`mse-webm-audio.webm`（Opus, 20,510 B） |
| 隔离 | 浏览器 profile、GUI 配置、捕获目录全部在 `build/webm-e2e/`，结束后删除 |

页面通过两个 `SourceBuffer`（`video/webm; codecs="vp9"`、`audio/webm; codecs="opus"`）
分块 `appendBuffer` 播放，2 秒素材。

## 2. 结果

### 2.1 浏览器播放与捕获

- `appendBuffer`：视频 27,804 B / 2 次，音频 20,510 B / 2 次；
- `readyState=4`，`currentTime=1.49 s`，`duration=2.021 s`；
- 捕获 2 轨：
  - `video/webm; codecs="vp9"` → `pdm-mse.webm`，27,804 B，SHA-256 `4a4003d0…6cd5`
  - `audio/webm; codecs="opus"` → `pdm-mse (1).webm`，20,510 B，SHA-256 `c428418b…d5d3`
- ffmpeg 合并：`pdm-mse.merged.webm`，47,971 B，SHA-256 `3e8718b8…93aa`；
- `mseStop` **0.079 s**，完成记录已写。

### 2.2 GUI 导入 / 归档 / 清理

临时 `APPDATA` 配置：`mse_archive=true`、`mse_delete_sources=true`。

- `downloads.json` 中 1 条任务，状态 `completed`（“已捕获”）；
- 输出归档到 `downloads/视频/pdm-mse.merged.webm`，文件存在；
- 两条源轨删除：`sources_deleted=true`、`count=2`、释放 48,314 B；
- `extra_paths` / `mse_tracks` 为空，`capture/mse` 目录无残留，manifest 已消费。

### 2.3 ffprobe

```
codec_name=vp9   codec_type=video   width=320 height=180
codec_name=opus  codec_type=audio
format_name=matroska,webm  duration=2.008
```

## 3. 结论

WebM MSE 捕获在真实浏览器、打包 Host、打包 GUI 上端到端成立：

- MIME 为 `video/webm`/`audio/webm` 时正确分轨；
- 输出按 `.webm` 命名（含同会话唯一化 `pdm-mse (1).webm`）；
- ffmpeg `-c copy` 生成可解码的 VP9+Opus WebM；
- GUI 导入后归档到视频分类，开启清理时删除两条源轨且保留合并文件。

## 4. 限制

- 使用本地 WebM MSE 测试页与生成素材，不是公网站点；公网 WebM MSE 站点未覆盖。
- 使用本机 Edge 156 隔离 headless；branded Chrome/Chrome for Testing 未用于本轮。
- 不涉及 DRM/EME；未验证 WebM 直播/长时长。

## 5. 证据

- [`bench/results/browser-verification-2026-11-08-mse-webm.json`](../bench/results/browser-verification-2026-11-08-mse-webm.json)
- [`bench/results/browser-verification-2026-11-08-mse-webm-raw.json`](../bench/results/browser-verification-2026-11-08-mse-webm-raw.json)
- 复现：`python browser/serve_demo.py --port 8000`，Edge 隔离配置加载扩展，
  `node bench/mse_webm_verify.mjs http://127.0.0.1:9334 http://127.0.0.1:8000/browser/mse-webm-test.html out.json`
