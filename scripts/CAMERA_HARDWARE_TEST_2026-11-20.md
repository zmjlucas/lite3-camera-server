# Lite3 相机实机检查：2026-11-20

本次只检查相机、网络与视频显示。未执行运动脚本，未发送运动、起立、步态或机械臂指令；没有启动、停止或重启原厂控制服务，没有升级系统或安装依赖。

## 实际连接与环境

- Mac 的机器人 Wi-Fi 地址为 `192.168.2.47`，同时存在 iPhone USB 网络；直接访问 `192.168.1.*` 会走 USB 默认网关。
- 通过 SSH 网关 `ysc@192.168.2.1` 到感知主机 `ysc@192.168.1.103`。本次没有修改 Mac 路由。
- 感知主机：Jetson Xavier NX / aarch64、Ubuntu 20.04.6、Python 3.8.10。
- 已有 numpy 1.23.1、OpenCV 4.9.0、FFmpeg、v4l2-ctl 和原生 RealSense SDK。
- Python 中没有 `pyrealsense2`；ROS Foxy 和 Noetic 中都没有 `web_video_server`。Foxy 查询到点云话题，未查询到可用 RGB 图像话题；Noetic ROS master 不可达。
- 原厂 `realsense` / `realsense_ros2` 服务均为 inactive。开始采集前，`fuser` 未发现视频设备占用。
- RealSense 工具识别 D435i、固件 `05.13.00.50`。`v4l2-ctl` 确认 `/dev/video4` 为 RGB 节点，支持 `YUYV 640×480 @ 15 fps`；此节点编号只对本次设备检查成立。

## 实测结果

1. 使用现有 OpenCV 从已核实的 RGB 节点采集 30 帧，图像为 `640×480×3`，共 2.552 秒（包括启动开销），驱动报告 15 fps；随后释放设备。
2. 仅部署 `~/robo-comp-camera/lite3_camera_server.py`，使用 `--source v4l2 --device /dev/video4 --width 640 --height 480 --fps 15 --quality 70`。HTTP 服务只监听感知主机回环地址 `127.0.0.1:8080`，SSH 转发到 Mac 的 `127.0.0.1:18080`。
3. 三次健康检查均为 `source=v4l2, status=live`，本次帧龄采样为 0.027 / 0.049 / 0.001 秒。帧龄是服务端采集新鲜度，**不是端到端视频延迟**。
4. Mac 连续读取 8.055 秒，收到 122 帧、122 个不同的 JPEG 内容，接收速率约 15.15 fps，平均 JPEG 大小 31,532 字节。FFprobe 解码确认图像为 `640×480`。
5. 本机 Chrome 实际显示了相机现场画面，状态为 `Live`；2 秒内 10 次采样均指向不同的已渲染视频帧。另在用户 Chrome 中打开了预览网址。
6. 退出本次预览后，`fuser /dev/video4` 确认为无占用，连接远端 8080 端口返回 connection refused，两项原厂相机服务仍为 inactive。相机释放与预览关闭路径通过实测。
7. 为实际网络和依赖补充了 `--jump`、V4L2 后端及本地 HTTP 检查绕过代理。最终 32 项离线回归测试通过；新代理检查路径也已在实际 Mac 上成功读取实时相机健康状态。

本次运行没有调用 NoMachine。实际使用的是 V4L2 RGB；**深度预览、断开实体 USB 后恢复、Wi-Fi 长时间稳定性和端到端延迟未验收**。

## 再次运行

在仓库根目录运行，按提示进行 SSH 身份验证：

```bash
python3 scripts/lite3_camera.py view ysc@192.168.1.103 \
  --jump ysc@192.168.2.1 \
  --backend v4l2 --device /dev/video4
```

本次已完成部署；换设备或修改服务端代码后，先运行：

```bash
python3 scripts/lite3_camera.py deploy ysc@192.168.1.103 --jump ysc@192.168.2.1
```

若本次测试的预览仍占用 18080 端口，先结束它，再运行上述命令。正常用启动工具运行时，在其终端按 Ctrl+C 即结束预览。

本次助手为浏览器保留了临时 SSH 预览连接。关闭浏览器标签页只停止播放；要结束本次相机服务和隧道，可在 Mac 终端运行以下命令（仅适用于本次临时连接）：

```bash
ssh -S /private/tmp/lite3-camera-perception-20261120.sock -O exit ysc@192.168.1.103
```

发现 Mac 系统网页代理会让 Python 默认 HTTP 客户端访问回环地址返回 502；启动工具已改为对本机就绪检查绕过代理，不更改系统代理设置。
