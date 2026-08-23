# FeaturePilot CLI

> 一个面向本地代码仓库的 CLI Coding Agent：先自然语言交流，再在需要时生成 Plan，并在隔离 Workspace 中执行可审查的任务。

FeaturePilot 把“理解任务到交付变更”放在同一个 Task Runtime 中：你可以直接开始对话，也可以先审阅 Plan、明确批准后再进入隔离 Workspace。两条路径共享 Agent、Runtime、权限和交付产物，只是任务控制程度不同。

## 项目状态

这是一个持续开发中的第一版原型，当前已具备可运行、可演示的核心闭环。

| 能力 | 当前状态 |
|---|---|
| 自然语言 Chat 与仓库分析 | 已实现 |
| 文件读写、搜索、测试与 Lint 工具 | 已实现 |
| 写入前权限确认与 Trusted Diff | 已实现 |
| Plan 创建、修订、明确批准 | 已实现 |
| 隔离 Workspace 中的 Managed Run | 已实现 |
| Validation、Patch、Report、单次 Run 事件产物 | 已实现 |
| 跨回合 Session 保存、恢复、取消与预算 | 已实现（C4） |
| Chat/Managed Run 统一 Runtime 身份与终止结果契约 | 已实现 |
| Chat/Managed Run 共用回合级取消与预算控制 | 已实现第一小切片 |

当前自动化基线：`218 passed, 1 skipped`。另有一套 C4 前 E2-lite 基测为 `4/4 passed`。跳过项是 Windows 当前环境缺少创建符号链接的权限，不代表功能失败。

## 同一 Task Runtime 的两条路径

### Chat：像和编程助手对话

你可以直接用自然语言询问当前仓库，例如“分析一下这个项目的入口”或“在 README 末尾加一行”。Chat 会先读取和理解代码，再提出要调用的工具；涉及写文件或执行可能有副作用的命令时，会先展示修改内容并等待你的允许。

Chat 直接面对当前仓库，适合探索代码、询问问题和小范围修改。

### Managed Run：先审批，再执行完整任务

Managed Run 面向一个有明确目标的开发任务。你先用自然语言描述需求，FeaturePilot 生成一份 Plan（计划），列出要读取和修改的文件、验证命令、验收条件与风险。只有你明确批准后，它才会创建隔离副本并让 Agent 执行。

执行结束后，系统会返回验证结果、可审查的变更补丁和任务报告，原仓库不会被直接修改。

```text
自然语言任务 → Plan（计划） → 你明确批准
             → 隔离副本执行 → 验证结果 + 变更补丁 + 报告
```

几个名词可以这样理解：

| 名词 | 白话解释 |
| --- | --- |
| Workspace | 为本次任务临时复制出的隔离工作目录 |
| Validation | 测试、代码规范检查等验证命令的执行结果 |
| Patch | 描述改了哪些文件、哪些行的 Diff，可先审查再应用 |
| Report | 本次任务的总结、状态、验证结果和风险 |

普通 Chat 的文件写入会先在内存中构造候选内容，展示由 Runtime 根据真实文件生成的 Diff；用户确认后，系统会再次核对文件快照，避免覆盖等待确认期间发生的外部修改。

## 快速开始

### 1. 安装

需要 Python 3.10+，以下示例为 PowerShell：

```powershell
git clone https://github.com/wshirley2/FeaturePilot.git
cd FeaturePilot
python -m pip install -e ".[dev]"
```

安装后可以使用 `featurepilot`。如果终端尚未识别该命令，始终可以使用等价入口：

```powershell
python -m featurepilot.cli
```

### 2. 配置模型

在项目根目录创建 `.env`。以 DeepSeek 为例：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.deepseek.com
FEATUREPILOT_MODEL=deepseek-chat
```

FeaturePilot 使用 OpenAI 兼容接口。也可以通过命令行临时覆盖模型、地址或密钥，例如 `-m <model>`、`--base-url <url>`、`--api-key <key>`。

`FEATUREPILOT_MODEL` 用于指定 FeaturePilot 使用的模型；命令行 `-m <model>` 可以临时覆盖该配置。

### 3. 启动 Chat

以仓库根目录为目标启动：

```powershell
featurepilot .
```

项目内附带了 `benchmarks\cli_data_tool`，这是一个很小的演示/评测仓库，用来快速体验 FeaturePilot 的读取、修改和验证流程；它不是额外依赖，也不是使用 FeaturePilot 的前置条件。

你可以直接体验这个示例仓库：

```powershell
featurepilot benchmarks\cli_data_tool
```

如果要分析自己的项目，把路径换成该项目的根目录即可，不需要复制到 `benchmarks` 目录：

```powershell
featurepilot <你的仓库路径>
```

进入后可以这样说：

```text
分析这个仓库的功能、入口文件和测试方式，不要修改代码。
```

Agent 的读取与搜索操作会在仓库范围内执行。需要写文件或执行需要审批的命令时，终端会展示权限确认面板。

### 5 分钟体验 Demo

下面的流程可以快速验证 Chat 和 Managed Run。项目内附带的
`benchmarks\cli_data_tool` 只是演示仓库，不会修改 FeaturePilot 本身的源码。

```powershell
# 1. 启动 Chat
featurepilot benchmarks\cli_data_tool
# 如果终端不能识别 featurepilot：
# python -m featurepilot.cli benchmarks\cli_data_tool

# 如果要单独体验 C4 Session，建议使用独立目录
# featurepilot benchmarks\cli_data_tool --sessions-dir .tmp\c4-demo

# 2. 在 Chat 中输入
分析这个仓库的功能、入口文件和测试方式，不要修改代码。

# 3. 再输入
请在 README.md 末尾增加一行 demo verification。

# 4. 出现 Diff 和权限面板时，输入 1 允许本次修改
# 5. 退出 Chat 后检查示例仓库的 README.md
Get-Content benchmarks\cli_data_tool\README.md | Select-Object -Last 5
```

如果看到 `demo verification`，说明 Chat 的读取、Diff 展示和受控写入流程正常。

在 Chat 中还可以体验 C4 Session：

```text
/status
/save
/sessions
/session show <session-id>
/compact
/resume <session-id>
```

退出后也可以从 CLI 查询和恢复：

```powershell
featurepilot sessions list benchmarks\cli_data_tool --sessions-dir .tmp\c4-demo
featurepilot chat benchmarks\cli_data_tool --sessions-dir .tmp\c4-demo --resume <session-id>
```

运行限制可以通过 Chat 启动参数体验，例如限制一次回合最多执行一批工具调用：

```powershell
featurepilot benchmarks\cli_data_tool --sessions-dir .tmp\c4-limit --max-tool-rounds 1
```

同一组 `--max-provider-calls`、`--max-tool-rounds`、时间、Token 和费用参数也可用于 `featurepilot run`
与 `featurepilot plan chat`。Managed Run 在 Agent 回合取消或超限后不会继续启动 Validation，而会保留
Workspace、Events、Patch 和 Report，并记录 `cancelled` 或 `limit_reached`。

想验证完整的 Plan / Managed Run 流程，可以退出 Chat 后执行：

```powershell
python -m featurepilot.cli plan chat benchmarks\cli_data_tool
```

然后输入：

```text
先制定计划：在 README.md 末尾增加一行 managed run verification。
```

确认 Plan 内容后输入“批准并执行”。预期结果是：系统创建隔离 Workspace，任务在副本中执行，源仓库不会被直接修改。

### Demo 验收清单

- [ ] Chat 能启动并显示目标仓库路径。
- [ ] Agent 能读取和解释仓库，不修改文件。
- [ ] 写文件前能看到真实 Diff。
- [ ] 输入 `1` 后，目标文件出现预期内容。
- [ ] 输入 `0` 或直接回车拒绝时，文件不会被修改，当前回合会停止。
- [ ] Plan 能展示文件范围、验证命令、验收条件和风险。
- [ ] 未明确批准前，不创建 Workspace，也不执行修改。
- [ ] Managed Run 完成后生成 `validation.json`、`changes.patch` 和 `report.md`。

权限面板中的数字含义是：`1` 仅允许本次，`2` 允许本会话范围，`3` 允许命令前缀，`0` 或直接回车表示拒绝。

## Chat 中的权限确认

写入文件时，FeaturePilot 先展示真实 Diff，再等待你的选择：

```text
1. 仅允许这一次
2. 本会话内允许此范围
3. 本会话内允许命令前缀（仅命令操作会显示）
0. 拒绝（默认；直接回车也拒绝）
```

规则概要：

- 仓库内读取、搜索、只读 Git、测试和 Lint 可按规则自动允许；
- 文件写入、安装依赖、网络访问、普通删除和未知命令需要确认；
- 强制递归删除、破坏性 Git 等明显危险命令会直接拒绝；
- 用户拒绝一次需要确认的副作用操作后，当前 Agent 回合会结束，不会继续反复申请其他写入或命令。

这是一层交互与执行策略，不是容器级安全沙箱。被允许的 Shell 命令仍在本机执行，请只在你信任的仓库中使用。

## 从 Chat 进入 Plan / Managed Run

在同一个 Chat 中，用自然语言描述希望先规划的任务：

```text
先制定计划：为 export 命令增加 --format json，同时保持默认 text 输出不变。
```

FeaturePilot 会生成草稿 Plan，列出预计阅读/修改的文件、验证命令和风险。你可以继续描述新的完整任务来生成修订版本；只有明确输入：

```text
批准并执行
```

系统才会创建 Workspace 并启动 Managed Run。

成功或失败后，运行目录会保留关键产物：

```text
runs/<plan-reference>-<run-id>/
├── workspace/        # 隔离副本，Agent 的实际修改位置
├── run.json          # Run 状态与结果摘要
├── events.jsonl      # 单次 Run 的执行事件
├── validation.json   # 验证结果
├── changes.patch     # 相对源仓库的聚合变更
└── report.md         # 审查报告
```

也可以使用高级命令行入口：

```powershell
featurepilot plan create benchmarks\cli_data_tool --task "为 export 增加 JSON 输出" --name json-export
featurepilot plan approve json-export-v1
featurepilot run json-export-v1
```

## 直接任务路径与计划控制路径

| | 直接任务路径（Chat） | 计划控制路径（Managed Run） |
|---|---|---|
| 使用场景 | 探索、问答、小范围协作 | 需要可审查闭环的开发任务 |
| 代码位置 | 用户当前仓库 | 隔离 Workspace |
| 写入控制 | 每次写入前展示 Trusted Diff | 只执行已批准 Plan 范围内的操作 |
| 交付结果 | 对话和当前会话变更 | Validation、Patch、Report、事件记录 |
| 前置条件 | 无 | 明确批准的 Plan |

## 常用命令

| 命令 | 作用 |
|---|---|
| `featurepilot [repository]` | 默认启动 Chat；省略仓库时使用当前目录 |
| `featurepilot chat [repository]` | 显式启动 Chat |
| `featurepilot profile <repository>` | 输出仓库画像 JSON |
| `featurepilot plan chat [repository]` | 直接进入 Plan 对话入口 |
| `featurepilot plan create ...` | 通过高级命令创建草稿 Plan |
| `featurepilot plan approve <reference>` | 批准 Plan |
| `featurepilot run <reference>` | 执行已批准的 Plan |
| `featurepilot sessions list <repository>` | 列出可恢复的 Chat Session |
| `featurepilot sessions show <session-id> <repository>` | 查看 Session 事件摘要与恢复警告 |

Chat 内可用 `/help` 查看命令；常用的有 `/status`、`/save`、`/sessions`、`/session show`、`/resume`、`/tools`、`/files`、`/diff`、`/tokens`、`/compact`、`/model` 和 `/exit`。

## 验证开发环境

在项目根目录执行：

```powershell
python -m pytest -q -p no:cacheprovider --basetemp='.tmp\pytest-readme'
python -m ruff check --no-cache .
```

## 技术演进与项目边界

FeaturePilot 从一个最小 AgentLoop 原型持续演进为面向本地代码仓库的 Coding Agent。
当前项目的重点是 FeaturePilot 自身的 Runtime、权限、Session、Plan 和 CLI 产品能力，以及这些能力之间的完整协作闭环。

| 基础 Agent 能力 | FeaturePilot 当前实现 |
|---|---|
| AgentLoop、Provider 和基础工具 | `RuntimeBootstrap` 返回统一 `TaskRuntime`，旧 `ChatRuntime` 名称作为兼容别名 |
| 基础仓库交互 | 仓库画像、入口/测试发现与仓库边界 |
| 文件修改与命令执行 | `ALLOW / ASK / DENY` 权限策略与终端确认 |
| 工具调用结果 | 写入前 Trusted Diff、文件快照复核与重新确认 |
| 通用对话执行 | Plan、明确批准、隔离 Workspace 与 Managed Run |
| 运行事件 | Session、Validation、Patch、Report 和可审查事件记录 |

早期基础代码来源于 [CoreCoder](https://github.com/he-yufeng/CoreCoder)；原版权声明和 MIT 许可证保留在 [LICENSE](LICENSE) 中。

## 当前限制与路线

- C4、统一 Runtime 身份/结果以及回合级取消与预算控制已完成；下一步收敛 Validation/子进程取消边界与目录结构；
- `events.jsonl` 当前记录单次 Managed Run，不等同于可恢复的 Chat Session；
- Bash 有路径策略与危险命令拦截，但不是容器或系统级沙箱；
- 工具调用依赖模型服务正确返回标准 Tool Calling；部分本地模型可能只输出看似工具调用的普通文本；
- C4 已限制发送给模型的上下文，但当前 Session 重放仍会构建完整 Events/Messages；超长会话的按需加载、Snapshot 和 Artifact Store 记录在 OPT-013；
- 过程提示的节奏收敛、限制拦截结果的终端降噪仍在产品优化项中持续评估；
- 当前重点是本地 CLI 工作流，不提供 Web、多用户协作或 MCP 平台能力。
- 当前限制参数约束 Agent 回合；Validation 子进程尚未接入同一个 `CancellationToken`，将在后续控制收敛/C5 中处理。

## 路线图

```text
已完成：Chat、Plan、Managed Run、Validation、Patch、Report、权限确认、Trusted Diff、C4、统一 Runtime 身份/结果、共享回合级取消与预算
当前：Validation/子进程取消边界与目录收敛
后续：C5 副作用感知并发、M2 Managed Run 增强、评测与公开 Demo 包装
```

## 技术来源与许可

- 早期技术基础：[CoreCoder](https://github.com/he-yufeng/CoreCoder)
- 许可证：[MIT](LICENSE)
