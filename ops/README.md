# 当前交付工具

唯一离线入口是根目录 `check.py`。`release_index.json` 列出当前文件及所有必跑套件。旧 R3、R5 原生发布脚本和试验锚点保留历史身份，当前入口不能执行它们。当前没有已授权的新实机发布入口。

## 执行完整离线检查

使用 Python 3.12 创建 `.venv-check`，再安装根目录 `requirements.txt`。必须安装 Node，UI 检查不允许静默跳过。

Windows 执行：

```powershell
.\.venv-check\Scripts\python.exe -B check.py
```

Linux 执行：

```sh
.venv-check/bin/python -B check.py
```

检查包含应用、历史无回滚契约、当前启动证据、观察器和交付检查。每套使用独立进程。报告和完整输出保存到新建的 `.audit/check-<id>`。Windows 成功返回 `offline_pass_native_pending`。ARM/Linux 原生检查成功仍显示实机验收待完成。`--require-native` 在非 ARM/Linux 返回非零，不授予原生证明。

Windows 应用测试入口模拟目录 fsync，报告明确列出该适配；生产目录同步保护不变。原生持久化和权限测试按具体测试 ID 跳过，其他跳过或整套没有实际执行测试均不能通过。Linux 不使用该模拟。

修改文件后，先审查差异，再显式运行 `check.py --refresh-inventory` 更新候选清单，最后重新检查。更新清单不授予测试通过、部署成功或实机验收。`--suite` 只用于定位，部分套件通过不是完整检查。

本地 Git 已保留改造前源码快照。`.private`、运行状态、隔离环境和历史审计批量文件均不跟踪。仅离线回归必需的少量历史文件显式跟踪。Git 使用 `.githooks/pre-commit`，提交前自动运行统一检查。CI 配置也运行同一入口。CI 未在远程平台执行，普通 Linux CI 不等于 ARM 实机验收。

提交检查在测试前后核对暂存区和工作区的全部清单文件，拒绝提交未被检查的另一份内容。新克隆需运行 `git config core.hooksPath .githooks` 启用本地钩子。

## 读取运行状态

默认报告的安装、进程、准入、控制、UI 和历史状态均是 `unknown`。离线测试不能改变这些字段。指定 HTTPS 入口及已核实的 PEM CA 文件后可以读取公共状态：

```sh
python -B check.py --status-url https://192.168.3.180:8910 --ca-file verified-public-ca.pem
```

TLS 校验保持启用。该命令只发送 GET，不携带账号、token 或控制命令。页面返回正常只证明接口响应。公共状态不能证明安装文件哈希、PID、启动准入或实机验收，它们仍为 `unknown`。观察器的写入计数也只标为 `writing_reported`，不能冒充独立核验的连续采集。

## 查看当前候选

`ops/startup_evidence` 包含 R6 已验证的失败证据保存。集中版测试继续验证原拒绝帧和尝试累计值能够保留，写入或关闭失败不能报告验收成功。`fixtures/original_verifier.txt` 仅用于离线提取原判据，不能作为原生入口。`phase_replay.py` 验证完整历史与稀疏观察可能出现计时分歧。它不是原实时故障证明。原 30 秒和 6 秒限制保持不变。

`ops/observer` 是尚未安装的观察器候选。它保持自动、手动、配对入口关闭，只发送读取请求。`--record-root` 必须是与正式状态目录不重叠的独立目录。观察历史按 3 秒目标间隔采集、3 天和 64 MiB 保留，有独占写锁。写失败显示错误和累计失败次数。新历史明确标注 `source=observer`、`control_active=false`、`learning_eligible=false`，不能作为控制或学习验收结果。查询 `source=control` 仍只读原历史，不合并成连续控制记录。

候选 systemd 文件只给独立记录目录写权限。当前机器未安装此候选，也未开启新控制试验。原生权限、systemd、TLS、开机启动和实际记录连续性须在相应环境独立核验后才能交付。
