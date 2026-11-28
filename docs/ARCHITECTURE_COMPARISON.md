# Lite3 架构与高星 GitHub 项目比较

研究日期：2026-11-28。研究基线：`dd3bde12a75305e31fd53e03d1d5ce7126ed78ba`。

实施跟进：下文保留重构前的比较依据。契约校验、应用编排、设备模块拆分、launcher 入口、浏览器客户端与安装包检查已在工作区落实；当前用法和模块职责见 [README](../README.md#repository-layout) 与 [开发指南](DEV_SERVER.md#verification)。

后续决策：按用户要求移除向后兼容层，统一使用 `src/launcher.py` 与 `lite3_dev_server` 包；客户端严格要求当前协议头，状态响应统一为完整 `ControlStatus`。下文关于保留旧入口的建议已被这一决策替代。

**结论：server、SDK、API 契约保留在同一仓库，符合本项目的规模与部署约束。当前结构已经具备轻量 SDK、设备网关、独立控制看门狗和接口测试。最值得完善的是契约验证、HTTP 与设备编排的职责归属，以及安装包验收。** 可以继续采用一个服务进程，逐步改善内部模块。

以下是源码比较与改进建议，尚未形成批准实施的设计或实施计划。Star 用于筛选社区规模；架构判断依据是实际依赖、职责、协议与测试代码。

## 当前结构与约束

| 入口或模块 | 当前职责 | 评价 |
| --- | --- | --- |
| `src/client.py` | SSH/SCP 部署、诊断、隧道与启动浏览器 | 属于运维 CLI；可以明确 launcher/CLI 命名 |
| `src/dev_client.py` | 兼容旧代码的导入路径 | 转发到唯一 SDK 实现；保留兼容价值 |
| `src/lite3_dev_server/client.py` | 标准库 HTTP SDK、lease/sequence/session 管理 | 公开调用入口已经集中 |
| `src/lite3_dev_server/api.py` | 控制请求与响应的 TypedDict/Literal | 服务端在 TYPE_CHECKING 中引用，已共享静态契约 |
| `src/server.py` | HTTP、相机发现/采集、设备状态组合、生命周期装配 | 职责集中，适合沿现有接口逐步拆分 |
| `src/control.py` | 控制权、TTL、急停状态与独立 UDP writer | watchdog 与 UDP 发送已由独立线程调度 |
| `src/arm_gateway.py`、`src/arm_motion.py` | 串口访问、远程 helper、受限操作状态机 | 已有实际设备接口与可注入测试依赖 |
| `src/remote/`、`src/web/` | 设备端 helper 与 Web 客户端 | 部署内容与仓库维护脚本已经区分 |

依据：[CLI 与部署清单](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/client.py)、[兼容导入桥](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/dev_client.py)、[SDK](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/lite3_dev_server/client.py)、[类型契约](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/lite3_dev_server/api.py)、[服务端](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/server.py)、[控制模块](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/control.py)。

当前运行约束对目录选择有直接影响：SDK 要求 Python 3.8+ 且运行时仅用标准库；设备端代码保持 Python 3.6 语法兼容，直接拷贝脚本运行；安装包仅包含 `lite3_dev_server`。服务端通过 TYPE_CHECKING 引用类型，运行时无需加载 TypedDict。SDK 包的 `__init__.py` 会立即导入类型和客户端，所以后续整理目录时，需要继续保持设备端与 SDK 的运行时导入独立。服务面向可信局域网，匿名 HTTP 是项目已明确的访问策略，本比较沿用这一约束。[包配置](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/pyproject.toml)、[公开导入](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/lite3_dev_server/__init__.py)、[兼容性检查](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/scripts/check.py)、[项目说明](../README.md)。

## GitHub 项目比较

Star 数为本次 GitHub REST 查询值；源码链接固定到检查时的 commit。OctoPrint 使用当时的 `dev` 分支，其内容包含面向 2.0 的 connector 重构。

| 项目 | Star | 相似点 | 对 Lite3 最有价值的实践 |
| --- | ---: | --- | --- |
| [Ollama](https://github.com/ollama/ollama) | 188,367，约 188.4k | 服务端、CLI、Go 客户端与共享类型同仓 | `api` 集中类型和客户端；服务端消费相同请求类型，CLI 调用客户端 |
| [Chroma](https://github.com/chroma-core/chroma) | 29,800，约 29.8k | Python SDK、HTTP 服务、共享接口同仓 | 独立轻量客户端发行包；同一行为测试接口可覆盖 HTTP 实现 |
| [MLflow](https://github.com/mlflow/mlflow) | 28,999，约 29.0k | Python SDK、HTTP 服务与协议定义同仓 | 共享协议描述驱动客户端路由映射与服务端路由注册 |
| [OctoPrint](https://github.com/OctoPrint/OctoPrint) | 9,232，约 9.2k | Python 服务、浏览器控制、真实串口设备 | HTTP route → printer facade → serial connector；浏览器有专门 API client |
| [LeRobot](https://github.com/huggingface/lerobot) | 29,131，约 29.1k | 设备接口、远程客户端与机器人 host | 明确连接生命周期、能力描述和发送动作语义；设备 host 执行命令超时停止 |

Star 数据来源：[Ollama metadata](https://api.github.com/repos/ollama/ollama)、[Chroma metadata](https://api.github.com/repos/chroma-core/chroma)、[MLflow metadata](https://api.github.com/repos/mlflow/mlflow)、[OctoPrint metadata](https://api.github.com/repos/OctoPrint/OctoPrint)、[LeRobot metadata](https://api.github.com/repos/huggingface/lerobot)。

### Ollama：共享类型与职责清楚的客户端入口

`api/types.go` 定义请求类型；`api/client.go` 的 `Generate` 接收 `GenerateRequest` 并发送 HTTP 请求；服务端 `GenerateHandler` 使用 `api.GenerateRequest` 解析请求；CLI 通过 `api.ClientFromEnvironment()` 调用客户端。这个结构支持我们继续同仓维护 SDK、server 和契约。[请求类型](https://github.com/ollama/ollama/blob/d227b7e210b569e9e01c6b4b0c40748e73dcd01a/api/types.go#L65)、[客户端](https://github.com/ollama/ollama/blob/d227b7e210b569e9e01c6b4b0c40748e73dcd01a/api/client.go#L277)、[服务端](https://github.com/ollama/ollama/blob/d227b7e210b569e9e01c6b4b0c40748e73dcd01a/server/routes.go#L308)、[CLI](https://github.com/ollama/ollama/blob/d227b7e210b569e9e01c6b4b0c40748e73dcd01a/cmd/cmd.go#L1214)。

适用范围是依赖方向和接口复用。Ollama 使用 Go/Gin，其 `routes.go` 本身也很大；评估 Lite3 拆分价值时，应看同一文件承担了多少种独立变化。

### Chroma：同仓代码可以发布不同依赖范围的包

Chroma 的 `chromadb-client` 有单独的发行元数据和依赖清单，构建时复用仓库源代码。其客户端依赖仍包含 NumPy/Pydantic 等库；“轻量”描述的是相对完整 Chroma 的安装范围。Lite3 已经通过 setuptools 的包列表实现 SDK 的依赖隔离，可以继续保持这点。[客户端发行定义](https://github.com/chroma-core/chroma/blob/de23e66316caa9edbc571427c9d9e617a8abf458/clients/python/pyproject.toml#L1-L47)、[复用源码的构建脚本](https://github.com/chroma-core/chroma/blob/de23e66316caa9edbc571427c9d9e617a8abf458/clients/python/build_python_thin_client.sh#L35-L46)。

另一个可迁移做法是跨实现的行为契约测试：Client 和 HTTP 实现使用 ServerAPI 接口，集成脚本启动服务后通过同步/异步 HTTP 客户端运行 property suite。Lite3 已有真实 HTTP 模拟集成，可进一步覆盖安装后的 wheel、错误响应与新增接口。[Client 接口](https://github.com/chroma-core/chroma/blob/de23e66316caa9edbc571427c9d9e617a8abf458/chromadb/api/client.py#L45-L86)、[HTTP 实现](https://github.com/chroma-core/chroma/blob/de23e66316caa9edbc571427c9d9e617a8abf458/chromadb/api/fastapi.py#L73-L145)、[集成测试脚本](https://github.com/chroma-core/chroma/blob/de23e66316caa9edbc571427c9d9e617a8abf458/clients/python/integration-test.sh#L18-L36)。

### MLflow：让协议描述参与路由与序列化

MLflow 的 `service.proto` 定义请求/响应以及部分 HTTP method/path；RestStore 从 descriptor 建立 endpoint map，server 也读取同一 descriptor 注册路由。handler 使用共享请求与响应消息，并调用 store 完成工作。这为减少 client/server 路由漂移提供了直接例子。[协议定义](https://github.com/mlflow/mlflow/blob/30224156d73b8ca5ea06993622ae4a815855c9df/mlflow/protos/service.proto#L22-L70)、[客户端映射](https://github.com/mlflow/mlflow/blob/30224156d73b8ca5ea06993622ae4a815855c9df/mlflow/store/tracking/rest_store.py#L202-L219)、[服务端路由](https://github.com/mlflow/mlflow/blob/30224156d73b8ca5ea06993622ae4a815855c9df/mlflow/server/handlers.py#L8434-L8463)、[handler 调用 store](https://github.com/mlflow/mlflow/blob/30224156d73b8ca5ea06993622ae4a815855c9df/mlflow/server/handlers.py#L1925-L1949)。

对 Lite3 的近期启发是完善现有 TypedDict 的静态检查与线上格式测试。Protobuf/OpenAPI 生成可在跨语言消费者增加时再评估。当前 MLflow skinny 仍包含 FastAPI/uvicorn 等依赖，要求 Python 3.11+；直接复用其工程规模会增加我们的运行与维护负担。[skinny 依赖](https://github.com/mlflow/mlflow/blob/30224156d73b8ca5ea06993622ae4a815855c9df/libs/skinny/pyproject.toml#L26-L53)。

### OctoPrint：最接近我们的设备服务结构

`POST /printer/printhead` 解析、校验请求并调用 `printer.jog()`；printer facade 再调用 connection 的 `jog()`，serial connector 负责具体设备通信。浏览器的 `OctoPrintPrinterClient.jog()` 集中构造请求，UI 通过它调用接口。这些职责在代码中可以分别定位。[HTTP route](https://github.com/OctoPrint/OctoPrint/blob/fd4f94786385e48a55554a19c1e3054475fcd9c1/src/octoprint/server/api/printer.py#L302-L356)、[printer facade](https://github.com/OctoPrint/OctoPrint/blob/fd4f94786385e48a55554a19c1e3054475fcd9c1/src/octoprint/printer/standard.py#L646-L678)、[serial connector](https://github.com/OctoPrint/OctoPrint/blob/fd4f94786385e48a55554a19c1e3054475fcd9c1/src/octoprint/plugins/serial_connector/connector.py#L12-L47)、[浏览器 client](https://github.com/OctoPrint/OctoPrint/blob/fd4f94786385e48a55554a19c1e3054475fcd9c1/src/octoprint/static/js/app/client/printer.js#L157-L168)。

Lite3 已有 ControlService 与 arm gateway，最实际的改善是把 HTTP handler 中的 arm 状态编排移到应用层接口，并在浏览器继续增长时把协议请求从 UI 中提取出来。OctoPrint 的虚拟打印机接在 serial transport factory 上，也提供了设备替身的参考。[虚拟设备接口](https://github.com/OctoPrint/OctoPrint/blob/fd4f94786385e48a55554a19c1e3054475fcd9c1/src/octoprint/plugins/virtual_printer/__init__.py#L413-L470)。

检查版本要求 Python 3.11+ 和 Flask，包含更大的插件体系。本比较只采用职责分工方面的证据。[运行依赖](https://github.com/OctoPrint/OctoPrint/blob/fd4f94786385e48a55554a19c1e3054475fcd9c1/pyproject.toml#L49-L70)。

### LeRobot：明确设备生命周期和命令含义

Robot 接口规定 connect/disconnect、离线可查询的 observation/action features、观测读取和动作发送。`send_action()` 返回实际发送的动作，包括限幅后的结果，有助于区分请求动作、已发送动作和设备观测。LeKiwiClient 通过 ZeroMQ 实现远程访问；host 根据命令超时调用 `stop_base()`。[设备接口](https://github.com/huggingface/lerobot/blob/712d75a8ae62b1ddb9219992ad10c118241eb13a/src/lerobot/robots/robot.py#L98-L151)、[动作返回契约](https://github.com/huggingface/lerobot/blob/712d75a8ae62b1ddb9219992ad10c118241eb13a/src/lerobot/robots/robot.py#L212-L240)、[远程 client](https://github.com/huggingface/lerobot/blob/712d75a8ae62b1ddb9219992ad10c118241eb13a/src/lerobot/robots/lekiwi/lekiwi_client.py#L36-L70)、[host watchdog](https://github.com/huggingface/lerobot/blob/712d75a8ae62b1ddb9219992ad10c118241eb13a/src/lerobot/robots/lekiwi/lekiwi_host.py#L62-L101)。

Lite3 的独立 writer/watchdog、TTL 和 observed/simulated/stop_verified 字段值得保留。LeRobot 的这个 watchdog 位于 host 主循环；参考其设备端责任划分时，也应保持 Lite3 现有的独立调度。LeRobot 当前要求 Python 3.12+ 并包含机器学习依赖，适合作为接口语义的比较对象。[运行依赖](https://github.com/huggingface/lerobot/blob/712d75a8ae62b1ddb9219992ad10c118241eb13a/pyproject.toml#L29-L63)。

## 与我们的实践逐项比较

| 维度 | Lite3 已有实践 | 判断与建议 |
| --- | --- | --- |
| 仓库组织 | server、SDK、Web、remote helper 同仓 | 继续保持；同仓便于同步协议和部署变更 |
| SDK 依赖 | 独立 Python 包、stdlib、py.typed | 已较清楚；增加实际 wheel 安装验收 |
| API 契约 | 共享 TypedDict，部分 HTTP 响应形状断言 | 补静态检查、关键字段响应校验、错误契约及版本兼容测试 |
| HTTP 与应用逻辑 | 大多数控制通过 ControlService；arm read/reconnect 在 handler 编排 | 集中设备状态组合规则，HTTP 层负责协议转换 |
| 相机模块 | LatestFrame/CaptureController 已形成接口，具体实现仍集中于 server.py | 沿现有接口提取发现、采集和生命周期代码 |
| 硬件时序 | 独立 watchdog、lease/TTL、序列失效；SDK 动作不自动重试 | 保持这些语义，模块拆分时用已有测试约束 |
| Web 客户端 | 已有统一 api() helper，payload 和会话状态仍在 HTML 中 | 页面继续增长时拆出小型浏览器 client |
| 发布与版本 | SDK wheel 与远程脚本分别交付；已有协议 header | 明确版本兼容策略并增加安装态测试 |

### 优先级一：把已声明的契约真正验收起来

SDK `_request()` 检查响应是 JSON object，`status()` 随后用 `cast()` 标注为 StatusResponse。纯内存假响应 `{}` 会被 `status()` 原样返回；这是本次直接复现的行为。潜在后果是服务与 SDK 漂移时，调用者在访问字段时才发现不匹配。[SDK 响应处理](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/lite3_dev_server/client.py)。

当前已经有字典形状测试和真实 loopback 集成；这些测试提供了良好基础。CI 运行 AST 语法检查与 unittest，尚未执行实际的 mypy/pyright；`sdk_typecheck.py` fixture 主要供未来静态检查使用。包测试检查源码导入与类型注解，尚未覆盖隔离环境安装 wheel。[形状测试](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/tests/test_dev_server.py)、[集成测试](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/tests/test_dev_integration.py)、[类型 fixture](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/tests/fixtures/sdk_typecheck.py)、[包测试](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/tests/test_sdk_package.py)、[CI](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/.github/workflows/ci.yml)。

建议先加一款静态检查器，并验证构建后的 wheel 在仓库外可以导入和调用模拟服务；对 mode、lease、关键状态区分字段补标准库校验，同时允许兼容的新增响应字段。完整 schema/codegen 的收益可以随外部消费者增加再评估。

### 优先级二：收拢设备编排，沿现有接口拆开 server

`server.py` 有 947 行，包含 Linux V4L2/sysfs/proc 发现、RealSense/V4L2 采集、HTTP、参数和服务装配。拆分理由在于它承载了多个独立变化来源：换相机驱动、修改 HTTP 契约、调整服务启动都需要修改同一个模块。[服务端源码](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/server.py)。

更具体的职责交叉在 `arm/state`、`arm/reconnect`：HTTP handler 直接调用 gateway，串口 I/O 结束后再读取 control 状态，组合 motion_enabled 与 estop。这段应用规则适合集中到一个设备服务入口；迁移时保留串口 I/O 不占用 control mutex 的约束。`ControlError` 携带 HTTP status 的耦合可以随后处理。[arm 编排](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/server.py)、[ControlService](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/control.py)、[并发急停断言](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/tests/test_dev_server.py)。

近期可以继续保留一个服务进程，把相机实现、HTTP 传输、设备应用逻辑和启动装配分成可单独定位的模块。现有 LatestFrame、CaptureController 与 gateway 已经提供了拆分位置。

### 优先级三：明确公共入口、交付与演进策略

三个 client 文件分别对应部署 CLI、兼容桥和 SDK。用一张入口表明确职责，后续可以将 CLI 的实现迁到 launcher 命名下并保留旧脚本入口。整理目录时同步更新 SERVER_BUNDLE，并继续运行“仅部署清单中的文件也能启动”的测试。[部署清单](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/client.py)、[部署集成](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/tests/test_dev_integration.py)。

服务端已经发送 `X-Lite3-Dev-Protocol: 1`，SDK 当前没有消费该 header。可先定义新增字段、未知能力、必需字段缺失和不兼容协议版本的处理约定，再增加相应测试。`api.py` 当前专注控制字典，camera endpoints、错误 envelope 和路由仍由其他代码及文档描述；是否扩展 SDK 到相机功能，需要按实际调用需求决定。[协议响应头](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/server.py)、[SDK](https://github.com/zmjlucas/lite3-dev-server/blob/dd3bde12a75305e31fd53e03d1d5ce7126ed78ba/src/lite3_dev_server/client.py)、[HTTP 契约说明](DEV_SERVER.md)。

## 验证范围

本次核对了本地源码、打包配置、CI 和上述固定版本的 GitHub 源码。执行的定向测试共 36 项，通过：

```bash
python3 -B -m unittest discover -s tests -p 'test_dev_client.py' -q
python3 -B -m unittest discover -s tests -p 'test_dev_server.py' -q
python3 -B -m unittest discover -s tests -p 'test_sdk_package.py' -q
python3 -B -m unittest discover -s tests -p 'test_dev_integration.py' -q
```

对应数量为 21、11、3、1。loopback 集成测试需要本地监听端口，获准在沙箱外运行后通过。全部使用模拟或测试替身；没有连接机器人、摄像头或串口。这里的结果限于所列测试；Python 3.6 运行、实体动作与停止效果仍需各自的验收。

研究阶段仅新增本记录。后续实现按上述建议改进代码，保留此记录作为重构前的比较基线。
