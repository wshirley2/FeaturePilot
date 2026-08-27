# TechPilot E2-lite 基线 v0.1.0

记录时间：2026-08-22  
环境：Windows 11、Python 3.12.5、Fake Provider（无网络、无真实模型费用）

运行命令：

```powershell
python scripts/run_baseline.py --output .tmp\baseline\v0.1.0.json
```

| 场景 | 结果 | 本机耗时 | 证明的产品边界 |
| --- | --- | ---: | --- |
| `chat-read-only` | 通过 | 2.365s | Chat 读取仓库但不修改文件。 |
| `chat-approved-change` | 通过 | 3.236s | 用户批准后才写入，并运行验证。 |
| `chat-denied-write` | 通过 | 2.216s | 用户拒绝后文件不变，当前回合停止。 |
| `managed-run-isolation` | 通过 | 3.224s | 明确批准后在 Workspace 执行，并保留事件产物。 |

汇总：**4/4 通过**。

本文件中的耗时是本机确定性测试的参考值，不作为跨机器性能门槛，也不代表真实模型的响应速度、Token
消耗或费用。后续 C4、C5、M2 改动后应重跑 `scripts/run_baseline.py`，先比较四个场景是否仍通过；
真实模型的成功率、成本和 Provider 对比应另建评测记录。
