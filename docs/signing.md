# Windows 代码签名（SignPath OSS）

本项目选择 **SignPath OSS** 作为公开发布的主签名路线：

- 公开 GitHub 仓库可申请免费 OSS 代码签名
- 不需要自己持有 PFX 证书或硬件令牌
- 签名在 SignPath 完成；GitHub Actions 只上传未签名 artifact、下载已签名 artifact
- 前提是所有 job 都在 GitHub-hosted runner 上执行（当前 `release.yml` 满足）

`release.yml` 同时保留了 PFX 兜底路径：如果没有配置 SignPath，但配置了
`WINDOWS_CERT_PFX_BASE64`，则使用 `packaging/sign_windows.ps1`；两者都没有时，
发布产物未签名，并输出 warning。

## 1. SignPath 侧准备

> 正确的 OSS 入口是 **SignPath Foundation**：
>
> - 申请：<https://signpath.org/apply>
> - Foundation 首页：<https://signpath.org/>
> - 产品页：<https://signpath.io/solutions/open-source-community>
> - GitHub 集成文档：<https://docs.signpath.io/trusted-build-systems/github>
>
> `https://signpath.io/solutions/open-source-software` 是旧链接，会返回 404。

1. 在 SignPath Foundation 申请 OSS 项目，并关联 GitHub 仓库
   `earllee-study/personal-download-manager`。
2. 安装 **SignPath GitHub App**，授权该仓库。
3. 在 SignPath 创建：
   - 项目（Project），例如 `pdm`
   - 签名策略（Signing Policy），例如 `release-signing`；dry-run 可另建
     `test-signing`
   - **一个** Artifact Configuration，例如 `pdm-executables`：
     - 根节点必须是 `<zip-file>`
     - 一个 `<pe-file path="*.exe" max-matches="unbounded" />` 即可同时处理
       便携版（3 个 exe）和安装包（1 个 exe）
     - workflow 上传的 zip 根目录就是 exe 文件（便携版会把
       `pdm-gui.exe`、`pdm.exe`、`pdm-host.exe` 平铺）
   - 建议先在 SignPath 的 Artifact Configuration 页面**上传样本 zip**，
     让 SignPath 自动分析结构；样本可以在本地生成：

```powershell
Compress-Archive -Path dist\pdm-gui\pdm-gui.exe, dist\pdm-cli\pdm.exe, dist\pdm-host\pdm-host.exe `
  -DestinationPath pdm-portable-sample.zip -Force
Compress-Archive -Path dist\pdm-setup-0.1.0.exe `
  -DestinationPath pdm-installer-sample.zip -Force
```

推荐配置：

```xml
<artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
  <zip-file>
    <pe-file path="*.exe" max-matches="unbounded" />
  </zip-file>
</artifact-configuration>
```

如果 SignPath 在样本中显示了文件夹前缀（例如
`unsigned-portable/pdm-gui.exe`），把 path 改为
`unsigned-portable/*.exe` 或 `**/*.exe`；当前 workflow 的
`upload-artifact` 路径会把文件放在 zip 根目录。

4. 创建 API token（需要该项目的 submitter 权限）。

## 2. GitHub 仓库配置

进入 **Settings → Secrets and variables → Actions**。

### Secret

| 名称 | 内容 |
|---|---|
| `SIGNPATH_API_TOKEN` | SignPath API token |

### Variables

| 名称 | 内容 |
|---|---|
| `SIGNPATH_ORGANIZATION_ID` | SignPath organization ID |
| `SIGNPATH_PROJECT_SLUG` | 项目 slug，例如 `pdm` |
| `SIGNPATH_SIGNING_POLICY_SLUG` | 签名策略 slug，例如 `release-signing` |
| `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG` | 可选；留空使用项目默认配置 |

### PFX 兜底（可选）

只有不使用 SignPath、改用自有证书时才需要：

| 名称 | 类型 |
|---|---|
| `WINDOWS_CERT_PFX_BASE64` | Secret |
| `WINDOWS_CERT_PASSWORD` | Secret |

## 3. Workflow 行为

`release.yml` 的签名流程：

1. **Detect signing method**
   - SignPath 的 token + org/project/policy 都存在 → `signpath`
   - 否则有 PFX → `pfx`
   - 否则 → `none`
2. **便携版**
   - 把三个 exe 复制到 `unsigned-portable/`
   - 上传为 GitHub artifact `pdm-portable-unsigned`
   - 提交 SignPath 签名请求
   - 下载签名结果到 `signed-portable/`
   - 覆盖 `dist/pdm-gui/pdm-gui.exe`、`dist/pdm-cli/pdm.exe`、
     `dist/pdm-host/pdm-host.exe`
   - 用 `Get-AuthenticodeSignature` 校验 `Status == Valid`
3. **安装包**
   - 用已签名的便携版 exe 构建安装包
   - 上传 `pdm-setup-<version>.exe`
   - 提交 SignPath 签名请求
   - 下载并覆盖安装包
   - 再次校验签名
4. **后续**
   - 安装包 docs 冒烟检查
   - zip、`latest.json`、`SHA256SUMS.txt`
   - dry-run 时跳过 GitHub Release 发布

## 4. 如何验证

### dry-run

```bash
git tag v0.1.0-dryrun
git push origin v0.1.0-dryrun
```

或手动运行 `Release` workflow，`version=0.1.0`、`dry_run=true`。

检查以下步骤：

- `Submit portable signing request (SignPath)`：success
- `Restore signed portable executables`：success
- `Submit installer signing request (SignPath)`：success
- `Restore signed installer`：success
- `Publish GitHub Release`：skipped

验证完成后删除临时 tag：

```bash
git push origin :refs/tags/v0.1.0-dryrun
git tag -d v0.1.0-dryrun
```

### 手动验证签名

```powershell
signtool verify /pa dist\pdm-gui\pdm-gui.exe
signtool verify /pa dist\pdm-cli\pdm.exe
signtool verify /pa dist\pdm-host\pdm-host.exe
signtool verify /pa dist\pdm-setup-0.1.0.exe
```

没有 `signtool` 时也可用：

```powershell
Get-AuthenticodeSignature dist\pdm-gui\pdm-gui.exe | Format-List
```

## 5. 注意

- SignPath OSS 策略要求所有 job 都在 GitHub-hosted runner 上运行，当前 workflow
  满足。
- 每次签名请求都必须先通过 `actions/upload-artifact` 上传 artifact；workflow
  已分别上传便携版和安装包。
- 如果 SignPath 变量缺失，签名步骤会跳过，发布产物未签名（会有 warning）。
- 如果使用 SignPath 的 **test-signing** 测试策略，签名证书可能不受 Windows
  信任，`Get-AuthenticodeSignature` 可能返回 `NotTrusted`；当前 workflow 要求
  `Valid`。正式 dry-run 请使用 `release-signing` 生产策略。
- 签名不等于 SmartScreen 立即无警告；信誉需要随签名使用逐步积累。
- API token 只授予所需项目/策略的 submitter 权限，并定期轮换。
- 安装包内只包含公开 docs；签名不会改变这一点，`release.yml` 仍会做 docs
  冒烟检查。
