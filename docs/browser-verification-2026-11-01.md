# 浏览器扩展实机验证（2026-11-01）

本轮在真实 Chrome 中加载未打包扩展，通过 Native Messaging 把页面扫描到的媒体交给
PDM GUI，验证到“下载完成”为止。

## 1. 环境

| 项 | 值 |
|---|---|
| 浏览器 | Google Chrome **156.0.8078.49**（Windows 11） |
| 扩展 | `browser/extension` 未打包加载（开发者模式） |
| 扩展 ID | `hfiihggbmdmoofcadgmkingenjgcaend` |
| Native Host | `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.pdm.downloader.host` |
| 宿主 | `%LOCALAPPDATA%\PersonalDownloadManager\native-host\pdm-host.exe` |
| 测试页 | `http://127.0.0.1:8000/browser/test-page.html` |
| 演示服务器 | `browser/serve_demo.py`（真实 AES-128 HLS + DASH + MP4；媒体字节为合成随机数据） |
| GUI | 便携版 `dist\pdm-gui\pdm-gui.exe`（下载队列正常消费捕获） |

## 2. 步骤

```bat
python packaging\install_native_host.py --browsers chrome
python browser\serve_demo.py --port 8000
dist\pdm-gui\pdm-gui.exe
```

然后在 `chrome://extensions` 开启开发者模式 → “加载已解压的扩展程序” → 选择
`browser\extension`，打开测试页，点击扩展图标，逐个点击媒体列表里的“下载”。

## 3. 结果

`%APPDATA%\PersonalDownloadManager\downloads.json` 中出现 6 个由浏览器捕获创建的演示任务，全部
`completed`，`capture\` 目录随后为空（GUI 已消费捕获）：

| 来源 | URL | 输出 | 大小 | SHA-256（前 16） |
|---|---|---|---:|---|
| 普通链接 | `/media/main.m3u8` | `main.ts` | 786,432 | `a92a654e018c00cf` |
| 内联脚本 | `/media/scripted.m3u8` | `scripted.ts` | 786,432 | `a92a654e018c00cf` |
| iframe | `/media/iframe.m3u8` | `iframe.ts` | 786,432 | `a92a654e018c00cf` |
| Shadow DOM | `/media/shadow.m3u8` | `shadow.ts` | 786,432 | `a92a654e018c00cf` |
| 普通链接 | `/media/other.mpd` | `other.mp4` | 131,084 | `2e2f73016b3c8f2b` |
| 普通链接 | `/media/plain.mp4` | `plain.mp4` | 262,144 | `55ac5b4d0884f07d` |

原始证据：`bench/results/browser-verification-2026-11-01.json`。

## 4. 解读

- **页面扫描**：iframe、Shadow DOM 与内联脚本中的 m3u8 都出现在媒体列表并成功下载；
  说明 `allFrames` + `shadowRoot` + 内联脚本扫描链路有效。
- **HLS AES-128**：4 个 HLS 输出均为 786,432 字节且 SHA-256 相同。明文总长为
  786,432；AES-CBC/PKCS#7 后的密文会是 786,480 字节。大小与一致性同时证明扩展交接、
  密钥获取、解密、拼接全部正确。
- **DASH**：`other.mpd` 走 DASH 引擎完成，输出 131,084 字节（初始化分片 + 媒体分片）。
- **普通 HTTP**：`plain.mp4` 262,144 字节完成。
- **捕获消费**：下载完成后 `capture\` 无残留，GUI 轮询入队正常。
- 扩展弹窗对 `blob:` 视频显示 MSE/blob 提示；MSE 实验捕获见第 10 节（2026-11-01 单独验证）。
- 测试媒体是**合成随机字节**，不是可播放视频；本次验证的是协议/交接/解密/落盘链路。

## 5. 接管下载验证

在扩展弹窗勾选“接管浏览器下载”，点击测试页的
`/media/takeover.bin`（`download="takeover-demo.bin"`，服务器返回
`Content-Disposition: attachment`，131,072 字节）：

| 证据 | 值 |
|---|---|
| Chrome 下载历史 `downloads.state` | **2（CANCELLED）** |
| Chrome `total_bytes` / `received_bytes` | 0 / 0 |
| Chrome `target_path` | 空 |
| Chrome 是否留下完整文件 | 否 |
| PDM 任务 | `completed` |
| PDM 输出 | `takeover-demo.bin`，131,072 字节 |
| PDM SHA-256 | `100f03d410362a326ed448ddba141955879113106222271b907b9d073759ea62` |
| 服务器同 URL SHA-256 | `100f03d410362a326ed448ddba141955879113106222271b907b9d073759ea62` |
| 服务器日志 | 一次连接在响应中途被终止（Chrome 下载被取消），随后是 PDM 的请求 |

结论：Chrome 的下载在写入任何字节前被取消，内容由 PDM 重新下载并完成，哈希与服务器
一致；没有浏览器残留副本。

## 6. HLS master 画质候选验证

测试页新增 `master.m3u8`，包含两个 `EXT-X-STREAM-INF` 变体。扩展 fetch 并解析 master 后，
弹窗中出现：

- `自动（最佳码率）`（master）
- `1280x720 · 2400 kbps`
- `640x360 · 800 kbps`

分别点击两个变体：

| 候选 | URL | 下载 | 大小 | SHA-256（前 16） |
|---|---|---|---:|---|
| 1280x720 · 2400 kbps | `/media/high.m3u8` | 1 次，completed | 786,432 | `f8644bed566ee727` |
| 640x360 · 800 kbps | `/media/low.m3u8` | 2 次，completed | 262,144 | `a264f465fb275bd7` |

两者均与服务器按同一播放列表下载并 AES-128 解密后的内容 SHA-256 完全一致；低码率被重复
点击一次，两次输出相同。说明 master 变体展开、标签与按变体下载链路有效。

## 7. 未覆盖

- MSE/blob 实际抓取、DRM、closed shadow root、私有播放器未覆盖。
- Cookie 转发与 Firefox 接管下载路径已在 2026-11-02 单独验证，见 [`browser-verification-2026-11-02.md`](browser-verification-2026-11-02.md)。

## 8. 右键菜单与 Firefox 159 实机验证（2026-11-01 第二轮回）

用户手动在 Chrome 156 与 Firefox 159 中加载新名称扩展，右键测试页的 `plain.mp4`
链接并选择“用 PDM 下载此链接”：

| 浏览器 | 输出 | 大小 | SHA-256 |
|---|---|---:|---|
| Chrome 156.0.8078.49 | `plain.mp4` | 262,144 | `91e0b151a9079b228e55111288910f4dd4f0795630d770a010897ef706000c73` |
| Firefox 159.0 | `plain (1).mp4` | 262,144 | `91e0b151a9079b228e55111288910f4dd4f0795630d770a010897ef706000c73` |

两个任务均为 `completed`，`referer` 为测试页；`user_agent` 分别包含
Chrome/156 与 Firefox/159。服务端同 URL 的 SHA-256 与两个输出一致，`capture\` 为空。
用户确认两个浏览器的右键菜单都出现了“用 PDM 下载此链接”。

原始证据：[`bench/results/browser-verification-2026-11-01-rightclick-firefox.json`](../bench/results/browser-verification-2026-11-01-rightclick-firefox.json)。

## 9. 验证后的环境状态

- 演示服务器已停止（`http://127.0.0.1:8000` 不再监听）。
- PDM GUI 已关闭；需要继续测试时重新启动即可。
- 本轮 2 个 `plain.mp4` 演示任务及输出已按 `size + SHA-256` 校验后清理；上一轮的 HLS/DASH/接管演示文件此前也已清理。
- `downloads.json` 中仅属于 `127.0.0.1:8000` 演示的任务已移除（其余任务保留）。
- **Chrome 扩展与新 Native Host 保留**；Firefox 临时扩展在重启后失效，需要重新载入。演示服务器随时可用 `python browser\serve_demo.py --port 8000` 重启。
- MSE 验证输出 `%APPDATA%\PersonalDownloadManager\capture\mse\pdm-mse.mp4`（14,100 字节）保留供检查；不需要时可删除。
- 如不再使用浏览器集成，可执行：

```bat
python packaging\install_native_host.py --uninstall
:: 然后在 chrome://extensions 移除 PDM 扩展
```

## 10. MSE/blob 实验捕获验证（2026-11-01）

在 Chrome/Edge 中手动重载 PDM 扩展，打开
`http://127.0.0.1:8000/browser/mse-test.html`，按“开始 MSE 捕获 → 播放 MSE 演示 →
停止并保存”操作：

| 项 | 值 |
|---|---|
| 输入 fixture | `browser/mse-fixture.mp4`，14,100 字节 |
| 输出 | `%APPDATA%\PersonalDownloadManager\capture\mse\pdm-mse.mp4` |
| 输出大小 | 14,100 字节 |
| SHA-256 | `9f1fbedf20d45186d55d46d69422e662eaffdd90e36190eb797e76cd5275cfc4` |
| 与 fixture 比较 | 字节完全一致 |
| ffprobe | 单路 h264 视频流，160x90 |
| `.part` 残留 | 0 |

这次验证覆盖的是“单轨、未加密 fMP4、页面 `appendBuffer` 字节捕获、Native Messaging
分块落盘”的边界；不涉及 DRM、Blob URL 解析或双轨合并。原始证据：
[`bench/results/browser-verification-2026-11-01-mse-capture.json`](../bench/results/browser-verification-2026-11-01-mse-capture.json)。
