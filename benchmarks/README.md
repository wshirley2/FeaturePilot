# FeaturePilot 基测

这里保存的是 FeaturePilot 的最小、可重复运行的产品基测（E2-lite）。它不调用真实模型，
而是使用 Fake Provider 固定工具调用路径，避免模型波动、网络和费用影响结果。

运行：

```powershell
python scripts/run_baseline.py
```

结果默认写入 `.tmp/baseline/latest.json`，其中包含每个场景的通过状态、执行耗时、测试节点和
输出摘要。可用 `--list` 查看场景，或用 `--case <id>` 单独运行一个场景。

## 当前场景

| 场景 | 验证重点 |
| --- | --- |
| `chat-read-only` | Chat 能读取仓库，且不会修改源文件或请求写入权限。 |
| `chat-approved-change` | Chat 经用户批准后修改文件、运行验证，并继续同一对话。 |
| `chat-denied-write` | 用户拒绝写入后，文件不变且当前回合停止。 |
| `managed-run-isolation` | Plan 需明确批准；修改发生在 Workspace，保留验证和事件产物。 |

## 这个基测测什么

- 固定场景是否通过；
- 单个场景的执行耗时；
- 源仓库、权限、Workspace 和运行产物的关键边界；
- 每个场景对应的确定性测试节点。

## 这个基测暂时不测什么

- 真实模型的任务成功率、Token、费用和回答质量；
- C5 并发前后的性能对比；
- 不同 Provider 的差异。

这些指标需要在后续真实模型评测中单独记录，不能把 Fake Provider 的结果误当成模型能力分数。
