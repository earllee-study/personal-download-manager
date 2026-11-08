# MSE 真实站点验证（2026-11-04）

> 目标：在真实公网播放器上验证 MSE 多轨捕获，而不是只使用本地夹具。
> 选用的站点是 **DASH-IF 参考播放器 + Akamai BBB**：公开、无 DRM、使用独立视频/音频
> SourceBuffer，适合验证多轨捕获。

## 1. 环境

| 项 | 值 |
|---|---|
| 浏览器 | **Microsoft Edge 156.0.4314.58**（Chromium 156） |
| 浏览器配置 | 隔离临时 `--user-data-dir`，不读取日常配置 |
| 扩展 | `browser/extension`，`--load-extension` 加载 |
| Native Host | `dist/pdm-host` 多轨构建，SHA-256 `0a53ddb6…5ba80` |
| ffmpeg | 9.0.2（合并时显式指定 `PDM_DOWNLOADER_FFMPEG`） |
| 播放器 | DASH-IF reference player v5.3.0 |
| 清单 | `https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd` |
| DRM | 无（MPD 中无 `ContentProtection`） |

页面：
`https://reference.dashif.org/dash.js/latest/samples/dash-if-reference-player/index.html?url=https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd`

## 2. 方法

1. 启动隔离 Edge，加载扩展并将播放器页面保持暂停；
2. 通过 CDP 打开扩展 popup，激活播放器标签页，发送 `mseStart`；
3. 在页面中点击 `#btn-load`，等待 5 s，然后 `video.play()`；
4. 采样 `video.currentTime` 15 s，确认播放实际推进；
5. 发送 `mseStop`，Host 结束所有轨道并尝试 ffmpeg 合并。

驱动脚本：`bench/mse_real_site_capture.mjs`。

## 3. 结果

播放进度（秒）：

```
0.011, 2.018, 4.034, 6.050, 8.070, 10.085, 12.099, 14.105
```

`video.readyState = 4`，页面创建了 2 个 SourceBuffer。捕获到：

| 轨道 | MIME | 大小 | SHA-256 |
|---|---|---|---:|
| `track-1` 视频 | `video/mp4;codecs="avc1.640015"` | 1,383,397 B | `6de21103b47459dcf6a16ba8d5efda0facb464f901b1638cf8f8cc5dd2731bd1` |
| `track-2` 音频 | `audio/mp4;codecs="mp4a.40.5"` | 201,301 B | `f91a81ab6f87fae2aaf7df28f99b32bfaf786fb527f4c8ddef2cbd06742608b9` |

Host 用固定命令合并：

```
ffmpeg -hide_banner -loglevel error -y -i video.mp4 -i audio.m4a \
  -map 0:v:0 -map 1:a:0 -c copy merged.mp4
```

合并结果：

| 项 | 值 |
|---|---|
| 文件 | `pdm-mse (3).merged.mp4` |
| 大小 | 1,578,521 B |
| SHA-256 | `5947265a0f5d0b1a0ff2ee0c3b21adbabd8abb4231fed1a456299d14787fc324` |
| ffprobe | `h264` 480×270 视频（20.0 s，549 kbps）+ `aac` 音频（24.064 s，64 kbps） |

输出目录：`%APPDATA%\PersonalDownloadManager\capture\mse\`。

## 4. 结论

- 真实公网 DASH.js 播放器的两个 SourceBuffer 被正确分轨捕获；
- 视频/音频 MIME 与 `track`/`kind` 正确；
- 真实轨道可用 Host 的固定 ffmpeg 命令合并为可播放的 h264+aac MP4；
- 捕获期间网络持续供流，播放进度与捕获正常推进。

## 5. 说明与限制

- 第一次运行时 Edge 进程环境没有 ffmpeg，停止回复中 `merge_error` 为
  `ffmpeg not found`；随后用同一个 `_merge_mse_tracks()` 函数并显式设置
  `PDM_DOWNLOADER_FFMPEG` 对真实轨道完成合并。本地夹具的“停止即合并”已在
  2026-11-04 的 Edge 实机中端到端验证。
- 另一次带 ffmpeg 环境的运行中，DASH.js 播放器在约 6 s 时自行重置
  （`currentTime` 回到 0、`readyState=0`），`mseStop` 未在验证超时内返回，
  留下了两个 `.part` 文件（已清理）。该问题已在 2026-11-04 修复并复验：
  受控 MediaSource 重置后 `mseStop` 0.102s 返回并完成合并，见
  [`browser-verification-2026-11-04-mse-reset-stop.md`](browser-verification-2026-11-04-mse-reset-stop.md)。
- 站点是公开参考播放器 + Akamai CDN，内容是未加密 VOD；不代表所有真实站点
  （尤其 DRM 站点）都能捕获。
- 本次验证浏览器为 Edge 156；Chrome 156 的多轨复验仍未单独完成。
- 原始证据：`bench/results/browser-verification-2026-11-04-mse-real-site.json`。
