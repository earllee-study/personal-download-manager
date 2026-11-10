# Changelog

本文件记录 PDM 的主要变更。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [0.1.0] - 2026-11-10

首个公开版本。

### Added

- 多连接分段 HTTP(S) 下载、动态分段、断点续传、校验与重试。
- 全局限速、每任务限速、主机策略、代理分组、站点 UA/Cookie/Basic/Digest 认证。
- GUI：下载列表、队列并发、暂停/继续/删除、托盘、主题、状态持久化。
- 定时开始与重复计划：每天、按星期、每隔 N 天。
- 失败自动重试队列、多队列、带宽时段、下载完成后关机。
- 分类目录、通配批量 URL、AIMD 自适应并发。
- HTTP/2 多路复用与实验性 HTTP/3（QUIC）。
- FTP/FTPS、SFTP、多源镜像。
- HLS（含标准 AES-128）与 DASH（多 Period、SegmentBase/SIDX、双轨 ffmpeg 合并）。
- Chromium/Firefox 浏览器集成、媒体嗅探、Native Messaging。
- MSE/blob 多轨实验捕获（未加密 fMP4/WebM，可选 ffmpeg 合并），GUI 队列导入、后台归档与源轨清理。
- 用户主动更新检查、安装包与便携版、GitHub Actions CI / Release 流水线。
- 393 项自动化测试。

### Known limitations

- Windows 构建当前未签名，首次运行可能出现 SmartScreen 提示。
- DRM/EME、SAMPLE-AES、直播 HLS/DASH 不支持。
- DASH 仅支持未加密内容；层级化 SIDX 暂不支持。
- HTTP/3 为显式开关，不支持代理或自动回退。
- Firefox 扩展需要手动加载；临时扩展重启后失效。
- 没有长时间 soak 测试。

[0.1.0]: https://github.com/earllee-study/personal-download-manager/releases/tag/v0.1.0
