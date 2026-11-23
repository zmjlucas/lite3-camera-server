# Lite3 Camera Server

[![CI](https://github.com/zmjlucas/lite3-camera-server/actions/workflows/ci.yml/badge.svg)](https://github.com/zmjlucas/lite3-camera-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

通过 SSH 在 Mac 浏览器中查看 Lite3 机器狗的相机画面。支持复用 ROS 图像话题、直接读取 RealSense、V4L2 RGB，以及无需机器人的本地演示。

本项目只负责相机预览，不发送运动指令。客户端无需安装 ROS；本地演示和自动化测试只使用 Python 标准库。

## 快速开始

需要 Python 3.8+。连接机器人还需要 OpenSSH（`ssh` / `scp`）和能访问感知主机的网络。日常开发推荐使用受维护的 Python 版本；3.8 兼容性用于旧设备环境。

```bash
git clone https://github.com/zmjlucas/lite3-camera-server.git
cd lite3-camera-server
python3 scripts/lite3_camera.py demo
```

浏览器打开 `http://127.0.0.1:18080/`，播放 160×120 的合成画面。在终端按 Ctrl+C 退出。无桌面环境时加 `--no-open`。

这是直接运行脚本的工具仓库，无需 `pip install`。完整使用方法见 [相机预览指南](scripts/CAMERA_STREAM.md)。

## 连接机器人

以下为示例地址，替换为实际感知主机；首次连接需核对 SSH 主机密钥。

```bash
# 只读检查主机、依赖、相机服务和 ROS 环境
python3 scripts/lite3_camera.py doctor ysc@192.168.1.103

# 默认复用 ROS 图像话题，需要相匹配的 web_video_server
python3 scripts/lite3_camera.py view ysc@192.168.1.103
```

| 模式 | 感知主机要求 | 用法 |
| --- | --- | --- |
| ROS（默认） | 已发布图像话题、匹配的 ROS 环境及 `web_video_server` | `view HOST` |
| RealSense | 空闲相机、兼容的 `pyrealsense2`、`numpy`、`cv2` | `deploy HOST` 后执行 `view HOST --backend realsense` |
| V4L2 RGB | Linux、支持 V4L2 的 OpenCV、已核实且空闲的 RGB 设备节点 | `deploy HOST` 后执行 `view HOST --backend v4l2 --device /dev/videoN` |
| 演示 | 本机 Python 标准库 | `demo` |

相机依赖须与设备架构、Python 和厂商 SDK 匹配，因此不通过统一的依赖文件自动安装。服务端保持 Python 3.6 语法兼容；当前完整测试套件的运行要求为 Python 3.8+，不声称已验证 Python 3.6 运行时。

需要跳转主机时，三个远程子命令都可加 `--jump ysc@192.168.2.1`；加 `--dry-run` 可先查看将执行的命令。`deploy` 只上传服务脚本到 `~/robo-comp-camera/`。

HTTP 服务和 SSH 转发均绑定 `127.0.0.1`，默认本机端口为 `18080`、感知主机端口为 `8080`。本项目的 HTTP 服务没有独立认证，应通过 SSH 隧道使用。

## 服务接口

以下接口适用于演示、RealSense 和 V4L2 服务；ROS 模式使用上游 `web_video_server` 的接口。

| 路径 | 内容 |
| --- | --- |
| `/` | 浏览器预览，断流时隐藏旧帧并重连 |
| `/stream.mjpg` | MJPEG 视频流 |
| `/snapshot.jpg` | 最近的有效 JPEG 帧 |
| `/healthz` | JSON 状态及服务端帧龄；无有效帧时返回 HTTP 503 |

`frame_age_s` 表示服务端采集新鲜度，不代表端到端延迟。

## 开发与验证

```bash
make check                 # 语法兼容性检查 + 标准库测试
make browser-test          # 可选：本机 Chrome 的真实浏览器检查
# Linux 或其他 Chromium 路径：
make browser-test BROWSER=/usr/bin/google-chrome
```

不使用 Make 时：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 tests/camera_browser_smoke.py
```

CI 在 Linux 的多个 Python 版本和 macOS 上运行测试，并单独运行 Chrome 检查；不连接机器人。贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 当前范围与已知限制

- [2026-11-20 实机记录](scripts/CAMERA_HARDWARE_TEST_2026-11-20.md)记录了单台 D435i / Jetson Xavier NX 的 V4L2 RGB 验证，不能推广为所有设备或后端均已验收。
- RealSense 的 `--depth` 提供伪彩深度预览，不提供距离测量。深度预览、实体 USB 断连恢复、长时间 Wi-Fi 稳定性和端到端延迟仍待实机验收。
- 独立原生深度助手模式 `native-depth` **尚未实现**。现有 4 项对应测试保留并明确跳过；它们不计入已通过的功能验证。实现该功能时须移除跳过标记并通过全部测试。
- ROS 页面断帧后可能保留最后一帧，需结合 ROS 话题频率判断是否仍在更新。

## 仓库结构

```text
scripts/       SSH 启动工具、独立相机服务及操作/实机记录
tests/         标准库测试与可选 Chrome 检查
.github/       CI、依赖更新和协作模板
```

MIT 许可证，见 [LICENSE](LICENSE)。
