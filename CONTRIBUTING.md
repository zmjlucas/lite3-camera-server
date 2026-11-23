# Contributing

欢迎提交可复现的问题和范围明确的 Pull Request。开发环境使用 macOS 或 Linux、Python 3.8+；本地演示和单元测试无需安装第三方 Python 包。

## 本地开发

1. 从 `main` 创建工作分支，例如 `feat/camera-status` 或 `fix/stream-timeout`。
2. 保持 `scripts/lite3_camera.py` 和 `scripts/lite3_camera_server.py` 可以直接执行，服务脚本可以单文件部署。
3. 修改行为时补充相应测试；文档和配置改动使用与改动相关的检查。
4. 提交前运行 `make check`。修改浏览器页面或 MJPEG 行为时再运行 `make browser-test`。
5. 在 PR 中说明改动、验证结果，以及是否真正连接过硬件。

`make check PYTHON=/path/to/python3` 可指定解释器。浏览器检查使用临时配置目录和随机回环端口，不使用现有浏览器资料。

启动工具保持 Python 3.8 语法兼容，独立服务保持 Python 3.6 语法兼容。`make check` 检查脚本和测试的 Python 3.8 语法；CI 另用 Python 3.8 运行 `make syntax-legacy` 检查服务端的 Python 3.6 语法。语法检查不等同于旧版本运行时验证。相机依赖保持延迟导入，使无相机的演示和测试仍可运行。

## 测试边界

CI 检查 Linux / macOS、Python 版本兼容性和真实 Chrome 播放行为，不执行实机测试。需要明确区分合成帧、模拟设备和真实相机结果。

`tests/test_lite3_camera_server.py` 中 4 项 `native-depth` 测试是待实现功能的规范，当前有明确的 `unittest.skip` 原因。不要把跳过计作通过；实现后应移除对应标记。

涉及机器人时只操作明确授权的相机设备，不为预览停止厂商服务，不发送运动指令。复用已发布的 ROS 图像优先于抢占设备。实机记录应包含环境、设备、命令、结果及尚未验证的部分；不提交密码、私钥或现场影像。

## 提交规范

提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)，例如：

```text
feat: add camera status indicator
fix: release video device on shutdown
docs: clarify V4L2 setup
ci: expand Python compatibility checks
```

`.superpowers/**` 是本地工作状态，必须保持忽略，不能提交或放入 PR；不要创建 `docs/superpowers/`。设计稿放在 `.superpowers/specs/`，实施计划放在 `.superpowers/plans/`。提交前检查 `git diff --cached --name-only`，不得使用 `git add -f` 绕过这些规则。
