# 打包

两条产线：

| 产物 | 是什么 | 目录 |
|---|---|---|
| `dist\pdm-gui\pdm-gui.exe` | 便携版 GUI（onedir，双击即用，无安装、无卸载） | 解压即用 |
| `dist\pdm-cli\pdm.exe` | 便携版 CLI | 解压即用 |
| `dist\pdm-host\pdm-host.exe` | 浏览器 Native Messaging 宿主（无界面） | 解压即用 |
| `dist\pdm-setup-0.1.0.exe` | 安装包（开始菜单/桌面快捷方式、卸载项） | 由 Inno Setup 生成 |

## 1. 构建便携版

```bat
cd D:\studyfile\claude_code\PDM
python -m pip install pyinstaller
packaging\build.cmd
```

- `packaging\make_icon.py` 用 GUI 自己的绘制代码生成 `icon.ico`；
- 三个 spec 都是 **onedir**（启动快、杀软误报少），不是 onefile；
- 产物：`dist\pdm-gui`（约 159 MB）、`dist\pdm-cli`（约 77 MB）、`dist\pdm-host`（约 37 MB）；
- 打包后已验证：CLI 能完成一次本地多段下载，也能对公网做 HTTPS 探测；GUI exe 能启动并保持运行。

## 2. 构建安装包

需要 [Inno Setup 6](https://jrsoftware.org/isinfo.php)（本机已通过 winget 安装到用户目录）：

```powershell
winget install JRSoftware.InnoSetup
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\pdm.iss
```

> 在 Git Bash 里运行 Setup.exe 时，先 `export MSYS_NO_PATHCONV=1`，否则 `/VERYSILENT` 会被改写成 Windows 路径。

版本号现在由 `check_version.py` 强制与 `pyproject.toml` / `__init__.py` 一致；CI 用
`ISCC.exe /DAppVersion=<version> packaging\pdm.iss` 覆盖脚本里的默认值，本地手工
构建也可以这样传。

产物 `dist\pdm-setup-0.1.0.exe`：

- **按用户安装**（`PrivilegesRequired=lowest`）：装到 `%LOCALAPPDATA%\Programs\Personal Download Manager`，不弹 UAC、不需要管理员；
- 开始菜单：Personal Download Manager、Personal Download Manager 命令行、卸载；
- 可选任务“注册浏览器集成”：安装宿主、扩展目录并写入 Chrome/Edge 的 HKCU 注册表键（卸载时清理）；
- 可选桌面快捷方式；
- 控制面板“应用和功能”里可卸载；
- 脚本在 `packaging\pdm.iss`，改版本号改 `#define AppVersion`。

安装包生成后，可一次生成三个便携版 zip、`SHA256SUMS.txt` 与 `latest.json`：

```bat
python packaging\make_release_assets.py --version 0.1.0 ^
  --base-url https://downloads.example.com/personal-download-manager/ ^
  --notes "PDM rebuild"
```

安装冒烟可静默安装到临时目录（`/TASKS=` 不选浏览器集成，避免覆盖现有 Host 注册），
运行 CLI/GUI 后再静默卸载。

## 3. 更新清单

构建好安装包后，用同一个校验器生成清单：

```bat
python packaging\make_update_manifest.py ^
  --installer dist\pdm-setup-0.2.0.exe ^
  --version 0.2.0 ^
  --base-url https://downloads.example.com/personal-download-manager/ ^
  --notes-file dist\release-notes.md ^
  --out dist\latest.json
```

- 脚本计算 `size`/`sha256`，并调用 `personal_download_manager.update.parse_manifest()` 验证；失败退出码 2。
- 安装器与清单上传到同一 HTTPS 目录；`packaging/update-manifest.example.json` 只是格式示例。
- 客户端要求 HTTPS（`localhost`/`127.0.0.1` 的 HTTP 仅用于本地测试），不会自动检查或静默安装。
- 完整清单字段、GUI/CLI 用法与安全边界见 [`../docs/updating.md`](../docs/updating.md)。

## 4. 注意

- 未签名：首次运行会触发 Windows SmartScreen（“更多信息 → 仍要运行”）。要消除需代码签名证书 + `signtool`。
- 升级：客户端有**用户主动**的更新检查（`pdm update` / GUI“帮助 → 检查更新…”），但安装包本身不包含常驻更新服务，也不会静默安装。发布时用 `make_update_manifest.py` 生成清单并托管到 HTTPS。
- 安装向导是英文（Inno Setup 不自带简体中文 `.isl`；任务描述仍是中文）。
- 安装包只打包 `pdm.iss` 中**显式列出**的公开 docs。不要改回 `docs\*`：开发目录里可能有被 `.gitignore` 排除的交接、开发状态、内部分析和 SSH 配置文档，通配符会把它们带进公开安装包。
- 本机已验证：静默安装（含 `browserintegration`）→ 清单为合法 JSON、注册表指向正确、宿主 `ping` 正常 → 静默卸载后目录与注册表键清空。
- 静默安装任务参数不要加引号：`/TASKS=browserintegration`。
- `dist\`、`build\` 是构建产物，不入库。
- 打包 GUI/CLI/Host 三部分；`bench\`、`tests\` 依赖项目源码，不随安装包分发。
- 便携版 zip **不含** `browser\extension`；浏览器集成需从源码树加载扩展，或使用 Setup 安装包里的 `browser-extension`（`pdm.iss` 的 `browserintegration` 任务）。
- 需要 HTTP/3 时，先在打包环境里 `pip install aioquic`（或 `pip install -e ".[full]"`）；GUI/CLI 两个 spec 已把 `aioquic` 加进 `hiddenimports`（Host 不需要）。未安装时 PyInstaller 只会给出 missing-hiddenimport 警告，产物仍可用，只是 `--http3` 会提示缺少依赖。
- SFTP 需要 `pip install paramiko`（或 `.[sftp]` / `.[full]`），GUI/CLI 两个 spec 已把 `paramiko` 加进 `hiddenimports`；FTP/FTPS 只用标准库。`pyftpdlib` 只用于测试/基准，不进安装包。
- GUI 与 CLI 都包含 HTTP(S)、HLS/DASH、FTP/FTPS/SFTP 与浏览器捕获入队；Host 只负责 Native Messaging，不打包 Qt/httpx。
- DASH 双轨合并使用**外部 ffmpeg**，不随安装包分发；请在设置里填 ffmpeg 路径、使用 `--ffmpeg`，或把 ffmpeg 放进 PATH。没有 ffmpeg 时双轨任务会保留视频/音频两个文件。
- MSE 多轨合并同样使用外部 ffmpeg（`PDM_DOWNLOADER_FFMPEG`、GUI `ffmpeg_path` 或 PATH）；未配置时保留 `.mp4`/`.m4a`（或 `.webm`）各轨，并返回 `merge_error`。

## 5. CI 与签名

- `.github/workflows/ci.yml`：Windows + Python 3.13/3.14，版本一致性、编译、PowerShell
  语法校验、全量 pytest。
- `.github/workflows/release.yml`：tag `vX.Y.Z` 或手动 `workflow_dispatch`（支持
  `dry_run`）触发构建、SignPath OSS 签名（PFX 兜底）、Inno Setup、zip、
  SHA256SUMS、`latest.json`、GitHub Release。
- `packaging/check_version.py`：三处版本一致性检查；发布 tag 必须匹配。
- SignPath 配置：`SIGNPATH_API_TOKEN` secret + `SIGNPATH_*` variables；未配置时
  回退 `WINDOWS_CERT_PFX_BASE64` / `WINDOWS_CERT_PASSWORD`；两者都没有时产物
  未签名并 warning。`packaging/sign_windows.ps1` 是 PFX 兜底。
- 完整步骤见 [`../docs/signing.md`](../docs/signing.md) 与
  [`../docs/ci.md`](../docs/ci.md)。
