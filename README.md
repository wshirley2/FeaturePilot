# FeaturePilot

FeaturePilot 是一个在终端里使用的代码助手。给它一个本地仓库，它可以阅读代码、查找入口、解释项目、修改文件并运行测试。

它不追求“收到一句话就埋头改完”，而是让过程保持可见：你可以直接在对话中阅读、修改和验证代码；写文件前会展示 Diff，任务结束后也能检查结果。

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
FEATUREPILOT_MODEL=deepseek-v4-flash
```

这里的模型名会原样显示在 Chat 和 `/status` 中。当前 DeepSeek 配置可使用
`deepseek-v4-flash` 或 `deepseek-v4-pro`；也可以通过 `--api-key`、`--base-url`
和 `-m` 临时覆盖这些配置。

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
| `/details` | 列出本 Session 已折叠的 Tool Call，选择一条展开 |
| `/details <tool-call-id> [page]` | 查看指定 Tool Call 已保存的完整详情；大结果按页显示 |
| `/help` | 查看全部本地命令 |
| `/exit` | 退出 Chat |

也可以在启动时恢复：

```powershell
featurepilot benchmarks\cli_data_tool --resume <session-id>
```

如果不想把 Session 放进目标仓库，可以单独指定目录：

```powershell
featurepilot benchmarks\cli_data_tool --sessions-dir .tmp\demo-sessions
```

恢复会话会重建对话上下文，但不会重新执行过去的文件写入或 Shell 命令。

## 可选的运行限制

你可以限制单个 Agent 回合能调用模型和工具的次数、运行时长、Token 或费用。例如：

```powershell
featurepilot benchmarks\cli_data_tool --max-tool-rounds 1
```

也可以用 `--max-provider-calls`、`--max-elapsed-seconds`、`--max-total-tokens` 或 `--max-estimated-cost-usd` 控制一次对话的调用次数、时间、Token 和估算费用。达到限制后，终端会明确提示，不会伪装成正常完成。

## 权限与安全边界

FeaturePilot 对操作做三类判断：允许、询问、拒绝。

- 仓库内读取、搜索、只读 Git、测试和 Lint 可按规则直接允许；
- 文件写入（包括依赖清单、lock 文件、迁移、CI/部署配置）、安装依赖、网络访问和可解析的一般命令需要确认；
- 删除、批量移动或重命名、仓库外写入、破坏性 Git、复杂 Shell 结构以及发布/推送会直接拒绝；
- 用户拒绝一次有副作用的操作后，当前 Agent 回合会结束，不会换一种写法反复申请。

这是一套交互和执行策略，不是系统级沙箱。经你允许的 Shell 命令仍会在本机运行，请只对可信仓库使用。

更多命令和参数可通过 `featurepilot --help` 或各子命令的 `--help` 查看。

## 已知限制

- Shell 命令有路径策略和危险命令拦截，但没有系统级沙箱；
- 不同模型对标准 Tool Calling 的支持程度不同；
- 超长 Session 仍会在恢复时构建完整事件和消息，按需加载尚未实现；
- 当前产品重心是本地 CLI，不包含 Web、多用户协作或 MCP 平台能力。

## 来源与许可

项目早期基于 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 的最小 AgentLoop 继续开发；现阶段主要代码集中在 FeaturePilot 的 Runtime、权限、Session、Chat 和 CLI。原版权声明与 MIT 许可证保留在 [LICENSE](LICENSE) 中。
