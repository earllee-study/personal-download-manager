# Chrome 157 MSE 多轨/停止健壮性复验（2026-11-07）

> 此前未做 Chrome 复验的原因：branded Chrome 156/157 会忽略 `--load-extension`
> 加载未打包扩展（popup 返回 `ERR_BLOCKED_BY_CLIENT`）；而使用用户默认配置 +
> 远程调试被安全策略拒绝。因此上一轮使用 Edge 156（同为 Chromium 156）验证。
>
> 本轮使用官方 **Chrome for Testing 157.0.8103.38**（与已安装 Chrome 版本相同）
> 在隔离临时配置中加载扩展，完成真实站点正常播放与“MediaSource 中途重置”两种复验。

## 1. 环境

| 项 | 值 |
|---|---|
| 浏览器 | Google Chrome for Testing **157.0.8103.38** |
| 模式 | `--headless=new`，隔离临时 `--user-data-dir` |
| 扩展 | `browser/extension`，`--load-extension`（Chrome for Testing 支持） |
| 隔离参数 | 测试实例专用 `--no-sandbox`；未使用用户日常 Chrome |
| Native Host | `dist/pdm-host` 多轨 + 停止健壮性构建，SHA-256 `5f31ea02…36f59` |
| ffmpeg | 9.0.2，`PDM_DOWNLOADER_FFMPEG` 显式指定 |
| 站点 | DASH-IF reference player v5.3.0 + Akamai BBB MPD（无 DRM） |

## 2. 正常播放捕获

播放进度：`2.50 → 4.52 → 6.53 → 8.55 → 10.56 → 12.58 s`，`readyState=4`，2 个 SourceBuffer。

| 轨道 | MIME | 大小 | SHA-256 |
|---|---|---|---:|
| `track-1` 视频 | `video/mp4;codecs="avc1.640015"` | 2,782,126 B | `8b488ed2044deb16dc79497d2d9e89a3d497a6f91ed5ffdd2a422878a3ede885` |
| `track-2` 音频 | `audio/mp4;codecs="mp4a.40.5"` | 167,793 B | `45c1232d1ccfd2c97952874586c5229361a139f71f525bcd85998f7ca5c8328a` |

合并：**2,944,027 B**，SHA-256 `4db2186f…884aed`；ffprobe 为 h264 480×270
（16.0 s）+ aac（20.05 s）。`mseStop` 返回 **0.539 s**。

## 3. 异常重置捕获

播放约 6.4 s 时执行：

```js
video.pause();
video.removeAttribute("src");
video.load();
```

| 项 | 值 |
|---|---|
| `mseStop` 返回 | **0.093 s** |
| 视频轨 | 3,623,658 B，`avc1.64001e`，SHA-256 `4975cda6…ee9f4` |
| 音频轨 | 234,608 B，`mp4a.40.5`，SHA-256 `583a5146…d233c5` |
| 合并 | 3,852,588 B，SHA-256 `4126df99…fe669` |
| ffprobe | h264 768×432（20.0 s）+ aac（28.07 s） |
| `.part` 残留 | 无 |

停止在 0.093 s 返回，两轨均完成 finalize 与 ffmpeg 合并，未出现挂起。

## 4. 结论

- Chrome 157（Chrome for Testing，同版本内核）在真实 DASH.js/Akamai 页面上
  成功捕获独立视频/音频 SourceBuffer；
- 正常停止与 MediaSource 中途重置两种路径均完成合并，停止耗时 0.54 s / 0.09 s；
- 验证使用最终 `dist` Host（多轨 + 停止健壮性），不是临时源码 Host；
- 未修改或使用用户日常 Chrome 配置；测试实例使用隔离 profile，结束后关闭。

## 5. 限制与说明

- branded Chrome 157 仍忽略 `--load-extension`；本轮使用的是官方 Chrome for Testing，
  用于自动化验证，不代表 branded Chrome 可以命令行加载未打包扩展。
- CFT headed 模式在当前会话环境退出码 3，因此使用 `--headless=new`；headless 对
  MSE/视频解码的实际播放推进正常（`currentTime` 持续增长）。
- 测试实例使用 `--no-sandbox`；仅用于隔离测试浏览器，未用于用户日常 Chrome。
- 站点是公开参考播放器 + Akamai CDN 的未加密 VOD；商业/DRM 站点不覆盖。
- 原始证据：
  - `bench/results/browser-verification-2026-11-07-mse-chrome-real-site.json`
  - `bench/results/browser-verification-2026-11-07-mse-chrome-reset-stop.json`
- 复现脚本：`bench/mse_real_site_capture.mjs`（CDP 端点 + 站点匹配 + 秒数 + 输出 + reset 秒数）。
