# FeaturePilot

FeaturePilot 是一个在终端里使用的代码助手。给它一个本地仓库，它可以阅读代码、查找入口、解释项目、修改文件并运行测试。

它不追求“收到一句话就埋头改完”，而是让过程保持可见：写文件前先展示 Diff，较大的任务可以先确认计划、再放进隔离副本执行；任务结束后，测试结果、补丁和运行记录都会留下来。

目前是 `0.1.0`，适合本地开发、功能演示和 Coding Agent 工程实践，还不是系统级安全沙箱。

## 快速开始

需要 Python 3.10 或更高版本。下面以 PowerShell 为例：

```powershell
git clone https://github.com/wshirley2/FeaturePilot.git
cd FeaturePilot

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

在项目根目录创建 `.env`。如果使用 DeepSeek：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.deepseek.com
FEATUREPILOT_MODEL=deepseek-chat
```

FeaturePilot 使用 OpenAI 兼容接口，也可以通过 `--api-key`、`--base-url` 和 `-m` 临时覆盖这些配置。

安装成功后，直接把仓库路径交给它：

```powershell
featurepilot benchmarks\cli_data_tool
```

`benchmarks\cli_data_tool` 是项目自带的小型 Python 示例仓库，适合第一次体验。分析自己的项目时，换成对应路径即可：

```powershell
featurepilot D:\path\to\your-project
```

如果终端暂时找不到 `featurepilot` 命令，可以使用等价入口：

```powershell
python -m featurepilot.cli benchmarks\cli_data_tool
```

## 先试一次只读分析

进入 Chat 后输入：

```text
请阅读这个项目，告诉我它解决什么问题、入口在哪里、如何运行测试。不要修改文件。
```

FeaturePilot 会在仓库范围内搜索和读取文件，并把工具调用过程显示在终端中。读取、搜索和其他符合规则的只读操作不需要逐次确认。

## 再试一次受控修改

继续输入：

```text
请在 README.md 末尾增加一行 demo verification。
```

写入发生前，终端会展示基于真实文件生成的 Diff：

- 输入 `1`：只允许这一次；
- 输入 `2`：本次会话内允许同类操作；
- 输入 `0` 或直接回车：拒绝并结束当前回合；
- 执行命令时还可能出现 `3`，用于允许相同命令前缀。

确认后，FeaturePilot 会再次检查文件是否在等待期间被外部修改，避免拿旧内容覆盖新变化。

退出 Chat 后可以直接检查结果：

```powershell
git diff -- benchmarks\cli_data_tool\README.md
```

这条路径直接操作你指定的仓库，适合代码阅读、问答和小范围修改。

## 大任务可以放进隔离副本

如果任务涉及多个文件，或者你希望先看清范围再执行，可以在 Chat 中说：

```text
先制定计划：为 export 命令增加 --format json，同时保持默认 text 输出不变。
```

FeaturePilot 会先生成一份草稿，列出预计读取和修改的文件、验证命令、验收条件与风险。此时不会创建副本，也不会改代码。

只有明确输入：

```text
批准并执行
```

系统才会创建隔离 Workspace，并在副本中完成修改和验证。源仓库不会被直接改动。

一次 Managed Run 通常会留下这些内容：

```text
runs/<plan-reference>-<run-id>/
├── workspace/        # 本次任务使用的隔离副本
├── sessions/         # Agent 会话事件
├── run.json          # 任务状态和结果摘要
├── events.jsonl      # 执行过程事件
├── validation.json   # 测试、Lint 等验证结果
├── changes.patch     # 相对源仓库的完整补丁
└── report.md         # 任务总结、风险和验证结论
```

验证命令可以响应取消和超时。若验证被取消，任务会记录为 `cancelled`，同时保留已生成的 Workspace、Patch 和报告，方便排查或继续处理。

简单说，两种用法的区别是：

- 直接 Chat 面向当前仓库，操作轻，适合边看边改；
- Managed Run 面向一个明确任务，先确认计划，再在隔离副本中交付可审查结果。

它们共用同一套 Agent、权限规则、运行限制和结果状态，不是两套互不相干的执行器。

## Session 与恢复

Chat 会自动保存会话事件。默认位置是：

```text
<repository>/.featurepilot/sessions/<session-id>.jsonl
```

常用的 Chat 命令：

| 命令 | 作用 |
|---|---|
| `/status` | 查看 Session ID、模型、上下文和用量 |
| `/sessions` | 列出当前仓库的会话 |
| `/session show` | 查看当前会话摘要 |
| `/resume <session-id>` | 恢复指定会话 |
| `/compact` | 压缩较长的上下文 |
| `/help` | 查看全部本地命令 |
| `/exit` | 退出 Chat |

也可以在启动时恢复：

```powershell
featurepilot chat benchmarks\cli_data_tool --resume <session-id>
```

如果不想把 Session 放进目标仓库，可以单独指定目录：

```powershell
featurepilot benchmarks\cli_data_tool --sessions-dir .tmp\demo-sessions
```

命令行也能查询已有会话：

```powershell
featurepilot sessions list benchmarks\cli_data_tool
featurepilot sessions show <session-id> benchmarks\cli_data_tool
```

恢复会话会重建对话上下文，但不会重新执行过去的文件写入或 Shell 命令。

## 运行限制

你可以限制单个 Agent 回合能调用模型和工具的次数、运行时长、Token 或费用。例如：

```powershell
featurepilot benchmarks\cli_data_tool --max-tool-rounds 1
```

常用参数包括：

- `--max-provider-calls`：最多请求模型多少次；
- `--max-tool-rounds`：最多执行多少批工具调用；
- `--max-elapsed-seconds`：回合最长运行时间；
- `--max-total-tokens`：Token 上限；
- `--max-estimated-cost-usd`：估算费用上限。

“一批工具调用”不等于“一个工具”。模型可能在同一批中并行读取多个文件，因此 `--max-tool-rounds 1` 仍可能显示多条工具记录。到达限制后，结果会明确记录为 `limit_reached`，不会伪装成正常完成。

目前这些限制主要约束单个 Agent 回合；覆盖 Agent、Validation 和产物收口全过程的整次任务预算还在开发中。

## 权限与安全边界

FeaturePilot 对操作做三类判断：允许、询问、拒绝。

- 仓库内读取、搜索、只读 Git、测试和 Lint 可按规则直接允许；
- 文件写入、安装依赖、网络访问、普通删除和未知命令需要确认；
- 强制递归删除、破坏性 Git 等高风险操作会直接拒绝；
- 用户拒绝一次有副作用的操作后，当前 Agent 回合会结束，不会换一种写法反复申请。

这是一套交互和执行策略，不是容器级隔离。经你允许的 Shell 命令仍会在本机运行，请只对可信仓库使用。Managed Run 的 Workspace 是任务副本，也不能替代操作系统沙箱。

## 常用入口

| 命令 | 作用 |
|---|---|
| `featurepilot [repository]` | 启动 Chat；省略路径时使用当前目录 |
| `featurepilot chat [repository]` | 显式启动 Chat |
| `featurepilot profile <repository>` | 输出仓库画像 JSON |
| `featurepilot plan chat [repository]` | 直接进入计划对话 |
| `featurepilot plan create ...` | 创建草稿 Plan |
| `featurepilot plan approve <reference>` | 批准 Plan |
| `featurepilot run <reference>` | 执行已批准的 Plan |
| `featurepilot sessions list <repository>` | 列出可恢复的 Session |
| `featurepilot sessions show <id> <repository>` | 查看 Session 摘要和恢复警告 |

完整参数可以通过 `featurepilot --help` 或各子命令的 `--help` 查看。

## 当前进度

FeaturePilot 已经跑通这些核心链路：

- 仓库画像、自然语言 Chat 和工具调用；
- 写入前 Trusted Diff、文件快照复核和权限确认；
- Plan 修订、明确批准与隔离 Workspace；
- Validation、Patch、Report 和运行事件；
- Session 保存、恢复、上下文压缩与用量展示；
- Chat 与 Managed Run 统一的取消、超限和结果状态；
- Validation 子进程取消、超时与进程树清理；
- Chat Session 与 Managed Run 产物的目录边界。

当前自动化基线为 `226 passed, 1 skipped`，Ruff 检查通过。跳过项来自 Windows 环境缺少创建符号链接的权限，不代表主流程失败。

接下来的重点是整次任务级预算，以及内部 Runtime 包结构的收敛。更细的设计和开发顺序见 `docs/`。

## 开发与验证

在项目根目录执行：

```powershell
python -m pytest -q -p no:cacheprovider --basetemp='.tmp\pytest-readme'
python -m ruff check --no-cache .
```

## 已知限制

- Shell 命令有路径策略和危险命令拦截，但没有系统级沙箱；
- 不同模型对标准 Tool Calling 的支持程度不同；
- 超长 Session 仍会在恢复时构建完整事件和消息，按需加载尚未实现；
- 运行限制目前以单个 Agent 回合为主，还不是完整的 whole-run 预算；
- 当前产品重心是本地 CLI，不包含 Web、多用户协作或 MCP 平台能力。

## 来源与许可

项目早期基于 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 的最小 AgentLoop 继续开发；现阶段主要代码集中在 FeaturePilot 的 Runtime、权限、Session、Plan、Managed Run 和 CLI。原版权声明与 MIT 许可证保留在 [LICENSE](LICENSE) 中。
