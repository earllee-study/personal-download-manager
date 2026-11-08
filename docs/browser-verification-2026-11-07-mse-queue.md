# MSE 捕获接入 GUI 队列 — 真机复验（2026-11-07）

> 目标：验证“扩展 → Native Host 完成记录 → GUI 自动导入为‘已捕获’任务 → 属性显示
> 源页面/轨道 → 删除清理全部文件”这条新链路在真实浏览器与打包产物上成立。
>
> 后续（同日）加入了后台归档：新捕获的主文件默认移动到下载目录/分类；本报告验证的是
> 导入、属性与删除链路（当时文件仍在 `capture/mse`）。归档的打包冒烟与自动化测试见
> `docs/testing.md` 第 22 节。

## 1. 环境

| 项 | 值 |
|---|---|
| 浏览器 | Microsoft **Edge 156.0.4314.58**（Chromium 156），隔离 `--user-data-dir` |
| 模式 | `--headless=new`、`--no-sandbox`（仅测试实例） |
| 扩展 | `browser/extension` 未打包，`--load-extension` 加载成功（ID `hfiihggbmdmoofcadgmkingenjgcaend`） |
| Native Host | `dist/pdm-host/pdm-host.exe`（已注册，SHA-256 `7cf019ed…53a96`） |
| GUI | `dist/pdm-gui/pdm-gui.exe`，临时 `APPDATA`，`QT_QPA_PLATFORM=offscreen` |
| ffmpeg | 9.0.2（`PDM_DOWNLOADER_FFMPEG` 显式指定） |
| 站点 | DASH-IF reference player + Akamai BBB MPD（无 DRM） |
| 隔离 | 浏览器 profile、GUI 配置、捕获目录全部在 `build/e2e-mse/` 临时目录，结束后删除 |

测试 URL：

```
https://reference.dashif.org/dash.js/latest/samples/dash-if-reference-player/index.html?url=https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd
```

## 2. 步骤

1. 以临时 `APPDATA` 启动打包 `pdm-gui.exe`（offscreen），它每 1.5 s 轮询捕获目录。
2. 以隔离配置启动 Edge，加载 `browser/extension`，打开上面的 DASH-IF 页面；
   `PDM_DOWNLOADER_CAPTURE_DIR` 指向临时 GUI 的 `PersonalDownloadManager/capture`，
   Native Host 由 Edge 继承该环境变量。
3. 运行 `node bench/mse_real_site_capture.mjs http://127.0.0.1:9333 dashif.org 12 build/e2e-mse/capture.json`：
   开始 MSE 捕获 → 点击 Load/Play → 采样 → `mseStop`。
4. 等待 GUI 轮询导入，读取临时 `downloads.json`，检查任务字段。
5. 用同一临时 `APPDATA` 构建 offscreen `MainWindow`，选中任务并调用 `show_properties()`
   （捕获 `QMessageBox.information` 文本），然后调用
   `manager.remove_many([id], delete_files=True)` 验证清理。

## 3. 结果

### 3.1 捕获

| 项 | 值 |
|---|---|
| 轨道 | 2（音频 167,793 B + 视频 989,951 B） |
| 合并 | `pdm-mse.merged.mp4`，1,153,622 B，SHA-256 `e986b448…25c3` |
| `merge_error` | `null` |
| `mseStop` | **0.519 s** |
| 完成记录 | `mse-session-b2b68aaf…c515.json` 已写 |

### 3.2 GUI 自动导入

GUI 轮询后（约 4 s 内）：

- `downloads.json` 中 **1 条任务**，状态 `completed`，状态列显示 **“已捕获”**；
- `options.capture = "mse"`，`mse_session` 与 manifest 一致；
- 源 URL 为 DASH-IF 页面 URL，页面标题为 **“DASH-IF Reference Client”**；
- `mse_tracks` = 2，`mse_merged = true`，主输出指向 `pdm-mse.merged.mp4`；
- `extra_paths` 含音频与视频源轨；
- 完成记录 manifest 已被消费（目录中无 `mse-session-*.json` 残留）。

### 3.3 属性与删除

属性和删除通过同一 `MainWindow`/`QueueManager` 代码在 offscreen Qt 中执行（见限制）：

- 属性文本包含：`来源：MSE 捕获`、`页面标题：DASH-IF Reference Client`、
  `捕获轨道：2 个`、`已用 ffmpeg 合并视频与音频`、两个 `附加文件：`；
- 删除任务并勾选删除文件后：`pdm-mse.merged.mp4`、`pdm-mse.m4a`、`pdm-mse.mp4`
  全部不存在，任务从模型移除。

## 4. 限制

- 官方 **Chrome for Testing 157** 从 Google Storage 下载约 19 KB/s（10 分钟仅 5%，
  200.8 MB），已中止；本轮使用本机已安装的 **Edge 156**（Chromium 156）完成真机验证。
  Edge 的隔离 headless 实例可正常 `--load-extension`，pdm 扩展与 Native Host 均实际工作。
- 打包 GUI 以 offscreen 运行；属性/删除不是鼠标点击，而是调用同一套
  `show_properties()` / `manager.remove_many(delete_files=True)` 代码。导入本身由打包
  GUI 的真实轮询完成。
- 未测试 Firefox（其 MAIN-world 注入仍未保证）与 branded Chrome（仍忽略
  `--load-extension`）。
- 站点为公开无 DRM 的 DASH-IF/Akamai 参考流；商业/DRM 站点不在范围。

## 5. 结论

新链路在真实浏览器 + 打包 Host + 打包 GUI 上端到端成立：扩展捕获 → Host 完成记录 →
GUI 自动出现一条“已捕获”任务（源页面/标题/两轨/合并文件正确）→ 属性信息完整 →
删除可清理合并文件与全部源轨。

原始证据：

- [`bench/results/browser-verification-2026-11-07-mse-queue.json`](../bench/results/browser-verification-2026-11-07-mse-queue.json)
- [`bench/results/browser-verification-2026-11-07-mse-queue-raw.json`](../bench/results/browser-verification-2026-11-07-mse-queue-raw.json)
