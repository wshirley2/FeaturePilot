# FeaturePilot 执行顺序清单

> 详细设计见 FeaturePilot_项目详细规划.md。
> 按依赖关系推进：上一步验收后，再进入下一步。

---

## 先记住一条主线

~~~text
先跑通 CoreCoder
→ 准备示例仓库和任务
→ 定义 Task / Plan
→ 在副本中改代码
→ 验证并生成 Diff
→ 记录 Trace
→ 做 FastAPI
→ 最后做 Web
~~~

当前不要先做：Web、MCP、多 Agent、Docker、向量数据库、复杂模型路由、用户登录。

---

## 阶段 0：建立基线

### 做什么

- 查看当前 Git 状态，不重置未提交改动。
- 跑 CoreCoder 当前测试和 lint。
- 记录工具、Agent 入口和当前限制。
- 新建 docs/devlog/baseline.md。

~~~powershell
git status --short
git diff --stat
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
~~~

### 完成标准

- [ ] 知道当前测试是否通过。
- [ ] 知道哪些文件已有用户改动。
- [ ] 有一份 baseline 记录。
- [ ] 没有执行清理或重置命令。

未完成前不要改 Agent 核心，也不要做 Web。

---

## 阶段 1：准备示例仓库

创建一个小型 Python CLI 仓库：

~~~text
benchmarks/cli_data_tool/
├── src/
├── tests/
├── README.md
└── pyproject.toml
~~~

第一个任务：

~~~text
为 export 命令增加 --format json 参数。
默认保持原来的 text 输出，并在 README 中增加 JSON 示例。
~~~

验收：

- 不传参数时保持原行为；
- JSON 输出合法；
- 非法格式给出错误；
- README 有示例；
- 项目验证命令通过。

### 完成标准

- [ ] 示例仓库可以单独运行。
- [ ] 第一个任务可以由人手动完成。
- [ ] 验收条件可以被脚本检查。
- [ ] 原始仓库有基准版本。

---

## 阶段 2：建立最小骨架

新增：

~~~text
featurepilot/
├── __init__.py
├── domain/
│   ├── project.py
│   ├── task.py
│   ├── plan.py
│   └── run.py
├── application.py
└── cli.py
~~~

先定义：

| 对象 | 作用 |
|---|---|
| Project | 一个本地代码仓库 |
| Task | 用户的功能需求 |
| Plan | Agent 的结构化实施计划 |
| Run | 一次执行和验证记录 |

先不要连接数据库、Web 或复杂插件。

### 完成标准

- [ ] 四个对象可以构造和序列化。
- [ ] Task 有描述、类型和验收条件。
- [ ] Plan 有步骤、预计文件和验证命令。
- [ ] Run 有状态和结果。
- [ ] featurepilot --help 可以运行。

---

## 阶段 3：仓库分析

### 依次实现

1. 文件枚举和 ignore 规则；
2. Python 文件识别；
3. AST 提取函数、类和 import；
4. 识别 CLI 入口、FastAPI 路由、配置和测试；
5. 从 pyproject.toml 提取验证命令；
6. 根据需求关键词给文件排序；
7. 输出 repository_profile.json。

第一版策略：

~~~text
文件名匹配 + 符号名匹配 + grep 命中 + import 邻接 + 文件角色
~~~

不要马上接向量数据库。

### 完成标准

- [ ] 能分析 cli_data_tool。
- [ ] 能输出项目入口和验证命令。
- [ ] JSON export 任务能找到 CLI、Exporter、README 和相关测试。
- [ ] 每个候选文件有选择理由。

---

## 阶段 4：结构化 Plan

### Plan 必须包含

~~~text
summary
assumptions
steps
read_files
modify_files
expected_files
validation_commands
risks
open_questions
~~~

### 依次实现

1. 定义 Pydantic schema；
2. 根据 Task 和仓库摘要生成 Plan；
3. 校验 JSON；
4. 检查文件和命令；
5. CLI 打印 Plan；
6. 增加 approve / reject；
7. 保存 Plan 版本。

### 完成标准

- [ ] 第一个任务可以生成合法 Plan。
- [ ] 可以人工批准或拒绝。
- [ ] 可以重新生成。
- [ ] Plan 有预计修改文件和验证方式。

---

## 阶段 5：独立 Workspace

### 第一版实现

实现 CopyWorkspaceBackend：

~~~text
原仓库
→ runs/<run_id>/workspace
→ Agent 在副本中工作
→ 对比修改并生成 Diff
~~~

### 必须保护

- 相对路径只能落在 workspace；
- 拒绝 .. 穿越；
- 拒绝 workspace 外绝对路径；
- 原始仓库前后 hash 不变；
- 运行目录不能被 Agent 访问。

### 完成标准

- [ ] Agent 修改的是副本。
- [ ] 原仓库不变。
- [ ] 路径穿越测试通过。
- [ ] 可以生成 final.diff。

---

## 阶段 6：受控工具执行

工具副作用分为：

~~~text
READ       read_file / grep / glob
WRITE      edit_file / write_file
EXECUTE    bash / validation
NETWORK    fetch_url
DELEGATE   agent
~~~

执行链：

~~~text
模型请求工具
→ 参数校验
→ 路径检查
→ Policy 检查
→ 执行
→ 记录结果
→ 返回模型
~~~

第一版 Policy：

- 读工具允许 workspace 内访问；
- 计划内 edit_file 允许；
- 计划外写入拒绝；
- fetch_url 和子 Agent 默认关闭；
- bash 只允许项目配置中的验证命令；
- 删除、安装依赖、网络和危险 Git 命令拒绝；
- 只读工具可并发，写工具串行。

### 完成标准

- [ ] 所有工具请求都经过 Policy。
- [ ] 越权请求被拒绝并说明原因。
- [ ] 工具异常不会破坏 Run。
- [ ] CoreCoder 原有 CLI 仍兼容。

---

## 阶段 7：验证、Diff 和报告

这是第一个完整 CLI 闭环：

~~~text
Task → Plan → Approve → Workspace → Agent 修改 → Validation → Diff → Review Report
~~~

先支持项目配置中的固定命令：

~~~yaml
validation:
  - ["python", "-m", "pytest", "-q"]
  - ["ruff", "check", "."]
~~~

报告包含：

- 任务摘要；
- Plan 摘要；
- 修改文件；
- Diff 统计；
- 验证结果；
- 策略拒绝；
- token、成本和耗时；
- 最终结论。

### 完成标准

- [ ] featurepilot run 能运行第一个任务。
- [ ] 成功和失败都有报告。
- [ ] 生成 final.diff。
- [ ] 原仓库没有被修改。
- [ ] FakeLLM 集成测试通过。

没有这个闭环，不要开始 Web。

---

## 阶段 8：Trace、状态和持久化

记录事件：

~~~text
task.created
plan.generated
plan.approved
run.started
model.finished
tool.requested
policy.decided
tool.finished
file.changed
validation.finished
run.completed
run.failed
~~~

存储：

- SQLite：Project、Task、Plan、Run 元数据；
- JSONL：事件 Trace；
- 文件：Diff、验证日志和报告。

### 完成标准

- [ ] 每个事件有 run_id 和 sequence。
- [ ] 中断后已有事件仍可读取。
- [ ] Run ID 可以找到所有产物。
- [ ] 可以查询历史 Run。
- [ ] secret 不进入 Trace。

---

## 阶段 9：FastAPI 后端

先实现：

~~~text
GET  /api/health
GET  /api/projects
POST /api/projects
GET  /api/projects/{id}/profile
POST /api/projects/{id}/tasks
GET  /api/tasks/{id}
POST /api/tasks/{id}/plan
POST /api/plans/{id}/approve
POST /api/tasks/{id}/runs
GET  /api/runs/{id}
GET  /api/runs/{id}/events
GET  /api/runs/{id}/diff
GET  /api/runs/{id}/review
POST /api/runs/{id}/cancel
~~~

顺序：

1. FastAPI app；
2. Request / Response schema；
3. Project API；
4. Task / Plan API；
5. Run API；
6. Artifact API；
7. SSE；
8. API 测试。

### 完成标准

- [ ] API 能完成 Project 到 Run。
- [ ] SSE 能收到事件。
- [ ] 刷新后历史状态仍存在。
- [ ] 非法路径和未知 Run 被拒绝。

---

## 阶段 10：React Web 工作台

页面顺序：

1. Projects：项目和仓库 Profile；
2. Task Composer：需求、类型、验收条件；
3. Plan Review：计划、文件、风险、验证；
4. Live Run：轮次、工具调用、修改、验证；
5. Change Review：Diff、日志、风险、成本；
6. Run History：历史任务和指标。

前端顺序：

1. API client 和类型；
2. 页面骨架；
3. Task API；
4. Plan API；
5. Run API；
6. SSE；
7. Diff 和日志；
8. 错误、空状态和视觉细节。

### 完成标准

- [ ] 不打开终端也能创建任务。
- [ ] 可以审批计划。
- [ ] 可以实时看到事件。
- [ ] 可以查看 Diff 和验证结果。
- [ ] 刷新页面不丢历史 Run。

---

## 阶段 11：评测和项目包装

准备两个小仓库：

~~~text
cli_data_tool
fastapi_orders
~~~

每个仓库准备 4 个任务：

- 新增 CLI 参数；
- 新增 API 筛选；
- 增加配置能力；
- 小型跨文件重构。

指标：

- 任务完成率；
- 验证通过率；
- 关键文件命中率；
- 无关文件读取比例；
- Plan 与实际文件偏差；
- 计划外修改次数；
- Agent 轮数和工具调用数；
- token、成本和耗时。

最后整理：

- README；
- 架构图；
- Demo GIF；
- 一份成功 Run；
- 一份失败 Run；
- Eval 报告；
- 安全边界；
- 简历 bullet；
- 面试问答。

---

## 当前只做三件事

1. 跑现有 CoreCoder 测试并记录结果；
2. 创建 benchmarks/cli_data_tool 示例仓库；
3. 写出 export --format json 任务及验收条件。

完成这三件后，再开始创建 featurepilot 包。

---

## 继续下一阶段的判断

~~~text
这一阶段有没有可运行产物？
这一阶段有没有自动化测试或手工验收证据？
下一阶段是否真的依赖这一阶段？
~~~

三个答案都是“是”，再继续。
