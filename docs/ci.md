# CI 与发布流水线

仓库内的 workflow 位于 `.github/workflows/`。CI 已在 GitHub Actions 实跑，
Python 3.13 / 3.14 矩阵均通过。

## 1. CI（`.github/workflows/ci.yml`）

触发：任意分支 push、pull request、手动 `workflow_dispatch`。

- 运行环境：`windows-latest`，Python `3.13` / `3.14` 矩阵（`fail-fast: false`）。
- 步骤：安装 `.[dev]` → `packaging/check_version.py` → `compileall` →
  PowerShell 签名脚本语法校验 → `pytest -q --junitxml=pytest.xml`。
- 环境：`PYTHONUTF8=1`、`QT_QPA_PLATFORM=offscreen`（GUI 测试无需桌面）。
- 失败时仍上传 `pytest.xml`（artifact 名 `pytest-<python>`）。
- 测试覆盖本地回环 HTTP/TLS/h2/h3、FTP/FTPS/SFTP、GUI offscreen；不依赖外部网络服务或商业软件。

## 2. Release（`.github/workflows/release.yml`）

触发：

- push tag `vX.Y.Z`；或
- 手动 `workflow_dispatch` 并提供 `version`（不带 `v`）。

流程：

1. 解析版本并写入 `VERSION` / `TAG`；`packaging/check_version.py --expect` 要求
   `pyproject.toml`、`src/personal_download_manager/__init__.py`、`packaging/pdm.iss` 三处一致。
2. 安装依赖（含 PyInstaller），**先跑全量 `pytest`**，再执行 `packaging\build.cmd`：
   - `dist\pdm-gui\pdm-gui.exe`
   - `dist\pdm-cli\pdm.exe`
   - `dist\pdm-host\pdm-host.exe`
3. **签名（便携版）**：优先使用 **SignPath OSS**：上传未签名 exe 为 GitHub
   artifact，提交 SignPath 签名请求，下载并覆盖 GUI/CLI/Host，再用
   `Get-AuthenticodeSignature` 校验。未配置 SignPath 但存在
   `WINDOWS_CERT_PFX_BASE64` 时回退到 `packaging/sign_windows.ps1` + PFX。
   签名必须在打安装包/zip 之前完成，否则包内仍是未签名文件。详见
   [`signing.md`](signing.md)。
4. 安装 Inno Setup，执行 `ISCC.exe /DAppVersion=<version> packaging\pdm.iss`，
   生成 `dist\pdm-setup-<version>.exe`（内含已签名的便携版程序）。
5. **签名（安装包）**：对 `pdm-setup-<version>.exe` 再执行一次 SignPath 签名
   （未配置 SignPath 时回退 PFX）并校验。
6. 将三个 onedir 目录压缩为 `*-<version>-win64.zip`（此时内部程序已签名）。
7. 生成 `dist\latest.json`（`make_update_manifest.py`，URL 指向本 tag 的 GitHub Release
   资产）与 `dist\SHA256SUMS.txt`。
8. 上传 artifact，并用 `gh release create`/`gh release upload --clobber` 发布到
   GitHub Release（`contents: write`）。

产物：

```
pdm-gui-<version>-win64.zip
pdm-cli-<version>-win64.zip
pdm-host-<version>-win64.zip
pdm-setup-<version>.exe
latest.json
SHA256SUMS.txt
```

## 3. 签名配置

主路线是 **SignPath OSS**（完整步骤见 [`signing.md`](signing.md)）。仓库需要：

| 类型 | 名称 |
|---|---|
| Secret | `SIGNPATH_API_TOKEN` |
| Variable | `SIGNPATH_ORGANIZATION_ID` |
| Variable | `SIGNPATH_PROJECT_SLUG` |
| Variable | `SIGNPATH_SIGNING_POLICY_SLUG` |
| Variable（可选） | `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG` |

PFX 兜底（可选）：

| Secret | 内容 |
|---|---|
| `WINDOWS_CERT_PFX_BASE64` | `.pfx` 的 Base64（单行，无换行） |
| `WINDOWS_CERT_PASSWORD` | PFX 口令 |

生成 Base64（PowerShell）：

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\secure\personal_download_manager.pfx"))
```

注意：

- SignPath 和 PFX 都没配置时**不会**伪造签名；release 明确 warning，SmartScreen
  仍会提示。
- SignPath OSS 要求所有 job 在 GitHub-hosted runner 上运行；当前 workflow 满足。
- PFX 只在 runner 临时目录存在，签名后 `finally` 删除；口令只作为进程参数/env，
  不写入文件、不打印。
- `sign_windows.ps1` 从 Windows SDK 查找 `signtool.exe`；找不到会失败而不是跳过。
- 证书过期/时间戳服务不可达会让 release 失败，这是有意的 fail-closed 行为。
- SignPath 的 Artifact Configuration 与 GitHub App 配置见 [`signing.md`](signing.md)。

## 4. 本地复现发布

```bat
:: 1) 版本一致
python packaging\check_version.py

:: 2) 测试
python -m pytest -q

:: 3) 便携版
packaging\build.cmd

:: 4) 安装器（版本由命令行传入）
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" /DAppVersion=0.1.0 packaging\pdm.iss
:: Git Bash 下必须先 export MSYS_NO_PATHCONV=1，否则 /DAppVersion=… 会被改写成路径

:: 5) 更新清单（URL 换成实际 HTTPS 目录）
python packaging\make_update_manifest.py ^
  --installer dist\pdm-setup-0.1.0.exe ^
  --version 0.1.0 ^
  --base-url https://downloads.example.com/personal-download-manager/ ^
  --notes "local build" ^
  --out dist\latest.json

:: 6) 可选：本地签名
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\sign_windows.ps1 ^
  -PfxPath C:\secure\personal_download_manager.pfx -Password "<password>" ^
  -Files dist\pdm-setup-0.1.0.exe,dist\pdm-gui\pdm-gui.exe
```

## 5. 本地发布结果（2026-11-02）

在 `D:\studyfile\claude_code\PDM` 上按上述步骤实际执行（未推送远端）：

| 步骤 | 结果 |
|---|---|
| `python -m pytest -q` | `327 passed, 1 skipped in 107.86s`（跳过 Node 语法检查） |
| `packaging\build.cmd` | exit 0；CLI/GUI/Host 于 22:35–22:37 生成 |
| 打包 CLI | `pdm.exe --version` → `pdm 0.1.0`；`update --help` 正常 |
| 打包 CLI 下载 | 本地 HTTP 4 MiB、4 分段、5 个 Range 请求，SHA-256 一致 |
| 打包 GUI | `QT_QPA_PLATFORM=offscreen` 启动 6 秒保持运行，随后停止 |
| 打包 Host | `ping` → 0.1.0；MSE 会话 65,536 B，SHA-256 一致 |
| Inno `/DAppVersion=0.1.0` | 成功（59.031 s），`dist\pdm-setup-0.1.0.exe` = 92,105,378 B，SHA-256 `5d226b694798104f5524d60d4195d81c896b2b609dc8f21007bc9449ad9a4359` |
| `make_update_manifest.py` | 生成 `dist\latest.json`（size 92,105,378，哈希与安装包一致） |
| zip + checksums | `pdm-gui-0.1.0-win64.zip` 68,177,737 B、`pdm-cli-…` 36,160,893 B、`pdm-host-…` 16,858,109 B；`SHA256SUMS.txt` 四个产物逐一复算一致 |
| 安装包循环 | 静默安装（含 browserintegration）→ Host 哈希匹配、清单路径正确、内置 CLI 可运行 → 静默卸载后目录与 Chrome/Edge 注册项清空 |
| Native Host | 已按 chrome/edge/firefox 重新注册，安装副本哈希与 `dist\pdm-host` 一致 |
| 签名 | **未执行**：本机无 PFX、无 signtool；仅通过 PowerShell Parser 语法校验 |

> 首次 `build.cmd` 因上一轮遗留的 `dist\pdm-gui\pdm-gui.exe`（PID 185304）锁定
> `_rust.pyd` 而失败；终止该 PDM 进程后重跑成功。未修改系统级下载设置。

`latest.json` 中的 URL 在本地构建时是示例 HTTPS 地址；真正发布时 workflow 会替换为
`https://github.com/<repo>/releases/download/<tag>/`。

### 5.1 2026-11-04 Host/安装包重建（多轨 + 停止健壮性）

- 仅重建 Host 与安装包，GUI/CLI zip 保持 2026-11-02 构建。
- `pdm-host/pdm-host.exe` 4,719,971 B，SHA-256 `5f31ea02e16ca3cf91be91181f1806c5546ae6e97bfa57503706d10e9bb36f59`；
  `pdm-host-0.1.0-win64.zip` 16,865,608 B，SHA-256 `9bd3e8d2a2ed8494dcec68ef27944aa0f518c9a9c85b41054e7c85a6eefd844b`；
  `pdm-setup-0.1.0.exe` 92,123,163 B，SHA-256 `db8dc033ba4911e73c0d0992905e6a4e26261e011b078a72f08ead9319cdf0bb`；
  `latest.json`/`SHA256SUMS.txt` 已同步。
- 打包 Host 多轨+ffmpeg 合并冒烟（14,100 B 视频 + 18,030 B 音频 → 31,589 B h264+aac）、
  静默安装→Host 哈希匹配→静默卸载均通过。
- 停止健壮性：请求/捕获链/停止 hook/`mse_end_all`/弹窗均有超时；受控 MediaSource
  重置后 `mseStop` 0.102s 返回并合并（1,607,331 B h264 640×360 + aac）。

## 6. 注意

- 版本号采用三处静态同步（`pyproject.toml` / `__init__.py` / Inno 默认值）；CI 在
  发布前强制检查。CLI `--version` 直接读取 `personal_download_manager.__version__`，不再单独维护。
- `windows-latest` 的 Python 3.14 与依赖（PySide6、aioquic、paramiko、pyftpdlib）
  需要 setup-python 能提供对应版本；如果某版本暂不可用，CI 会失败而不是静默降级。
- 首次发布前需要在 GitHub 仓库启用 Actions，并按需配置签名 secrets。
- 更新清单 `latest.json` 默认上传到 Release 资产；也可改用独立静态 HTTPS 站点，
  只需把清单 URL 告知用户或写入 GUI 设置。
- `.github/workflows` 与本地脚本都不修改系统证书或全局代理。
