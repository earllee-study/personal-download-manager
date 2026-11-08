# 自动更新（用户主动检查）

PDM 的更新是**用户主动触发**的：

- 程序启动时**不会**联网检查；
- 不会静默下载或静默安装；
- 只有点击“帮助 → 检查更新…”或运行 `pdm update` 才会读取清单；
- 安装包下载后必须通过 `SHA-256` 校验，运行前还会再次确认；
- CLI 只有显式 `--launch` 才会启动安装器；GUI 会弹出“现在运行安装程序吗？”。

## 1. 清单格式

```json
{
  "version": "0.2.0",
  "url": "https://downloads.example.com/personal-download-manager/pdm-setup-0.2.0.exe",
  "sha256": "…64 位十六进制…",
  "size": 71303168,
  "notes": "修复……",
  "page_url": "https://downloads.example.com/personal-download-manager/",
  "published_at": "2026-11-01"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 是 | 数字点分版本（`0.2.0`、`v1.2`、`1.2.0.1`）；只有比当前 `__version__` 新才会提示 |
| `url` | 是 | 安装器地址，必须以 `.exe` 结尾；HTTPS（回环地址允许 HTTP） |
| `sha256` | 是 | 安装包的 SHA-256，64 位十六进制 |
| `size` | 否 | 精确字节数；提供时会校验，防止截断/多写 |
| `notes` | 否 | 更新说明，最多 64 KiB |
| `page_url` | 否 | 发布页，HTTPS（回环允许 HTTP） |
| `published_at` | 否 | 展示用日期字符串 |

规则：

- 清单和安装包默认必须使用 **HTTPS**；只有 `localhost` / `127.0.0.1` / `::1` 允许 HTTP，
  便于本地测试。重定向后的**最终** URL 也要满足同一规则。
- URL 不允许包含用户名/密码。
- 清单最大 1 MiB，安装包最大 2 GiB。
- 不支持预发布通道：`1.2.0-beta` 与 `1.2.0` 视为同一数字版本。

## 2. CLI

```bat
:: 只检查
pdm update --manifest https://downloads.example.com/personal-download-manager/latest.json

:: 检查并下载到指定目录（校验后打印路径）
pdm update --manifest https://…/latest.json --download D:\updates

:: 下载、校验并运行安装程序（显式 opt-in）
pdm update --manifest https://…/latest.json --launch

:: 机器可读
pdm update --manifest https://…/latest.json --json
```

- 没有新版本时退出码 0，输出“已是最新版本”。
- 清单/下载/校验错误输出到 stderr，退出码 1。
- `--launch` 未指定 `--download` 时使用 `%LOCALAPPDATA%\PersonalDownloadManager\updates`。
- `--json` 输出：

```json
{
  "current_version": "0.1.0",
  "update": {"version": "0.2.0", "url": "…", "sha256": "…", "size": 123, "notes": "…", "page_url": "…", "published_at": "…"},
  "downloaded": "C:\\Users\\…\\updates\\pdm-setup-0.2.0.exe",
  "launched": false
}
```

## 3. GUI

1. 设置 → “更新清单” 填入发布方的 HTTPS JSON 地址。
2. 帮助 → “检查更新…”：后台检查，不阻塞界面。
3. 发现新版本时显示版本、大小、说明；“下载并安装”开始下载，显示进度，可取消。
4. 下载完成后校验 SHA-256；随后询问是否运行安装程序。选择“否”会保留文件并显示路径。
5. 未配置 URL 时只提示配置，不发起任何网络请求。

安装包在 `%LOCALAPPDATA%\PersonalDownloadManager\updates`；更新器不会删除旧安装包。

## 4. 发布流程

1. 更新 `pyproject.toml` 与 `src/personal_download_manager/__init__.py` 的版本号，然后运行
   `packaging\build.cmd` 构建便携版，并用 Inno Setup 生成
   `dist\pdm-setup-X.Y.Z.exe`。
2. 生成并校验清单：

   ```bat
   python packaging\make_update_manifest.py ^
     --installer dist\pdm-setup-0.2.0.exe ^
     --version 0.2.0 ^
     --base-url https://downloads.example.com/personal-download-manager/ ^
     --notes-file dist\release-notes.md ^
     --out dist\latest.json
   ```

   脚本会计算大小/SHA-256，并调用客户端同一个 `parse_manifest()` 验证；失败则退出码 2。
3. 把安装器与 `latest.json` 上传到同一 HTTPS 目录；把清单 URL 告知用户或写入设置。
4. 建议在发布页同时公布 SHA-256，便于离线核对。

## 5. 安全边界

- `SHA-256` 只能证明**传输完整性**：清单托管方如果被攻破，可以发布自己的文件与哈希。
  真正的发布者身份需要**代码签名**（P7 后续项）与受信任的 HTTPS 托管。
- 未签名的安装器仍会触发 Windows SmartScreen；更新器不会绕过它。
- 更新器不自动提权；Inno Setup 安装包按用户安装（无需管理员）。
- 不执行 `.exe` 以外的文件，不传静默安装参数；安装器以自己的正常 UI 运行。
- 下载先写 `.part`，校验通过后才原子改名为最终文件；失败/取消会清理 `.part`。
- 不读取 `HTTP_PROXY`/`HTTPS_PROXY`（`trust_env=False`），需要代理时应由系统/发布方提供
  可达的 HTTPS 地址。

## 6. 本地测试

测试与手工验证可使用回环 HTTP：

```bat
python -m http.server 8000 --directory D:\update-test
:: latest.json 中 url 写 http://127.0.0.1:8000/pdm-setup-0.2.0.exe
pdm update --manifest http://127.0.0.1:8000/latest.json --download D:\update-test\out
```

注意：`parse_manifest()` 只对回环地址放行 HTTP；任何公网 HTTP 清单都会被拒绝。
