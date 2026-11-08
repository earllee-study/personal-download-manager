# MSE 停止健壮性修复与异常重置复验（2026-11-04）

> 背景：2026-11-04 的一次真实站点复验中，DASH.js 播放器在捕获过程中自行重置，
> `mseStop` 长时间不返回并留下 `.part`。本轮修复停止路径的超时/端口处理，并在
> **受控 MediaSource 重置**场景下复验。

## 1. 修复内容

扩展后台（`browser/extension/background.js`）：

- `mseRequest(message, timeoutMs)` 默认 **20 s** 超时；超时后关闭 Native Messaging
  端口并让所有排队请求一起失败，避免把迟到的回复匹配给下一条消息（响应错位）。
- `mseStop` 的停止 hook 等待 **5 s**；捕获链等待 **10 s**。
- 捕获链超时：关闭端口、清理状态并返回明确错误，不再无限等待；不会把后续
  `mse_end_all` 发到可能错位的端口上。
- `mse_end_all` 请求超时 **150 s**（覆盖 Host 的有界 ffmpeg 合并）。
- `mseAbort` 的捕获链等待 **5 s**，`mse_abort_all` 超时 **10 s**。

弹窗（`browser/extension/popup.js`）：

- 所有 MSE 消息使用 `sendRuntimeMessage()`，开始 30 s、停止 170 s、状态 5 s 超时；
  超时显示“宿主响应超时”，不会让按钮永久停在“正在停止”。

Host（`src/personal_download_manager/browser_host.py`）：

- ffmpeg 合并超时从 600 s 收紧到 **120 s**；超时返回 `merge_error` 并保留各轨。

## 2. 异常重置复验

环境：Edge 156.0.4314.58，隔离临时 profile，修复后的 `browser/extension`，
`dist/pdm-host` 最终多轨构建（SHA-256 `5f31ea02…36f59`），ffmpeg 9.0.2。

站点：DASH-IF reference player v5.3.0 + Akamai BBB MPD（无 DRM）。

步骤：

1. 开始 MSE 捕获；
2. 点击 Load/Play，让播放推进；
3. 约 **7.9 s** 时在页面中执行
   `video.pause(); video.removeAttribute("src"); video.load();`，销毁当前 MediaSource；
4. 1.5 s 后点击“停止并保存”。

结果：

```
reset: {"ok": true}
stop seconds: 0.10
stop: { ok: true, track_count: 2, merged: {...} }
```

| 项 | 值 |
|---|---|
| 停止返回时间 | **0.102 s** |
| 视频轨 | 1,477,052 B，`video/mp4;codecs="avc1.64001e"`，SHA-256 `38d49584…4ab7bb7` |
| 音频轨 | 134,458 B，`audio/mp4;codecs="mp4a.40.5"`，SHA-256 `b73c5db2…af4b5c4` |
| 合并文件 | 1,607,331 B，SHA-256 `69386e26…07028e3` |
| ffprobe | h264 640×360 视频（12.0 s）+ aac 音频（16.04 s） |
| `.part` 残留 | 无 |

说明：重置发生在捕获中途，Host 仍能结束已写入的两个轨道并完成 ffmpeg 合并；
停止调用在 0.10 s 内返回，未再出现此前的挂起。

## 3. 验证与证据

- 全量测试：**335 passed in 103.34s**（新增 ffmpeg 超时单测）。
- 自动化：多轨、512 MiB/轨与 1 GiB 上限、WebM 命名、ffmpeg 合并、超时报告。
- 实机：本地多轨、DASH-IF/Akamai 真实站点、受控 MediaSource 重置。
- 原始证据：`bench/results/browser-verification-2026-11-04-mse-reset-stop.json`
- 复现脚本：`bench/mse_real_site_capture.mjs`（第 5 个参数为 reset-after 秒数）

## 4. 限制

- 复验浏览器为 Edge 156；Chrome 156 多轨/重置场景未单独复验。
- 受控重置是“销毁 video src/MediaSource”，不等同于所有播放器私有重置逻辑；
  但覆盖了本次观察到的停止挂起路径。
- 站点仍是公开参考播放器 + Akamai CDN 的未加密 VOD；DRM 站点按设计拒绝。
