# MISERVER

MISERVER 监测四台 DGX Spark / GB10 节点的温度，通过一台外部小米空气净化器调节散热风力，并提供 HTTPS 控制页面和温度历史。

生产 Docker 入口使用固定规则温控，机器学习及学习参数应用均关闭。控制器区分目标请求、设备实际档位和执行确认，按温度余量、负载、持续趋势及故障边界调节风力。现行策略与发布约束见 [当前交付工具](ops/README.md)。

## 适用硬件和网络

当前实现固定绑定以下现场配置，不是通用即装即用服务。

- 节点为 `192.168.3.180`、`192.168.3.181`、`192.168.3.183`、`192.168.3.184`，复用各节点已有的 `http://<IP>:9100/status`，不安装新的节点 Agent。
- 控制页面为 `https://192.168.3.180:8910/`。写操作只接受真实连接来自 `192.168.3.0/24` 或回环地址，并检查 Host、Origin、HTTPS 和请求字段。可信局域网内用户可以修改设置和提交配对，没有独立管理口令。
- 唯一可绑定设备为 `192.168.3.24` 的 `zhimi.airpurifier.m2`，DID 为 `70632677`，MAC 为 `78:11:dc:47:87:bf`。本地档位为 0 到 16，0 档仍保持运转。同型号设备不能直接沿用这台设备的标定。
- Docker 配置使用 Linux host 网络、UID/GID `1000:1000`，以及 `/home/bestvcboy/` 下的固定状态、准入和诊断目录。部署前需要独立核验镜像、目录身份、持久预算和设备读回，不能直接启动第二个控制进程。

本服务控制外部净化器，不修改 DGX 内置风扇固件，不自动关机或降频。正 RPM 只证明电机旋转，不能证明实际风量或散热能力。CPU 显示值是 ACPI 保护代理，非 GPU 硬件最高温是跨器件最大值，都不是独立校准探头。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| [core.py](core.py) | 固定规则控制、传感器判断和日志存储。 |
| [service.py](service.py) | 节点轮询、设备执行、电机预算、HTTPS API 和应用生命周期。 |
| [qr_connector.py](qr_connector.py) | 米家扫码连接与固定设备绑定。 |
| [ui.html](ui.html)、[ui.js](ui.js)、[ui.css](ui.css) | 控制页面与温度历史界面。 |
| [learning.py](learning.py) | 保留的学习实现，生产入口停用。 |
| [ops/docker](ops/docker) | Docker 配置、只读预检和监督进程。 |
| [check.py](check.py)、[release_index.json](release_index.json) | 唯一完整离线检查入口与候选源码清单。 |
| `test_*.py`、`ops/` 下的测试 | 控制、存储、界面和交付回归。 |

## 准备离线开发环境

安装 Python 3.12 和 Node.js 22。Node 用于离线界面检查，不能缺省跳过。

Windows PowerShell 执行以下命令。

```powershell
py -3.12 -m venv .venv-check
.\.venv-check\Scripts\python.exe -m pip install -r requirements.txt
git config core.hooksPath .githooks
.\.venv-check\Scripts\python.exe -B check.py
```

Linux 执行以下命令。

```sh
python3.12 -m venv .venv-check
.venv-check/bin/python -m pip install -r requirements.txt
git config core.hooksPath .githooks
.venv-check/bin/python -B check.py
```

完整检查在独立进程中执行六套测试，报告写入新建的 `.audit/check-<id>`。测试使用替身和临时状态，不登录真实账号或访问真实风机。Windows 通过仍标记原生验证待完成，普通 Linux 检查也不能代替 ARM64 持久化检查或实机散热验收。

修改清单内文件后，先审查差异，再用同一 Python 执行 `-B check.py --refresh-inventory`，最后重新执行完整检查。提交钩子核对暂存区和工作区字节，并运行完整检查。清单刷新和部分套件通过都不等于发布成功。GitHub Actions 在 Ubuntu 和 Windows 上使用相同检查入口。

## 文档和本地数据

- [当前交付工具](ops/README.md) 记录固定规则、Docker 部署约束、检查和运行状态判据。
- [历史用户说明](ops/legacy-user-guide.md) 保留旧版详细说明，其中的 v14 学习和 systemd 指令仅供历史查阅。
- [第三方声明](THIRD_PARTY_NOTICES.md) 记录扫码协议改编来源及相应许可。
- [协作约束](AGENTS.md) 要求发布失败后保留证据并向前修复，禁止自动恢复旧代码或学习检查点。

凭据、TLS 私钥、运行状态和现场审计记录留在本地，不提交到仓库。已跟踪的少量 `.audit` 文件是完整离线回归所需的历史样本，不是当前部署或实机验收入口。
