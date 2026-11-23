# 在 Mac 浏览器查看 Lite3 相机

Mac 用 SSH 连接感知主机，通过本地浏览器播放相机画面。Mac 这端只需要 Python 3.8 或更新版本、OpenSSH 和浏览器。相机预览的命令不会发送运动指令。

以下命令在仓库根目录运行。实机命令以厂商默认的感知主机 `ysc@192.168.1.103` 为例，按机器人的实际账户和 IP 替换。

## 现在没有机器人

```bash
python3 scripts/lite3_camera.py demo
```

浏览器会打开 `http://127.0.0.1:18080/`，显示持续变化的合成画面。加 `--no-open` 可以手动打开。终端按 Ctrl+C 结束。

演示画面固定为 160×120；它用于检查播放链路，实际相机分辨率由所选设备和参数决定。RealSense、V4L2 和演示页面直接检查视频数据是否到达；连接结束或约 2.5 秒未收到可解码画面时，会隐藏旧画面并重连。

## 连上机器人后

先让 Mac 接入机器狗 Wi-Fi 或指定的有线网络，再检查感知主机可用的相机接口。首次 SSH 连接先核对主机密钥。

```bash
python3 scripts/lite3_camera.py doctor ysc@192.168.1.103
```

`doctor` 读取系统、Python 依赖、ROS 环境、相机服务状态和视频设备信息。三个远程子命令 `doctor`、`deploy` 和 `view` 都支持 `--dry-run`，只打印将执行的命令。

### 通过机器人 Wi-Fi 跳转连接

若 Mac 接入机器狗的 `192.168.2.*` Wi-Fi，但感知主机在内部 `192.168.1.*` 网段，可通过已确认能访问感知主机的机器人 SSH 主机跳转。例如，Mac 的 `192.168.1.*` 路由被 iPhone USB 网络占用时：

```bash
python3 scripts/lite3_camera.py doctor ysc@192.168.1.103 --jump ysc@192.168.2.1
python3 scripts/lite3_camera.py view ysc@192.168.1.103 --jump ysc@192.168.2.1
# 仅在下方 RealSense 或 V4L2 方式需要部署服务时运行：
python3 scripts/lite3_camera.py deploy ysc@192.168.1.103 --jump ysc@192.168.2.1
```

`--jump` 接受单个 SSH 别名、主机名或 `user@IPv4`，对 SSH 和 SCP 都使用 OpenSSH `-J`。跳转主机只转发 SSH 连接，相机命令仍在感知主机执行，不启动运动控制器。两台主机均需正常身份验证及主机密钥核对；脚本不保存密码，也不修改路由或 SSH 配置。不加 `--jump` 时仍直接连接目标。

### 已有 ROS 相机话题

优先复用正在发布的 RGB 图像话题。感知主机需要运行相机发布节点，且目标 ROS 环境中需要有 `web_video_server`。`view` 启动预览服务并转发端口，浏览器首页列出可用图像话题。

```bash
python3 scripts/lite3_camera.py view ysc@192.168.1.103
```

有多个 ROS 环境时，用 `--ros-setup` 指定与相机节点相同的环境，例如 `--ros-setup /opt/ros/melodic/setup.bash`。也可指定包含所需包的工作空间 `setup.bash`。脚本根据环境中的 `ROS_VERSION` 选择 ROS 1 或 ROS 2。找到实际话题后，用 `--topic /实际话题名` 直接打开该路画面。

ROS 1 预览使用独立节点名，避免挤掉其他预览。ROS 2 请求 `sensor_data` 订阅配置，以兼容相机常用的 best-effort 发布方式；这要求 `web_video_server` 支持 `default_qos_profile`。旧构建若不支持该参数且没有画面，需核对其订阅 QoS 与相机发布 QoS，不要仅凭话题存在判断相机预览成功。

ROS 模式使用上游的预览页面；发布端停帧时，该页面可能保留最后一帧。需结合 `rostopic hz`／`ros2 topic hz` 判断是否仍在更新。本仓库的自动断流提示和隐藏旧画面适用于下方 RealSense、V4L2 模式及本地演示。

如果缺少 `web_video_server`，先在感知主机加载匹配的 ROS 环境，再用 `apt-cache policy "ros-${ROS_DISTRO}-web-video-server"` 检查配置的软件源是否有对应版本。确认有候选版本后，可在感知主机安装 `sudo apt install "ros-${ROS_DISTRO}-web-video-server"`。对 Melodic、Foxy 等旧环境，找不到候选包时应使用与厂商系统匹配的构建，或选下方的 RealSense 方式。新版上游源码的依赖可能超出旧镜像的支持范围。

### 直接读取 RealSense

当相机空闲时，可以从感知主机直接读取 RGB。该方式需要感知主机的 Python 3 能导入 `pyrealsense2`、`numpy` 和 `cv2`。厂商手册说明镜像预装原生 librealsense，但 Python 绑定需以 `doctor` 实测为准。

```bash
python3 scripts/lite3_camera.py deploy ysc@192.168.1.103
python3 scripts/lite3_camera.py view ysc@192.168.1.103 --backend realsense
```

`deploy` 将服务脚本上传到感知主机的 `~/robo-comp-camera/`。默认使用 `640×480`、`15 fps` 和 JPEG 质量 `70`。可用 `--width`、`--height`、`--fps`、`--quality` 调整，连接了多台相机时用 `--serial` 选定设备。加 `--depth` 可在 RGB 旁边查看伪彩深度预览，画面颜色仅用于观察，不能当作距离读数。

### 通过 V4L2 读取 RGB

感知主机缺少 `pyrealsense2` 或 `web_video_server`，但已有支持 V4L2 的 OpenCV 时，可以读取相机的 RGB 视频节点。先在感知主机核对设备及支持的格式，不要猜测 `/dev/video0` 就是 RGB。以下以已确认的 `/dev/video4` 为例：

```bash
v4l2-ctl --list-devices
v4l2-ctl --device /dev/video4 --list-formats-ext
fuser /dev/video4
```

确认它是 RGB 节点、支持所选分辨率和帧率，且没有 ROS 相机节点、厂商查看器或其他程序占用后，再从 Mac 部署并预览。若相机正在发布 ROS 图像，优先使用现有话题，不要为预览停止厂商服务。

```bash
python3 scripts/lite3_camera.py deploy ysc@192.168.1.103 --jump ysc@192.168.2.1
python3 scripts/lite3_camera.py view ysc@192.168.1.103 --jump ysc@192.168.2.1 --backend v4l2 --device /dev/video4
```

可直接连接感知主机时省略 `--jump`。`--device` 必须显式给出 `/dev/videoN` 或 `/dev/v4l/by-id/名称`；设备编号可能随系统重启或 USB 连接变化。该模式只提供 RGB，不支持 `--depth` 或 `--serial`。可用 `--width`、`--height`、`--fps` 和 `--quality` 请求设备支持的设置，实际画面及帧率仍需验证。

本次机器人连接的 RGB 实测、帧率、浏览器显示及退出释放结果见 [2026-11-20 实机测试记录](CAMERA_HARDWARE_TEST_2026-11-20.md)。

## 退出和排查

`view` 默认通过 SSH 将 Mac 的 `127.0.0.1:18080` 转发至感知主机的 `127.0.0.1:8080`。可加 `--no-open` 后手动打开终端打印的地址。在运行 `view` 的终端按 Ctrl+C，结束这次预览服务和隧道。

| 现象 | 检查 |
| --- | --- |
| SSH 连不上 | 确认 Mac 所连网络及感知主机 IP，确认 SSH 端口可达。 |
| 端口被占用 | 本机改用 `--local-port 18081`。感知主机改用 `--remote-port 8081`。 |
| ROS 页面没有图像或画面冻结 | 确认 `--ros-setup` 与相机节点所用环境一致。在该环境中用 `rostopic list`／`rostopic hz` 或 `ros2 topic list`／`ros2 topic hz` 检查实际图像话题是否持续有帧。 |
| RealSense 报设备忙 | 检查是否有 ROS 相机节点或 `realsense-viewer` 正在读取。若 ROS 正在发布图像，优先选 ROS 方式。 |
| RealSense 缺 Python 绑定 | 用 `doctor` 确认 Python 版本及缺少的模块。所用绑定须与感知主机的架构、Python 和厂商 SDK 匹配。 |
| V4L2 没有图像 | 核对 `--device` 是 RGB 节点、未被其他程序占用，且 OpenCV 支持 V4L2；按设备支持的格式选择分辨率和帧率。 |
| V4L2 参数与实际画面不符，或读取终止 | OpenCV 的参数设置是请求，设备可能协商其他值；核对实际分辨率和帧率。终止性读取错误需先排除占用或连接问题，再重新运行 `view`。 |
| 本地页面出现 502 | 检查浏览器代理是否拦截 `127.0.0.1`。启动器的就绪检查已绕过代理；可用 `curl --noproxy '*' http://127.0.0.1:18080/healthz` 排查，或仅在临时 Chrome 验证会话使用 `--no-proxy-server`，不修改系统代理。 |
| 画面延迟或断续 | RealSense 或 V4L2 模式可试 `--quality 55`，也可选相机支持的较低分辨率或帧率，或使用厂商指定的有线网络复测。RealSense、V4L2 及演示服务的 `/healthz` 可检查最近是否收到帧。 |

离线演示只验证 Mac 上的浏览器和视频服务。实机 RGB 结果见上方测试记录；深度预览和端到端延迟仍需单独验证。

## 离线验证记录（2026-11-20）

在当前 Mac 上通过了 32 项标准库测试，以及独立的 Chrome 浏览器检查：JPEG 解码显示、正常流结束和传输停顿时隐藏旧图并重连。测试覆盖跳转连接、V4L2 参数及本地代理不影响就绪检查。Python 3.6 服务端／3.8 启动工具通过语法检查，离线测试实际使用本机 Python 3.13；机器人上的实际版本与结果见实机测试记录。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
# 可选：用本机 Chrome 的临时配置测试浏览器；不接触已有浏览器资料或机器人。
PYTHONDONTWRITEBYTECODE=1 python3 tests/camera_browser_smoke.py
```

浏览器检查需要 Chrome，或用 `--browser /浏览器可执行文件绝对路径` 指定 Chromium 系列浏览器。测试使用随机本机端口，可与演示同时运行。

厂商资料见 [网络配置](https://developer.deeprobotics.cn/docs/lite3/guide/software/network/)、[相机接口](https://developer.deeprobotics.cn/docs/lite3/interfaces/device/camera/) 和 [相机开发示例](https://developer.deeprobotics.cn/docs/lite3/examples/software/camera-example/)。ROS 的预览与快照参数见 [web_video_server](https://github.com/RobotWebTools/web_video_server)。
