# FeaturePilot

FeaturePilot 是一个在终端里使用的代码助手。给它一个本地仓库，它可以阅读代码、查找入口、解释项目、修改文件并运行测试。

它不追求“收到一句话就埋头改完”，而是让过程保持可见：你可以直接在对话中阅读、修改和验证代码；写文件前会展示 Diff，任务结束后也能检查结果。

目前是 `0.1.0`，适合本地开发、功能演示和 Coding Agent 工程实践，还不是系统级安全沙箱。

## 快速开始

需要 Python 3.10 或更高版本。下面以 PowerShell 为例：

### 快速体验

当前尚未发布到 PyPI，使用 pipx 从 GitHub 安装即可：

```powershell
pipx install "git+https://github.com/wshirley2/FeaturePilot.git"
featurepilot
```

首次启动会进入 Provider 配置向导。项目正式发布到 PyPI 后，安装地址可简化为 `pipx install featurepilot`。

### 从源码安装与开发

```powershell
git clone https://github.com/wshirley2/FeaturePilot.git
cd FeaturePilot

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

首次安装后，直接运行主入口会进入一次性的配置向导：

```powershell
featurepilot
```

向导保存 Provider、Base URL、Model 和 API Key 到当前用户目录：Windows 为
`%APPDATA%\FeaturePilot\config.json`，Linux/macOS 为
`~/.config/featurepilot/config.json`。API Key 不会打印、提交或写入安装包。之后
`featurepilot` 是默认的 TUI 入口；非交互终端会自动回退到普通文本 Chat。

也可以随时显式初始化或修复配置：

```powershell
featurepilot init
featurepilot reconfigure
```

项目根目录的 `.env` 仍可作为团队共享的最低优先级默认值。如果使用 DeepSeek：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.deepseek.com
FEATUREPILOT_MODEL=deepseek-v4-flash
```

配置优先级固定为：`CLI 参数 > 环境变量 > 用户配置 > 项目 .env`。这里的模型名会原样显示在 Chat 和 `/status` 中。当前 DeepSeek 配置可使用
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

首次打开仓库会先确认目录信任；TUI 的快捷键和 Chat 命令可以在运行后输入 `/help` 查看。

## 使用方式

进入 Chat 后，可以先从只读问题开始：

```text
请阅读这个项目，告诉我它解决什么问题、入口在哪里、如何运行测试。不要修改文件。
```

TUI 会展示流式回答、工具调用、Session 状态和待确认操作。写入文件前会先展示 Diff；输入 `1` 允许一次，输入 `2` 在当前会话内允许同类操作，直接回车拒绝。输入 `/help` 查看 Chat 内命令，常用的是 `/status`、`/resume <session-id>` 和 `/exit`。

Session 默认保存在目标仓库的 `.featurepilot/sessions/` 下；可以用 `--resume` 恢复，也可以用 `--sessions-dir` 指定其他目录。恢复只重建对话，不会重新执行历史工具调用。

## 安全边界

读取、搜索和常规测试通常可以直接执行；写文件、安装依赖、网络访问和命令执行会经过权限确认。删除、仓库外写入和破坏性 Git 操作会被阻止。FeaturePilot 提供的是交互和执行策略，不是系统级沙箱。

更多参数可通过 `featurepilot --help` 或各子命令的 `--help` 查看。

## 当前边界

当前产品不包含 Web、多用户协作、MCP 平台、Role/Skill 平台或系统级沙箱。C5 已完成第一版：模型完整返回一轮 Tool Call 后，安全且资源明确的读取可并发；写入、Bash、网络、委派和未知工具仍串行。Plan、Workspace 和 Managed Run 是显式高级命令，不会自动介入普通 Chat。

## 来源与许可

项目早期参考 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 的最小 AgentLoop 继续开发；当前运行内核已收敛到 `featurepilot.engine`，唯一公开命令和发行包均为 FeaturePilot。许可信息见 [LICENSE](LICENSE)。
