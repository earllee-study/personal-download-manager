# MSE 多轨实机验证（2026-11-04）

> 目标：在真实 Chromium 浏览器中验证“多 SourceBuffer 捕获 + Native Host 落盘 +
> 可选 ffmpeg 合并”完整链路，而不是只验证单元测试/夹具拼接。

## 1. 环境

| 项 | 值 |
|---|---|
| 浏览器 | **Microsoft Edge 156.0.4314.58**（Chromium 156） |
| 浏览器配置 | 隔离临时 `--user-data-dir`，不读取用户日常配置 |
| 扩展 | `browser/extension`，通过 `--load-extension` 加载（Edge 156 可用） |
| Native Host | 当前源码临时 PyInstaller 构建（0.1.0），非 `dist/` 旧 Host |
| ffmpeg | 9.0.2-essentials_build（`D:\Application\tools\ffmpeg\bin\ffmpeg.exe`） |
| 测试页 | `http://127.0.0.1:8000/browser/mse-test.html` |
| 驱动方式 | `bench/mse_browser_verify.mjs`（CDP：激活页面 → `mseStart` → `playMulti()` → `mseStop`） |

## 2. 步骤

1. `python browser/serve_demo.py --port 8000`
2. 用 `--load-extension` 启动隔离 Edge，并开启 `--remote-debugging-port=9224`
3. 用当前源码构建临时 Host，`install_native_host.py --host ... --browsers chrome`
4. 运行：

```bat
node bench\mse_browser_verify.mjs http://127.0.0.1:9224
```

脚本会打开扩展 popup、激活测试页、开始捕获、执行页面多轨演示、停止并保存。

## 3. 结果

页面日志：

```
multi appendBuffer(video=14100, audio=18030)
```

- `video.readyState = 4`，页面创建了 **2 个 SourceBuffer**
- 扩展捕获到两条独立轨道：

| 轨道 | MIME | 大小 | SHA-256 |
|---|---|---:|---|
| `track-1` 视频 | `video/mp4; codecs="avc1.42C00A"` | 14,100 B | `9f1fbedf20d45186d55d46d69422e662eaffdd90e36190eb797e76cd5275cfc4` |
| `track-2` 音频 | `audio/mp4; codecs="mp4a.40.2"` | 18,030 B | `e1d5f5fa058daefcc8e0138672b386e32d44eff72ff07566ec1b45c4c662b455` |

Host 用固定命令合并：

```
ffmpeg -hide_banner -loglevel error -y -i video.mp4 -i audio.m4a \
  -map 0:v:0 -map 1:a:0 -c copy merged.mp4
```

合并结果：

| 项 | 值 |
|---|---|
| 文件 | `pdm-mse (2).merged.mp4` |
| 大小 | 31,589 B |
| SHA-256 | `93020e31ebd55d8ed4121e54cc56d871579c89f3585270f5afee91f4cd13f70d` |
| ffprobe | `h264` 160x90 视频（2.0 s）+ `aac` 音频（2.02 s） |

输出目录：`%APPDATA%\PersonalDownloadManager\capture\mse\`。

## 4. 结论

- 多 SourceBuffer 被正确分轨，视频/音频分别写入并返回 `track`/`kind`/`mime`。
- 逐字节 SHA-256 与 `browser/mse-fixture.mp4`、`browser/mse-audio.mp4` 完全一致。
- 配置 ffmpeg 后，`mse_end_all` 成功合并为可播放的 h264+aac MP4；源轨保留。
- 合并命令固定，不经过 shell，只处理 Host 自己创建的文件。

## 5. 限制与说明

- 本轮实机是 **Edge 156**；Chrome 156 branded 的 `--load-extension` 仍不加载未打包扩展
  （popup 返回 `ERR_BLOCKED_BY_CLIENT`），因此没有用隔离 Chrome 复验。Chrome 的现有
  用户配置里扩展已加载，但默认配置 + 远程调试被安全策略拒绝，未触碰。
- 测试素材是本地 fMP4 视频/音频夹具，不是具体公网站点；DRM/EME 站点仍会被拒绝。
- 验证时使用的是**源码临时构建** Host；2026-11-04 后续已重建 `dist/pdm-host`、Host zip
  与安装包。打包 Host 的多轨 + ffmpeg 合并冒烟通过（31,589 B h264+aac），安装包内
  Host 哈希与 dist 一致。
- 复现脚本：`bench/mse_browser_verify.mjs`；原始证据：
  `bench/results/browser-verification-2026-11-04-mse-multitrack.json`。
