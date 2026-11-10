# Security Policy

## Supported versions

当前支持最新公开版本（0.1.x）。旧版本不保证安全更新。

## Reporting a vulnerability

请**不要**通过公开 issue 报告安全问题。

优先使用 GitHub Security Advisories：

```text
https://github.com/earllee-study/personal-download-manager/security/advisories/new
```

报告中请包含：

- 受影响版本
- 操作系统与安装方式
- 复现步骤或最小样例
- 影响说明
- 如果可能，提供修复建议

请避免在报告里包含真实 Cookie、令牌、私钥或他人隐私数据。

## Scope

重点关注：

- 下载引擎的 Range/分段/续传/校验逻辑
- HTTP/TLS、代理、认证、Cookie 处理
- FTP/FTPS/SFTP 连接与主机密钥校验
- 浏览器 Native Messaging Host
- 更新清单与安装包校验
- GUI 的本地文件路径处理

不在范围内：

- DRM/EME 解密或绕过
- 第三方站点自身的安全问题
- 未签名构建的 SmartScreen 警告（这是已知发布状态）
- 用户主动下载的不可信内容

## Response

这是个人维护的开源项目，安全报告会尽力及时处理，但不提供漏洞赏金。

## Verifying releases

每个 Release 提供 `SHA256SUMS.txt` 和 `latest.json`。下载后请校验：

```powershell
Get-FileHash .\pdm-setup-0.1.0.exe -Algorithm SHA256
```

Windows 构建当前未签名；请以 GitHub Release 的校验和为准。
