# Contributing to PDM

感谢你愿意参与 Personal Download Manager (PDM)。

PDM 是一个用于测试与互操作性研究的开源下载管理器。欢迎 bug report、功能建议和 pull request。

## 范围与边界

- 只实现公开协议与互操作性功能。
- 不实现 DRM/EME 解密、SAMPLE-AES 绕过、第三方软件注册/授权绕过或私有服务协议。
- 不要提交包含他人隐私、Cookie、访问令牌、私钥或未授权内容的测试数据。
- 只下载你有权下载的内容。

## 开发环境

要求：

- Windows 10/11
- Python 3.11+
- Git

安装开发依赖：

```bat
python -m pip install -e ".[dev]"
```

运行测试：

```bat
python -m pytest -q
```

CI 等价运行方式：

```bat
python packaging/run_tests.py -q
```

GUI 测试使用 offscreen Qt；本地如需手动启动 GUI：

```bat
python -m personal_download_manager.gui
```

## 打包与发布

构建便携版：

```bat
packaging\build.cmd
```

构建安装包需要 Inno Setup 6：

```bat
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\pdm.iss
```

发布流水线见 `.github/workflows/release.yml` 与 `docs/ci.md`。

## 代码约定

- 使用类型标注；保持模块职责清晰。
- 新行为需要测试；修 bug 时优先补回归测试。
- GUI 线程不要做阻塞 I/O；耗时操作放到工作线程。
- 不要在没有讨论的情况下引入重量级运行时依赖。
- 保持输出可复现；性能结论需要可重复的本地基准。
- 不要提交 `dist/`、`build/`、日志、私钥、证书或本地配置。

## Pull Request

1. Fork 仓库并从 `main` 创建分支。
2. 完成修改并运行测试。
3. 提交 PR，说明：
   - 解决的问题
   - 主要改动
   - 测试方式
   - 是否有兼容性影响
4. 确保 CI 通过。
5. UI 改动建议附截图或简短说明。

## 安全问题

不要通过公开 issue 报告安全漏洞。请按 [`SECURITY.md`](SECURITY.md) 私下报告。

## 行为准则

参与本项目即表示同意遵守 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
