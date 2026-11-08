# 测试与基准说明

> 公开仓库不包含原始基准数据与内部证据（`bench/results/`）；相关章节仅保留通用测试方法。

## 1. 快速开始

```bat
cd D:\studyfile\claude_code\PDM
python -m pip install -e ".[dev]"
python -m pytest -q
python bench/local_bench.py
python bench/list_bench.py
python bench/http3_bench.py
python bench/h3_loss_bench.py
python bench/remote_bench.py
python -m pytest -q tests/test_update.py tests/test_release.py tests/test_i18n.py tests/test_theme.py
python packaging/check_version.py
pdm.cmd update --manifest http://127.0.0.1:8000/latest.json --download D:\updates
```

本机（Windows 11，Python 3.14.7）结果：

- `392 passed in 132.17s`（2026-11-08，MSE 大内容/P2 之后，Node 在 PATH；无 Node 时 `391 passed, 1 skipped`。HTTP/3 测试需要 aioquic，SFTP 测试需要 paramiko，FTP 测试需要 pyftpdlib）
- 基准输出见 README 第 4.3–4.11 节。

## 2. 测试服务器能力

`tests/server.py` 是一个纯标准库的 HTTP/1.1 服务器，按路由配置行为：

| 能力 | Route 字段 | 用途 |
|---|---|---|
| 单区间 206 | 默认 | 正常多连接 |
| 忽略 Range（200 全量） | `ignore_range=True` | 单连接回退 |
| chunked、无 Content-Length | `chunked=True` | 未知长度流式 |
| 拒绝带 Range 的 GET | `reject_range_requests=True` | HEAD 探测回退 |
| 每连接限速 | `throttle_bps=N` | 观察多连接收益 |
| 首个 range 请求超快 | `first_range_fast=True` | 触发动态分段 |
| 固定次数 503 | `fail_times=N` / `fail_range_times=N` | 重试 |
| 并发上限（超出即 503） | `max_parallel=N` | AIMD 退避基准 |
| 发到一半断开 | `drop_after=N, drop_times=K` | 断点重试 |
| 认证 | `require_auth=(user, password)` | Basic Auth |
| 重定向 | `redirect_to="/path"` | 3xx 跟随 |
| Content-Disposition | `content_disposition="..."` | 文件名解析 |
| If-Range 校验 | `etag` / `last_modified` | 内容变化检测 |
| TLS（ALPN http/1.1） | `TestServer(tls=True)` | HTTP/1.1 TLS 基线 |
| 请求延迟 | `TestServer(latency=0.05)` | 高 RTT 场景 |

`tests/h2server.py` 是独立的 TLS + HTTP/2 服务器（`h2` 库 + `tests/certs` 自签名证书）：支持 GET、单区间 Range、请求延迟、每流限速，并统计连接数、单连接峰值并发流、协议错误、握手 ALPN、每条请求的连接号/结束时间。`configured_alpn` 是服务器实际提供的协议列表（默认 `["h2"]`），`alpn_protocols` 是观察到的集合；双 ALPN 模式用于诊断客户端是否提供 h2。它用于验证多路复用本身，不能替代完整的 h2 实现。

`tests/h3server.py` 是独立的 TLS + HTTP/3（QUIC）服务器（`aioquic` + 同一张测试证书）：支持 GET/HEAD、单区间 Range、请求延迟、每流限速，统计连接数、单连接峰值并发流和告警的 ALPN。`tests/udp_proxy.py` 是它前面的双向丢包 UDP 中继，用于弱网正确性测试与基准；它一次只服务一个客户端地址。

`tests/ftpserver.py` 用 pyftpdlib 起一个进程内 FTP/显式 FTPS 服务器（DummyAuthorizer 挂载临时目录，支持 REST/SIZE/MDTM）。`tests/sftpserver.py` 用 paramiko 的服务端起一个真实 SSH/SFTP 服务器（生成主机密钥，支持密码与公钥认证，SFTP 子系统只读挂载临时目录）。两者都只用于测试/基准，不是产品依赖。

服务器会记录每个请求的方法、Range、If-Range、状态码、Authorization、User-Agent、Cookie、Referer 和发送字节数，测试据此断言“确实发了 N 个 Range 请求”“续传从非零偏移开始”“站点规则凭据出现在每个请求上”。

## 3. 测试矩阵

| 文件 | 重点 | 关键断言 |
|---|---|---|
| `test_headers.py` | 纯解析 | Content-Range、Retry-After、RFC 5987、Windows 文件名、大小后缀 |
| `test_probe.py` | 探测 | 206/200/416、重定向、Content-Disposition、HEAD 回退、认证、404 |
| `test_download.py` | 下载正确性 | 8 段文件字节级一致、无 Range 回退、chunked、自动命名、重定向、Auth、空文件、SHA-256、缺 Content-Range 拒绝、非 identity 编码拒绝 |
| `test_resume.py` | 续传 | 取消→续传、`--restart`、ETag 变化→重下；检查续传请求 start>0 |
| `test_retry.py` | 容错 | 503 重试、连接中断后从断点重试 |
| `test_dynamic.py` | 动态分段 | 开启后 `segments_created>4`；关闭后严格 `==4` |
| `test_limit.py` | 限速 | 1 MiB @256 KiB/s 的耗时下界 |
| `test_cli.py` | CLI | probe JSON、download、非法头、`--version` |
| `test_scheduler.py` | 调度/重试 | 错误分级、退避上限、定时开始、暂停/继续、404 不重试、预算耗尽、持久化 |
| `test_policy.py` | 主机策略/代理组 | 通配匹配、连接配额阻塞、全局限速跨下载、每主机限速、代理组优先级与两种 JSON 形式、未知组回退、CLI 参数 |
| `test_schedule_repeat.py` | 重复计划 | 每天/每周/每隔 N 天 `next_after()` 边界（周一=0、锚点对齐、未来锚点、N=1）、非法规则/天数/锚点、任务序列化、完成/失败后重排、重启滚动、覆盖旧文件、对话框 |
| `test_large_list.py` | 大列表 | 2 万行 id→行索引在更新/删除后正确、过滤/排序、只绘制可见行、更新开销不随行数线性增长 |
| `test_adaptive.py` | AIMD/HTTP2 | 加性增/乘性减、冷却、RAM 上限、503 退避、共享客户端下载正确 |
| `test_mirror.py` | 多源镜像 | 兼容性规则、轮询分配、失效切换、旧日志重映射、双镜像实际分段、镜像不匹配/无 Range/不可达时忽略 |
| `test_hls.py` | HLS | master/媒体列表解析、码率选择、相对 URI、BYTERANGE、EXT-X-MAP、AES-128（显式/sequence IV、密钥轮换、METHOD=NONE、错误密钥）、SAMPLE-AES/DRM/直播拒绝、拼接顺序、并发、取消、进度回调、SHA-256 |
| `test_queues.py` | 多队列/带宽 | 队列配置清理、按队列调度、移动任务、未知队列回退、队列列/过滤、时段解析与跨午夜、对话框、主窗口应用限速 |
| `test_browser_host.py` | 浏览器集成 | 原生消息分帧、非法消息/超大消息、ping/download、捕获文件、扩展 ID/清单/scripting 权限、页面扫描（iframe/shadow/MSE 提示/来源标签）、HLS master 变体展开、DASH 交接、手动验证页、Node 语法检查、捕获→任务、**MSE 多轨会话/分块/可配置上限与磁盘余量/WebM 命名/ffmpeg 合并/DRM 拒绝/完成记录 manifest、fMP4 与 WebM 测试页/素材/驱动脚本、Firefox Gecko base64 分支与 128 版本门槛** |
| `test_mse_capture.py` | MSE→GUI | 完成记录解析、合并文件优先/最大视频回退、越界与缺失文件拒绝、已捕获任务字段、队列插入不启动 worker、删除全部轨道、主窗口按会话去重 |
| `test_capture_archive.py` | MSE 归档 | 同卷移动、唯一命名、大小/SHA 校验、跨卷复制与失败清理、取消、管理器成功/失败/删除等待/重启续传/关闭开关、主窗口自动归档 |
| `test_capture_cleanup.py` | MSE 源轨清理 | 默认关闭的归档后清理、未合并不清理、手动保留主文件、越界路径保护、归档中拒绝、主窗口菜单动作 |
| `test_categories.py` | 分类目录 | 默认扩展名匹配、未知扩展名回退、自动/不分类/指定分类、关闭分类、子目录净化、分类表去重 |
| `test_urlbatch.py` | 通配 URL | 数字/字符/步长/零填充、`{a,b}`、多行与注释、去重、展开上限、非法范围 |
| `test_site_policy.py` | 站点认证 | 探测/每个分段/镜像主机/HLS 分片都带站点 UA/Cookie/Basic 认证、规则覆盖任务值、未设置项沿用任务值、CLI `--policy` 端到端 |
| `test_batch_cli.py` | 批量 CLI | `batch` 展开并逐个下载、分类子目录、非法模式/未知分类退出码 |
| `test_http2.py` | HTTP/2 | ALPN 协商、8 段共用一条连接、字节级完整、明文退化、h1 TLS 基线、连接号/ALPN/结束时间元数据 |
| `test_dash.py` | DASH | ISO-8601 时长、SegmentTemplate（编号/时间线/重复/继承）、SegmentList、嵌套 BaseURL、轨道选择、直播/加密拒绝、端到端拼接、站点凭据、CLI、双轨下载、ffmpeg 路径解析、合并命令、无 ffmpeg 回退、多 Period 逐段拼接、SegmentBase/SIDX 与整文件回退 |
| `test_http3.py` | HTTP/3 | ALPN=h3、8 段一条 QUIC 连接、字节级完整、探测元数据、站点请求头、HLS 分片共连接、2% 丢包后 SHA 仍正确、代理/明文拒绝、CLI `--http3` 与互斥参数 |
| `test_ftp.py` | FTP/FTPS | 字节级下载、SIZE/MDTM 探测、多连接分段/动态拆分、取消后 REST 续传、FTPS 多连接、FTP/FTPS 主动模式（PORT/EPRT）、REST 不可用回退单流、错误密码/缺失文件、SHA-256、代理拒绝、CLI `--ftp-active` 与协议参数互斥 |
| `test_sftp.py` | SFTP | 下载/探测、取消后 seek 续传、密码/私钥认证、未知主机拒绝/known_hosts、缺失文件、SHA-256、代理拒绝、CLI |
| `test_gui.py` | GUI/队列 | 配置钳制与持久化、模型过滤、中断状态、队列端到端、暂停后续传、主机规则/代理组对话框、分类/批量对话框、HTTP/2 与 HTTP/3 互斥、FTP/SFTP URL 与 SFTP 选项、FTP 主动模式、更新清单/ffmpeg 路径设置与更新对话框状态 |
| `test_update.py` | 自动更新 | 版本比较、清单字段/HTTPS/回环规则、检查更新、流式下载、SHA-256/大小校验、取消清理、`.exe` 启动校验、CLI `update` JSON/下载/启动、清单生成工具 |
| `test_release.py` | 发布/CI | pyproject/`__init__`/Inno 三处版本一致、版本漂移与 tag 期望、缺失文件、CLI `--version` 与包版本同源 |
| `test_i18n.py` | 界面多语言 | 英文目录覆盖所有 GUI 中文串、语言解析/持久化、重复计划英文描述、状态/表头/提示、更新/添加/设置对话框、主窗口、Show 过滤器、标准按钮中英本地化 |
| `test_theme.py` | macOS 风格 UI | 浅/深色调色板与 QSS、表单右对齐、表格无边框/无网格/隐藏行号、状态色 tone、13 个线性图标有像素、主窗口圆角卡片与行高 |
| `test_shutdown.py` | 下载完成后关机 | 策略（空列表/终态/暂停/计划/重试/未知状态）、平台命令与 `shell=False`、设置 clamp/持久化/对话框、倒计时一次触发、取消、新增任务取消、失败上报、真实下载后触发、主窗口开关与倒计时标签 |

## 4. 基准方法论

`bench/local_bench.py` 用“每连接 4 MiB/s”的本地服务器，避免公网带宽波动：

- 32 MiB 文件，连接数 1/2/4/8/16；每连接限速 4 MiB/s。
- 关闭动态分段，保证测到的是纯 N 路并行。
- 动态分段对比：16 MiB、4 连接、其中 1 个连接先以 64 MiB/s 完成，其余 3 个 2 MiB/s；对比 static/dynamic。

结果解读要点：

1. **1→8 近线性**说明分段/写入/调度没有串行瓶颈。
2. **16 连接回退**是测试环境（Python 服务器 + GIL + 回环 + TLS 握手）造成的，不是引擎上限；真实环境要看服务端是否按连接限速。
3. **动态分段收益**只在连接速度不均时出现；速度均匀时动态拆分没有意义，`--no-dynamic` 可减少碎片。

## 5. 公网验证

```bat
pdm.cmd download "https://proof.ovh.net/files/10Mb.dat" -o a.dat -n 1 --no-progress
pdm.cmd download "https://proof.ovh.net/files/10Mb.dat" -o b.dat -n 8 --no-progress
pdm.cmd download "https://proof.ovh.net/files/10Mb.dat" -o c.dat -n 16 --min-segment 256K --no-progress
```

三个文件的 SHA-256 应一致。本机结果：

```
e5b844cc57f57094ea4585e235f36c78c1cd222262bb89d53c94dcb4d6b3e55d
```

该链路总带宽约 1.5 MiB/s，因此 1 连接已经打满；8/16 连接不会更快，16 连接因握手开销略慢。这正好验证了“多连接只解决每连接限速，不解决总带宽瓶颈”。

## 6. 本地与公网基准

原始基准数据与内部报告不随公开仓库发布；本地基准方法与通用测试见其他章节。

## 7. 自适应并发（AIMD）

`python bench/adaptive_bench.py`：16 MiB、每连接 4 MiB/s、服务器**最多接受 3 个并发分段**（超出返回 503 + `Retry-After: 1`），3 次中位数：

```
          mode  elapsed  retries       503s  peak  final
    fixed -n 8    3.28s       10         10     8      8
      adaptive    2.70s        1          1     4      2
```

- 固定 8：5 条连接被持续拒绝、各自重试，共 10 次 503；
- AIMD：2 起，涨到 4 被拒一次即减半，1 次 503 后完成；
- AIMD 的加性增/乘性减与冷却逻辑在 `tests/test_adaptive.py` 里按虚拟时钟验证，不依赖真实等待。

HTTP/2：`--http2` 让所有分段共用一个 httpx 客户端。公网 `https://proof.ovh.net/files/10Mb.dat` 协商到 `HTTP/2`，8 段下载 SHA-256 与单连接一致。本地多路复用由 `tests/h2server.py` + `tests/test_http2.py` 自动化覆盖（见第 10 节），`test_adaptive.py::test_http2_client_is_shared_and_still_correct` 另覆盖共享客户端路径的正确性。

## 8. 多源镜像

`python bench/mirror_bench.py`：两个本地服务器、同一 32 MiB 文件、每连接 2 MiB/s、每主机连接上限 4（第二个服务器用 `localhost` 访问以被视为独立主机），3 次中位数：

```
          mode   elapsed        speed
 single source     4.10s      7.80 MiB/s
   two mirrors     2.13s     15.04 MiB/s
```

单源被主机连接上限卡在约 8 MiB/s，双镜像接近线性翻倍。若没有主机级限制（只有每连接限速），回环上镜像不会更快——瓶颈是每连接的限速而不是主机。

一致性：镜像在下载开始前各自探测一次，要求 `total_size` 相同且强 ETag（无 ETag 时用 Last-Modified）一致；不满足的镜像被丢弃并记录日志。分段带 `source` 索引写入续传日志，旧日志加载时按当前镜像数取模。

## 9. HLS

`python bench/hls_bench.py`：8 个 512 KiB 分片、每连接 512 KiB/s，3 次中位数：

```
 connections   elapsed
           1     8.16s
           4     2.17s
```

解析与下载的正确性由 `tests/test_hls.py` 覆盖：master 选码率、相对 URI、BYTERANGE（含无 offset 的续接）、EXT-X-MAP、AES-128（显式 IV、media-sequence 推导 IV、密钥轮换、METHOD=NONE、错误密钥长度）、SAMPLE-AES/DRM/直播拒绝、拼接顺序、SHA-256、4 连接并行耗时下界。

AES-128 使用 `cryptography` 的 AES-CBC + PKCS#7：按 `EXT-X-KEY` 的 URI 拉取 16 字节密钥（按 URI 缓存），每个分段用显式 IV 或 `media sequence` 大端 16 字节作为 IV 解密；`METHOD=NONE` 清除密钥，密钥可轮换。SAMPLE-AES、非 identity `KEYFORMAT` 与 DRM 直接拒绝。加密初始化分片仅在 `EXT-X-MAP` 带 IV 时解密。

测试服务器只能托管普通字节路由，播放列表通过 `Route.content` 在服务器启动后写入；这足以覆盖解析 + 并发 + 拼接，但不涉及真实编码/封装。

## 10. HTTP/2 多路复用

`python bench/http2_bench.py`：本地 TLS 服务器，16 MiB、每请求 50 ms 延迟、每流 2 MiB/s，3 次中位数：

```
      mode   elapsed        speed  conns  streams
   h1 -n 1     8.17s      1.96 MiB/s      1        1
   h1 -n 8     1.49s     10.71 MiB/s      8        1
   h2 -n 8     1.22s     13.11 MiB/s      2        8
```

- `conns` 为传输阶段实际 TCP/TLS 连接数（h2 另有 1 条探测连接）；`streams` 为单连接并发流。
- h1 侧为 `TestServer(tls=True)` + `latency`，h2 侧为 `tests/h2server.py`；两边使用同一证书、同样的延迟与限速。
- 不限速时（`--throttle none --size 32M --latency 0.02`）：h1 -n 1 140.61、h1 -n 8 46.70、h2 -n 8 71.50 MiB/s。回环上单连接最快，h2 的意义是用更少连接获得并发。
- 绝对值受 Python 测试服务器限制；可复现的是连接/流行为与相对差异。

自动化断言（`tests/test_http2.py`）：ALPN 必须为 `h2`，传输阶段只有 1 条连接，单连接峰值并发流 ≥ 4，文件 SHA 与源一致；`--http2` 对明文 HTTP/1.1 服务器必须正常退化为普通下载。

## 11. DASH

`dash.py` 解析静态单/多 Period MPD，可下载**一条**轨道（视频或音频），或用 `--track both` 依次下载两轨并调用外部 ffmpeg `-c copy` 合并。多 Period 会逐段选择轨道并把每段的初始化分片插到对应位置；`SegmentBase`/`indexRange` 通过单文件 MP4 的 `sidx` 媒体引用按字节范围下载。与 HLS 共享 `streaming.py` 的分片并发/重试/限速/进度。

正确性由 `tests/test_dash.py` 覆盖：

- `SegmentTemplate` + `@duration` 与零填充 `$Number%03d$`；
- `SegmentTimeline`（`$Time$`、`r` 重复、`r=-1` 由下一条 `S@t` 或 Period 时长收敛）；
- `SegmentList`（`Initialization`、`mediaRange`）、`SegmentTemplate` 从 AdaptationSet 继承；
- MPD/Period/AdaptationSet/Representation 四级 BaseURL 拼接；
- 单文件 Representation（无分片清单）；
- 轨道选择：best/worst/高度/索引/RepresentationID；
- 拒绝：`type=dynamic`、`ContentProtection`、层级化 SIDX（`reference_type=1`）；
- 多 Period：逐 Period 选择轨道、每段 init 插入位置、跨 Period 拼接；
- `SegmentBase`：`indexRange` 元数据、SIDX 解析、按 Range 下载、无 `indexRange` 整文件回退；
- 端到端：初始化分片 + 媒体分片顺序拼接、站点认证/UA 覆盖、CLI `dash`；
- 双轨：`--track both` 下载两轨、ffmpeg 目录/缺失路径解析、`-c copy` 合并命令、无 ffmpeg 时保留 `.mp4` + `.audio.m4a`、CLI 合并路径。

单轨输出是 fragmented MP4 的简单拼接，不做 remux；`--track both` 在有 ffmpeg 时合并为一个 MP4（`-c copy`，不重编码），没有 ffmpeg 时保留两轨。没有 DASH 专用基准：分片并发链路与 HLS 共用同一套代码，性能由 `bench/hls_bench.py` 覆盖。

真实合并验证（2026-11-01）：用 ffmpeg 4.3.1 生成 4 秒双轨 DASH 夹具（mpeg4 + AAC），`--track both --ffmpeg` 输出 84,308 字节；`ffmpeg -i` 确认两条流，`-f null -` 解码通过。安装 ffmpeg 9.0.2 到 `D:\Application\tools\ffmpeg` 后复验，输出 84,392 字节，`ffprobe` 显示 `mpeg4` 视频 + `aac` 音频。删除/不配置 ffmpeg 时输出 `.mp4` + `.audio.m4a` 两个文件并返回 `extra_paths`。

## 12. 大列表（GUI 模型/视图）

`python bench/list_bench.py --sizes 1000,5000,20000 --repeats 5`：offscreen Qt，构建 `DownloadTableModel` + `TaskFilterProxy` + `QTableView`（进度列用真实委托），每档重复 5 次取中位数。指标：

- `build`：逐个 `add_task` 建 2 万行模型；
- `paint`：`viewport().grab()`（只应绘制可见行，因此与行数无关）；
- `update`：本应随 worker 进度到达的 200 次 `update_task`；
- `sorted update`：先按状态排序，再做同样的更新（Qt 代理会动态重排）；
- `sort`：按状态列排序一次；
- `filter`：设置搜索词并重建过滤；
- `remove100`：走 GUI 的批量删除路径 `remove_rows`；
- `save`：`downloads.json` 的序列化耗时/大小（`tracemalloc` 另报 Python 堆峰值）。

本机（2026-10-30）结果见 README 4.9；优化前数据保存在 内部基准结果（未公开），用于对比“O(行数) 扫描 → O(1) 索引”的改动。

回归测试 `tests/test_large_list.py` 不依赖绝对耗时（除一个宽松上限）：它用 1k 与 20k 两个模型各做 1000 次更新，要求 20k 的耗时不比 1k 慢 5 倍以上（另加 50 ms 余量）；如果 id→行索引丢失、`update_task` 退化为线性扫描，这条会失败。

## 13. HTTP/3（QUIC）与丢包

`bench/http3_bench.py`：三套本地 TLS 服务器（`TestServer(tls=True)`、`H2Server`、`H3Server`），16 MiB、50 ms 请求延迟、每流 2 MiB/s，3 次中位数；另外跑 h3 -n 16。指标含实际连接数与单连接峰值并发流。

`bench/h3_loss_bench.py`：`H3Server` 前接 `tests/udp_proxy.py`，按 0/1/3/5% 双向丢 UDP 包，4 MiB、4 段、3 次中位数，并用 SHA-256 判定每次下载。

本机结果见 README 4.10。可复现的结论：

- h3 的 8/16 个分段确实在**一条** QUIC 连接上（探测另占一条），ALPN 为 `h3`；
- 无丢包回环上 aioquic 的 Python 包处理是瓶颈（约 5 MiB/s，16 流不比 8 流快），h2 约 13 MiB/s；这不是引擎并发上限；
- 丢包升高时吞吐下降但 SHA-256 全部正确，说明 QUIC 重传与引擎的分段重试配合正常；
- 没有“TCP 同等丢包”的对照（用户态无法公平注入），因此不声称 h3 在回环上更快。

自动化测试 `tests/test_http3.py` 只用真实 QUIC 服务器，不 mock aioquic；没有安装 aioquic 时整组 `pytest.importorskip` 跳过。

## 14. FTP / SFTP

`bench/remote_bench.py`：本地 pyftpdlib / paramiko 服务器、64 MiB 文件、3 次中位数，比较原始 `ftplib.retrbinary` / `paramiko.get` 与 PDM 的单流驱动，每次校验 SHA-256。本机结果见 README 4.11：

- 无丢包回环：FTP 原始约 1004 MiB/s，`PDM ftp -n 1` 约 962 MiB/s（单流开销很小），`-n 8` 约 91 MiB/s（多连接的固定成本，回环上反而更慢）；SFTP 原始约 121 MiB/s，引擎约 98 MiB/s（约 19% 开销）。
- 每连接限速场景才是 FTP 多连接的目标；本地基准见 `bench/remote_bench.py`。
- SFTP 始终单流；开销来自 part/journal、进度采样、限速桶和最终 SHA/改名。

正确性测试 `tests/test_ftp.py` / `tests/test_sftp.py` 连的是真实服务器进程内实现，不 mock `ftplib`/paramiko。FTP 多连接测试检查 `segments_created>1`、`peak_connections>1`、SHA 一致；主动模式测试还检查服务器收到的 `PORT`/`EPRT`（且没有 `PASV`/`EPSV`），明文与显式 FTPS 各覆盖多连接。默认被动模式的测试断言收到 `PASV`/`EPSV` 且没有 `PORT`/`EPRT`。取消后续传测试断言 `resumed=True` 且最终 SHA 正确。`tests/ftpserver.py` 的 `allow_rest=False` 用于验证“REST 不可用时回退单流”，`modes` 记录数据通道协商方式。SFTP 主机密钥测试分别覆盖：空 known_hosts → 拒绝；服务器公钥写入 known_hosts → 接受；`--sftp-auto-host-key` → 接受；私钥（RSA）认证。

FTP 主动模式在 CLI 中的非法组合（HTTP、SFTP）返回退出码 2；GUI 复选框写入任务的 `ftp_active`，worker 只对 `ftp/ftps` 传给引擎。

## 15. 自动更新

`tests/test_update.py` 用 `tests/server.py` 托管清单与安装包（回环 HTTP 被显式允许），覆盖：

- `version_is_newer()` 的数字点分比较（`v` 前缀、补零、预发布后缀视为同数字版本）；
- 清单必填字段、64 位 SHA-256、`.exe` URL、大小范围、notes 大小、URL 凭据拒绝；
- 非回环 HTTP 清单被拒绝，HTTPS/回环 HTTP 可用；重定向后的最终 URL 也复查；
- `check_update()` 在版本相同时返回 `None`；
- `download_update()` 的 `.part` 流式写入、进度回调、原子改名；重复调用复用已校验文件；
- SHA-256 不匹配、大小不匹配、取消下载时不留下最终文件或 `.part`；
- `launch_installer()` 只接受存在的 `.exe`，并用 `Popen([path], shell=False)`；
- CLI `update` 的 `--json` / `--download` / `--launch`，以及非 HTTPS 清单的退出码 1；
- `packaging/make_update_manifest.py` 生成的清单能被客户端同一个 `parse_manifest()` 接受。

GUI 侧 `test_gui.py` 覆盖更新清单的设置持久化，以及 `UpdateDialog` 在“已是最新/发现新版本”两种状态下的文案、按钮与说明显示（不联网）。完整清单格式与发布步骤见 [`updating.md`](updating.md)。

## 16. HTTP/2 本地验证

HTTP/2 多路复用由本地 TLS+h2 测试服务器与 `tests/test_http2.py` 覆盖。

## 17. 失败排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `server ignored the Range request` | 服务器中途不支持 Range，或 `If-Range` 校验失败 | `--connections 1 --restart` |
| 续传后 `resumed: no` | ETag/Last-Modified/总大小变化，或 part 文件缺失 | 查看日志；必要时 `--restart` |
| 16 连接比 8 慢 | 总带宽已满 / 握手开销 / 服务端每 IP 限速 | 降连接数，观察瓶颈 |
| `HTTP 401` | 需要认证 | `--auth user:pass`，必要时 `--auth-type digest` |
| `HTTP 407` | 代理认证 | 使用带凭据的代理 URL，或先配置代理 |
| FTP `PORT`/`EPRT` 被拒绝或数据连接超时 | 服务器不支持主动模式，或 NAT/防火墙阻止服务器回连本机高位端口 | 去掉 `--ftp-active` 改用默认被动模式；或放行服务器到本机的回连 |
| `update ... must use HTTPS` | 清单/安装包用了公网 HTTP | 改为 HTTPS；本地测试只能用 `localhost`/`127.0.0.1` |
| 更新 SHA-256 mismatch | 清单与安装包不一致、被截断或代理改写了内容 | 重新生成清单（`make_update_manifest.py`）；检查下载链路 |
| 更新下载后无法启动安装器 | 非 Windows / 非 `.exe` / 文件为空 / 权限 | 使用 Windows 安装包；确认文件存在且完整 |
| `check_version.py` 报 version mismatch | `pyproject.toml`、`__init__.py`、Inno 默认值不同步 | 三处改成同一版本；发布 tag 必须是 `vX.Y.Z` |
| 切换语言后界面没变 | 语言更改在重启后生效 | 关闭并重新启动 GUI |
| 浅色/深色主题下图标或进度条颜色不对 | 图标未按主题重生成，或 delegate 用了错误的 palette | 切换主题后由 `MainWindow._refresh_icons()` 重建；进度条用应用 palette 判断深浅 |
| 英文界面仍有中文 | 新增 UI 字符串未加入 `i18n._EN`，或属于分类/队列等用户数据 | 运行 `tests/test_i18n.py`；用户数据按原样显示是预期 |
| Release workflow 找不到 `ISCC.exe` | runner 未安装 Inno Setup 或路径变化 | workflow 已 `choco install innosetup`；本地用 `%LOCALAPPDATA%` 路径 |
| Release 签名失败 | 未配置/错误证书、signtool 缺失、时间戳服务不可达 | 检查 `WINDOWS_CERT_*` secrets 与 Windows SDK；无证书时不要配置 secret |
| `TLS certificate error` | 自签名/企业代理 | 仅测试环境使用 `--insecure` |
| `ChecksumMismatch` | 文件变化或哈希错误 | 重新下载；确认 `--sha256` |
| GUI 测试偶发进程崩溃 | offscreen Qt 下队列信号在测试对象回收后才投递 | 新增 GUI 网络测试后在 `manager.shutdown()` 后调用 `settle()`（清空事件队列） |
| `hierarchical SIDX index references are not supported` | SegmentBase 的 SIDX 引用了嵌套索引 | 当前只解析顶层 `sidx` 媒体引用；可用工具展开索引 |
| `segment index contained no SIDX media references` | indexRange 指向的不是有效 SIDX 或全部为嵌套索引 | 检查 `indexRange` 与 MP4 索引结构 |
| `DRM/EME MSE capture is not supported` | 页面使用 EME/DRM 或 Sink 的 MIME 带 encv/enca | 只捕获未加密 fMP4/WebM；不做解密 |
| `MSE track exceeds 8 GiB` / `MSE capture exceeds 16 GiB` | 单轨或总会话超过默认上限 | 用 `PDM_MSE_MAX_TRACK_BYTES` / `PDM_MSE_MAX_TOTAL_BYTES` 调整；当前仍是有界实验 |
| `MSE capture stopped: low disk space` | 捕获盘剩余空间低于 `PDM_MSE_MIN_FREE_BYTES`（默认 1 GiB） | 释放磁盘空间后重试；已捕获数据会保存 |
| `no MSE data was captured` | 开始捕获后没有 appendBuffer，或轨道未成功建立 | 先开始捕获再播放；多轨页面需同时产生视频/音频 appendBuffer |

## 18. CI 与发布脚本

`.github/workflows/ci.yml` 在 `windows-latest` 上跑 Python 3.13/3.14 矩阵：

1. `pip install -e ".[dev]"`；
2. `python packaging/check_version.py`（三处版本一致）；
3. `python -m compileall -q src tests packaging`；
4. PowerShell Parser 校验 `packaging/sign_windows.ps1`；
5. `python -m pytest -q --junitxml=pytest.xml`，失败也上传 JUnit artifact。

`.github/workflows/release.yml` 由 tag `vX.Y.Z` 或手动 `version` 触发：版本一致性 → 全量 pytest →
构建 PyInstaller 三产物 →
Inno Setup（`/DAppVersion=$VERSION`）→ 三个 zip → 可选签名 → `SHA256SUMS.txt` 与
`latest.json` → GitHub Release。签名 secrets 为 `WINDOWS_CERT_PFX_BASE64` /
`WINDOWS_CERT_PASSWORD`；缺失时输出 warning 并发布未签名产物。完整说明见
[`ci.md`](ci.md)。

本地可先验证不依赖远端的部分：

```bat
python packaging\check_version.py
python -m pytest -q
python -m compileall -q src tests packaging
python packaging\make_release_assets.py --version 0.1.0 --base-url https://downloads.example.com/personal-download-manager/
powershell -NoProfile -Command "$t=$null; $e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'packaging/sign_windows.ps1'), [ref]$t, [ref]$e); if($e){exit 1}"
```

注意：当前目录不是 git 仓库（也无 `gh`/远端/凭据），workflow 尚未在 GitHub 远端执行；
本地没有代码签名证书与 `signtool`/Windows SDK，因此只验证了签名脚本语法与签名管线分支，
未实际签出可信二进制。2026-11-08 的 PyInstaller 重建尝试在 CLI COLLECT 阶段被系统内存
压力停止（可用约 0.4 GiB/16 GiB）；释放内存后已重跑成功：三产物 → ISCC →
`make_release_assets.py` → 临时安装/卸载冒烟。当前 dist 为 2026-11-08 版本。

## 19. 界面多语言

`tests/test_i18n.py` 覆盖：

- `_EN` 英文目录对 `main_window.py` / `dialogs.py` / `model.py` / `update_dialog.py`
  中所有中文界面字面量的完整性（AST 扫描；docstring 与“主队列”数据除外）；
- `set_language("system"/"zh"/"en")`、`follow system` 的 `zh*` 判断、`AppSettings.language`
  的钳制与 JSON 往返；
- `translate_repeat()`：每天、每周多日、每隔 N 天；
- 模型状态、表头、重复描述与状态提示（`Repeat: Every 2 days at 06:00`）；
- 更新对话框、添加对话框、设置对话框的代表性英文文本；主窗口标题/菜单/动作/占位符；
- `install_translation_filter()` 对 Show 事件的自动翻译（QLabel 与标准 OK/Cancel 按钮）。

覆盖范围：主窗口、添加/批量/设置/定时/更新，以及主机规则、代理分组、分类、队列、
带宽时段编辑器。分类名、队列名等用户数据不翻译；语言切换在重启后生效。

手工审计脚本（离屏，无字体环境也可用）会遍历所有控件文本并报告英文模式下的中文残留：

```bat
set QT_QPA_PLATFORM=offscreen
python -c "from PySide6.QtWidgets import QApplication; from personal_download_manager.gui import i18n; ..."
```

实际提交的回归只依赖 `test_i18n.py`，不依赖截图或字体。

## 20. macOS 风格 UI

`tests/test_theme.py` 检查：

- `apply_theme("light"/"dark")` 设置 macOS 调色板（`#F5F5F7` / `#1C1C1E`）与对应 QSS；
- `polish_widget_tree()` 把 `QFormLayout` 标签右对齐，表格无边框、无网格、无交替行、隐藏行号；
- `set_label_tone()` 的 `secondary/error/warning` 属性；
- 13 个 `symbol_icon()`（add/play/pause/trash/file/folder/clock/sliders/check/copy/info/refresh/batch）在 16×16 下都有非透明像素；
- 主窗口存在 `ContentCard` 圆角卡片、表格 `NoFrame`、隐藏行号、36px 行高。

`tests/test_i18n.py` 另检查中文“确定/取消”与英文 “OK/Cancel” 的标准按钮本地化。界面截图 `docs/gui.png` 使用真实字体、演示任务生成（浅色）；深色主题可通过设置切换，样式由同一套颜色字典生成。

## 21. 浏览器扩展实机验证

2026-11-01 在真实 **Chrome 156.0.8078.49** 中手动加载 `browser/extension`，注册 Native Host，并用 `browser/serve_demo.py` 提供 AES-128 HLS、DASH、MP4：

```bat
python packaging\install_native_host.py --browsers chrome
python browser\serve_demo.py --port 8000
dist\pdm-gui\pdm-gui.exe
:: Chrome: chrome://extensions -> 开发者模式 -> 加载已解压的扩展程序 -> browser\extension
:: 打开 http://127.0.0.1:8000/browser/test-page.html
```

结果：页面扫描发现来自普通链接、内联脚本、iframe、Shadow DOM 的 6 个媒体 URL，全部经 Native Host 入队并 `completed`：

- 4 个 HLS 输出均为 786,432 字节且 SHA-256 相同（明文总长；密文应为 786,480，证明 AES-128 解密正确）；
- DASH 输出 131,084 字节，普通 MP4 262,144 字节；
- `capture\` 目录在 GUI 消费后为空。

接管下载也已验证：勾选“接管浏览器下载”后点击测试页的 `takeover.bin`，Chrome 下载历史为 `CANCELLED`、0 字节、无 target path；PDM 任务完成 131,072 字节，SHA-256 与服务器一致，没有浏览器残留副本。

HLS master 画质候选也已在同一环境验证：`master.m3u8` 展开为 `1280x720 · 2400 kbps` 与 `640x360 · 800 kbps`，分别下载 786,432 / 262,144 字节，SHA-256 与服务器解密内容一致。

右键菜单与 Firefox 159 实机也已验证（2026-11-01）：Chrome 156 / Firefox 159 右键测试页 `plain.mp4`，两个任务均 `completed`、262,144 字节，SHA-256 与服务端一致；原始数据见 内部基准结果（未公开）。完整表格、限制和清理步骤见 [`browser-verification-2026-11-01.md`](browser-verification-2026-11-01.md)，原始数据见 内部基准结果（未公开）。Cookie 转发（Chrome 156）与 Firefox 159 接管下载已验证，见 [`browser-verification-2026-11-02.md`](browser-verification-2026-11-02.md)；MSE 实验捕获已验证（见第 22 节）。

## 22. MSE/blob 多轨实验捕获

`mse_capture.js`（MAIN world）hook `MediaSource.prototype.addSourceBuffer`、
`SourceBuffer.prototype.appendBuffer` 与 `SourceBuffer.prototype.changeType`；每个
SourceBuffer 分配独立 `track`，MIME 可用时按 `video/`/`audio/` 分类，缺失时从 init
字节嗅探 fMP4 `hdlr`（vide/soun）或 WebM `TrackType`。`mse_bridge.js`（isolated world）
把 `ArrayBuffer` 转 base64 并带 `track/kind` 交给 service worker；Firefox 下由 MAIN world
先转 base64 字符串（Gecko 的 content script 不能访问页面 principal 的 ArrayBuffer），
bridge 直接转发；background 按 track
维护独立 Native Messaging 会话，调用 `mse_start`/`mse_chunk`/`mse_end_all`；
`browser_host.MseSessions` 写入 `%APPDATA%\PersonalDownloadManager\capture\mse\mse-<id>.part`，
结束时原子改名并返回 size/SHA-256。

边界与拒绝项：

- 多轨：每个 SourceBuffer 独立轨道，最多 4 个会话；视频默认 `.mp4`/`.webm`，
  音频默认 `.m4a`/`.webm`；
- 未加密 fMP4/WebM：MIME 含 `encv`/`enca`/Widevine/PlayReady/FairPlay/ClearKey 或调用
  `requestMediaKeySystemAccess` 时立即停止；
- 4 MiB/分块；默认 8 GiB/轨、16 GiB/总会话（`PDM_MSE_MAX_TRACK_BYTES`/`PDM_MSE_MAX_TOTAL_BYTES` 可调）；`PDM_MSE_MIN_FREE_BYTES`（默认 1 GiB）保留磁盘余量；超过上限/空间不足时停止并保存已有数据；
- 可选合并：配置 ffmpeg（`PDM_DOWNLOADER_FFMPEG`、GUI `ffmpeg_path` 或 PATH）时，
  `mse_end_all` 对最大的视频+音频执行固定 `-map 0:v:0 -map 1:a:0 -c copy`，
  输出 `<stem>.merged.mp4/.webm`；源轨保留。ffmpeg 缺失/失败时只保留各轨并返回
  `merge_error`；
- `mse_end`/`mse_end_all` 写出 `mse-session-*.json` 完成记录；GUI 轮询后作为“已捕获”任务导入队列（合并文件优先、按会话去重、路径必须在 `capture/mse` 内）；文件名做 Windows 净化；宿主只对自建文件执行 ffmpeg，不经过 shell。

自动化覆盖（`tests/test_browser_host.py`）：

- `mse_start` → `mse_chunk`（seq 连续）→ `mse_end` 的字节级拼接、size/SHA-256；
- 多轨 `mse_end_all`：无 ffmpeg 时保留两条轨；有 ffmpeg 时验证合并命令、输出和源轨；
- 默认每轨 8 GiB / 会话 16 GiB 上限（测试中 monkeypatch 为小值）、低磁盘停止、WebM 命名、`mse_abort_all`；
- DRM MIME、错误 sequence、4 MiB 分块上限、文件名路径净化、空会话结束；
- 扩展 popup/background 接线、`mse_capture.js`/`mse_bridge.js`/`mse-test.html`、
  视频+音频 fMP4 fixture、Node 语法检查。

GUI 导入覆盖（`tests/test_mse_capture.py`）：

- 合并文件优先；无合并时选择最大视频轨（没有视频再选音频）；
- `make_mse_task()` 输出 `completed + options.capture="mse"`，状态列显示“已捕获”，
  `extra_paths` 包含全部非主轨；
- 越界路径（`capture/mse` 之外）、缺失文件、混合记录中的坏条目被拒绝/丢弃；
- `QueueManager.add_captured_task()` 不创建 worker、不进入队列；删除任务会一并删除
  合并文件与源轨；
- 主窗口轮询导入一次，记录删除后重复出现（模拟重启）不会产生重复行。

归档覆盖（`tests/test_capture_archive.py`）：

- 同卷 `os.replace`、已有同名文件时 `(1)`、大小不匹配保留源文件；
- 强制跨卷路径：分块复制、SHA-256 校验、临时 `.pdm-archive.part` 清理、取消保留源；
- `QueueManager` 成功更新 output/size、失败写 `archive_error` 并保留源、删除任务等待
  线程结束再清理、`archive_pending` 源文件仍在时重启续传、关闭设置不搬动；
- 主窗口在导入后自动归档，状态从“归档中”变为“已捕获”。

源轨清理覆盖（`tests/test_capture_cleanup.py`）：

- 自动清理仅在“合并成功 + 已归档 + 设置开启”时删除两条源轨，并更新 `extra_paths`/`mse_tracks`
  与 `sources_deleted_*`；关闭设置或未合并时保留源轨；
- 手动清理保留当前 output（合并文件或主轨），删除其他 `capture/mse` 源轨；
- 路径越界、其他任务 output、归档进行中都受到保护；
- 主窗口菜单动作启用、确认框与删除结果。

打包冒烟（2026-11-07）：真实 `browser/mse-fixture.mp4`（14,100 B）与 `mse-audio.mp4`
（18,030 B）经打包 Host 写入、ffmpeg 合并；临时 `APPDATA` 且 `mse_delete_sources=true`
启动打包 GUI 后，合并文件归档到 `downloads/视频/cleanup.merged.mp4`，两条源轨删除，
`extra_paths`/`mse_tracks` 清空（释放 32,130 B）。

### 公开站点矩阵（2026-11-08）

`bench/mse_site_matrix.mjs` 通过 CDP 依次打开独立 tab、等待页面 `complete`、激活 tab、
`mseStart`、触发播放、等待 `currentTime > 1`，再 `mseStop`，每个用例输出 JSON。用例：

- `dashif-akamai-bbb`：DASH-IF + Akamai BBB（MP4，h264 480×270 + aac）
- `dashif-shaka-angel-one`：DASH-IF + Shaka angel-one（MP4，h264 1920×1080 + aac）
- `dashif-unified-tears`：DASH-IF + Unified Tears of Steel（MP4，h264 480×270 + aac）
- `local-webm`：本地 VP9/Opus WebM 控制页（WebM）
- `hlsjs-shaka-angel-fmp4`：hls.js demo + Shaka fMP4 HLS（默认 blocked：jQuery 的 `ajax.googleapis.com` 在本网络 TCP 超时，`hls-demo.js` 报 `$ is not defined`；测试专用 jQuery shim 下已跑通：content 注入 2 轨 → 5,506,934 B；webRequest 重定向 → 2,730,802 B）

4/4 可执行用例通过；`PDM_MATRIX_ONLY=id1,id2` 可只跑指定用例。报告
[`mse-site-matrix-2026-11-08.md`](mse-site-matrix-2026-11-08.md)，证据
内部基准结果（未公开）。

### Firefox MAIN-world（2026-11-08）

用 WebDriver BiDi（`--remote-debugging-port`）临时安装探针扩展，在 Firefox 159.0.1
隔离 headless 中验证 `scripting.executeScript({world:"MAIN"})`：MAIN world 读到
`__pdmMseHooked`，isolated world 读不到。Firefox 的 content script 不能访问页面
principal 传来的 `ArrayBuffer`（`Permission denied to access property "constructor"`，
带不带 transfer 都一样），因此 `mse_capture.js` 在 MAIN world 先把 chunk 转 base64，
`mse_bridge.js` 收到字符串直接转发；Chromium 路径不变。本地 WebM 页与严格 CSP
（`script-src 'self'`）页均捕获 2 轨 / 4 chunks / 48,321 B（实际 48,314 B）、零错误。
`chrome.scripting` 命名空间在 Firefox 下同样是 Promise 语义。完整 background→Native Host→GUI
端到端也已通过：WebM 27,804 B + 20,510 B → 合并 47,971 B（VP9 320×180 + Opus）；
本地 fMP4 14,100 B + 18,030 B → 合并 31,589 B（h264 160×90 + aac）；DASH-IF/Akamai BBB
4,028,948 B + 167,793 B → 合并 4,189,997 B（h264 480×270 + aac）。打包 GUI 导入
“已捕获”任务并归档，SHA-256 与合并文件一致。popup 页面按钮已通过 iframe 加载真实
`popup.html` 并点击“开始/停止”验证（状态“多轨捕获中”→“已合并”，2 轨/47,971 B）；
工具栏 action popup 未点击。本地 hls.js 第二播放器（muxed 242,020 B，GUI 归档一致）
已通过，公网 hls.js demo 因 jQuery/googleapis 不可达仍 blocked；Shaka Player demo
（`*.appspot.com`）在本网络不可达；商业 DRM/直播未测；
Firefox 128+ 才支持 MAIN world（更低版本由 background 明确报错）。公网 demo 的阻塞原因
是页面从 `ajax.googleapis.com` 加载 jQuery，本机 TCP 443 超时，`hls-demo.js` 抛
`$ is not defined`，播放器未绑定视频源；`hls.js`/`hls-demo.js` 自身均 200。
测试专用 jQuery shim（测试副本 `content_scripts`/MAIN/document_start 注入 jQuery 2.1.3）
下 demo 正常初始化，捕获 2 轨 → 合并 5,506,934 B（h264 480×360 + aac，40 s）；
另用 `webRequestBlocking` 把 jQuery 302 到 cdnjs，同样通过 → 合并 2,730,802 B。
生产扩展未改动。
报告 [`firefox-mse-mainworld-evaluation-2026-11-08.md`](firefox-mse-mainworld-evaluation-2026-11-08.md)，
证据 内部基准结果（未公开）、
内部基准结果（未公开）、
内部基准结果（未公开）、
内部基准结果（未公开）、
内部基准结果（未公开）、
内部基准结果（未公开）、
内部基准结果（未公开）、
内部基准结果（未公开）、
内部基准结果（未公开）（blocked）、
内部基准结果（未公开）（jQuery/googleapis 超时诊断）、
内部基准结果（未公开）（content_scripts 注入）、
内部基准结果（未公开）（webRequest 重定向）、
内部基准结果（未公开）（popup UI iframe 点击）。

打包冒烟（2026-11-07）：打包 Host 写入视频/音频各 5,000 B 两轨，临时 `APPDATA` 且
`download_dir=<temp>/downloads` 启动打包 GUI；任务 output 变为 `downloads/视频/archive.mp4`，
源主轨移走，音频源轨留在 `capture/mse`，manifest 被消费。

打包端到端冒烟（2026-11-07）：用 `dist/pdm-host/pdm-host.exe --spool <临时 APPDATA>/PersonalDownloadManager/capture`
发送 `mse_start`/`mse_chunk`/`mse_end_all`（视频+音频两轨），再用临时 `APPDATA` 启动
`dist/pdm-gui/pdm-gui.exe`（offscreen）；`downloads.json` 中只出现 1 条 `completed`、
`options.capture=mse` 的任务，manifest 被消费，输出指向 `capture/mse` 内的轨道。

WebM 实机验证（2026-11-08）：新增 `browser/mse-webm-test.html`（`video/webm; codecs="vp9"` 与
`audio/webm; codecs="opus"` 两个 SourceBuffer、分块 appendBuffer）、
`browser/mse-webm-video.webm` / `mse-webm-audio.webm` 与 `bench/mse_webm_verify.mjs`。
Edge 156 隔离 headless 实际播放（readyState=4，currentTime 1.49 s），捕获视频 27,804 B +
音频 20,510 B；打包 Host 生成 `.webm` 并合并为 47,971 B `.merged.webm`，ffprobe 为
VP9 320×180 + Opus；打包 GUI 归档到 `downloads/视频` 并在清理开启时删除两条源轨。
报告 [`browser-verification-2026-11-08-mse-webm.md`](browser-verification-2026-11-08-mse-webm.md)。

真机复验（2026-11-07，Edge 156.0.4314.58 隔离 headless + `--load-extension`）：
DASH-IF/Akamai 实际捕获 2 轨并 ffmpeg 合并为 1,153,622 B（`mseStop` 0.519 s）；
打包 GUI 自动导入 1 条“已捕获”任务（源 URL、标题 “DASH-IF Reference Client”、
2 轨、合并文件正确），属性文本包含源/轨道/合并/附加文件，删除后合并文件、
`.m4a`、`.mp4` 全部清理。报告
[`browser-verification-2026-11-07-mse-queue.md`](browser-verification-2026-11-07-mse-queue.md)。
限制：Chrome for Testing 官方下载约 19 KB/s 已中止，因此使用本机 Edge；属性/删除通过
同一 `MainWindow`/`QueueManager` 代码在 offscreen Qt 中执行，不是鼠标点击。

本地真实合并验证（2026-11-03）：用 `browser/mse-fixture.mp4`（h264）与
`browser/mse-audio.mp4`（AAC）作为两条轨，`mse_end_all` + 真实 ffmpeg 输出
`demo.merged.mp4`（31,589 B）；`ffprobe` 确认 h264 视频 + aac 音频两条流。

手动验证（Chrome/Edge，需手动加载扩展）：

```bat
python browser\serve_demo.py --port 8000
:: 打开 http://127.0.0.1:8000/browser/mse-test.html
:: 弹窗“开始 MSE 捕获” -> 页面“播放多轨演示（视频+音频）” -> 弹窗“停止并保存”
```

真实站点（DASH-IF/Akamai）可用
`node bench/mse_real_site_capture.mjs <cdp> dashif.org 15 out.json`；脚本先开始捕获，
再点击 Load/Play，采样后发送 `mseStop`。

输出位置：`%APPDATA%\PersonalDownloadManager\capture\mse\`；默认在后台把合并文件
（无合并时为主轨）归档到下载目录/分类，源轨仍留在捕获目录。单轨 Chrome/Edge 手动验证
（14,100 B，SHA-256 与 fixture 一致）见
内部基准结果（未公开）。**多轨 Edge 156 实机验证通过**
（2026-11-04）：页面创建 2 个 SourceBuffer，捕获取到 `track-1` 视频 14,100 B +
`track-2` 音频 18,030 B，真实 ffmpeg 合并为 31,589 B 的 h264+aac MP4；报告
[`browser-verification-2026-11-04-mse-multitrack.md`](browser-verification-2026-11-04-mse-multitrack.md)，
证据 内部基准结果（未公开）。**DASH-IF/Akamai
真实站点也已通过**：视频 1,383,397 B + 音频 201,301 B，合并 1,578,521 B h264 480×270 +
aac；报告 [`browser-verification-2026-11-04-mse-real-site.md`](browser-verification-2026-11-04-mse-real-site.md)，
证据 内部基准结果（未公开）。另观察到页面中途
重置 MediaSource 时 `mseStop` 曾可能挂起；**2026-11-04 已修复并复验**：请求/捕获链/
停止 hook/`mse_end_all`/弹窗均有超时，受控重置后 `mseStop` **0.102s** 返回并完成合并；
报告 [`browser-verification-2026-11-04-mse-reset-stop.md`](browser-verification-2026-11-04-mse-reset-stop.md)，
证据 内部基准结果（未公开）。**Chrome 157
复验（2026-11-07）**：官方 Chrome for Testing 157.0.8103.38，headless 隔离配置；
DASH-IF/Akamai 正常捕获视频 2,782,126 B + 音频 167,793 B → 合并 2,944,027 B；
MediaSource 中途重置后视频 3,623,658 B + 音频 234,608 B → 合并 3,852,588 B，
`mseStop` **0.093s** 返回。报告
[`browser-verification-2026-11-07-mse-chrome.md`](browser-verification-2026-11-07-mse-chrome.md)；
branded Chrome 仍不支持 `--load-extension`，所以使用 Chrome for Testing。**Firefox
159 的 MAIN-world 注入已验证（2026-11-08）**：BiDi 探针 + 本地 WebM/严格 CSP 页均
捕获 2 轨 / 4 chunks / 48,321 B；Gecko 下 ArrayBuffer 需在 MAIN world 先转 base64；
完整 background→Native Host→GUI 端到端与本地 WebM/CSP 页已验证；本地 fMP4、
DASH-IF/Akamai BBB 与本地 hls.js 第二播放器也已通过；公网 hls.js demo 因
jQuery/googleapis 不可达 blocked；popup 页面按钮已验证（iframe 真实 popup.html）；
工具栏 action popup、商业 DRM/直播未测；Shaka Player demo（appspot）不可达（见下）。


## 23. 下载完成后关机

策略与命令在 `src/personal_download_manager/shutdown.py`，不依赖 Qt：

- `should_shut_down(statuses)`：列表非空、至少一个 `completed`，且所有状态都属于
  `completed/failed/cancelled`；`queued/downloading/paused/retrying/scheduled` 以及未知
  状态都阻止关机。暂停任务按“用户可能还要继续”处理，不会被关机丢弃。
- `shutdown_command(platform, delay)`：Windows 为 `["shutdown", "/s", "/t", "0"]`，
  其他平台为 `["shutdown", "-h", "now"]`；始终是参数列表，不拼接 shell 字符串。
- `run_shutdown(command)`：`subprocess.run(..., shell=False, check=True, timeout=30)`；
  Windows 额外带 `CREATE_NO_WINDOW`，避免 pythonw/托盘版本闪出控制台。

`QueueManager` 的测试（`tests/test_shutdown.py`）不启动真实关机：构造
`QueueManager(..., shutdown_runner=calls.append)` 注入记录器，并直接调用
`_tick_shutdown()` 推进倒计时。覆盖：

- 队列空闲且全部完成时倒计时开始、逐秒递减、到 0 只执行一次命令；
- `cancel_shutdown()` 停表后不再执行；
- 空任务列表、只有失败任务时不关机；
- 暂停/等待/下载中/计划/待重试状态阻止倒计时，删除暂停任务后重新起表；
- 倒计时中新增任务会停表（开关保持开启）；
- runner 抛 `OSError` 时发出 `shutdown_failed` 并自动 disarm；
- 真实 HTTP 下载完成后自动进入倒计时并触发注入的命令；
- 主窗口菜单开关、状态栏倒计时标签与非模态取消框。

设置侧：`AppSettings.shutdown_delay` 默认 60 秒，clamp 到 1–3600（设置对话框范围
5–3600），随 `config.json` 持久化；开关本身是 `QueueManager` 的会话状态，不落盘，
因此重启程序不会自动关机。
