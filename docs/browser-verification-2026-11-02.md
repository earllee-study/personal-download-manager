# Cookie 转发与 Firefox 接管验证（2026-11-02）

## 1. Cookie 转发（Chrome 156）

测试页脚本设置 `pdmcookie=verified`，右键
`http://127.0.0.1:8000/media/cookie-check.bin` → “用 PDM 下载此链接”。

该路由的行为：

- 不带 Cookie：HTTP 403
- 带 `Cookie: pdmcookie=verified`：HTTP 200，65,536 字节

PDM 任务结果：

| 项 | 值 |
|---|---|
| 状态 | `completed` |
| 大小 | 65,536 字节 |
| 任务 options.cookie | `pdmcookie=verified` |
| User-Agent | Chrome/156.0.0.0 |
| 输出 SHA-256 | `c2587d822e012b70a8761ecfe0454379f1828b63642ebff8de9a85a6786f1f71` |

任务能通过“无 Cookie 就 403”的路由完成，说明扩展读取目标 URL 的 Cookie 并转发给了 PDM。

## 2. Firefox 159 接管下载

在干净的 Firefox 配置 `pdm-test` 中临时载入 `browser/firefox/manifest.json`，
勾选“接管浏览器下载”，点击测试页的 `takeover-demo.bin`：

| 项 | 值 |
|---|---|
| 状态 | `completed` |
| 大小 | 131,072 字节 |
| User-Agent | Firefox/159.0 |
| 输出 SHA-256 | `9692c630a4ff23c0088c492f25814ad716d1b7b333eb3eeae97a6ce123c99a81` |
| 服务端同 URL SHA-256 | 同上 |
| 用户确认接管成功 | 是 |

Firefox 的临时扩展重启后会失效；需要重新在 `about:debugging` 载入。

## 3. 原始证据

```text
bench/results/browser-verification-2026-11-02-cookie-firefox-takeover.json
```
