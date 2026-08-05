# CLI Data Tool

一个用于 FeaturePilot 评测的最小 Python CLI 项目。

## 当前功能

首次运行时，先在本目录安装这个示例包：

```powershell
python -m pip install -e .
```

`export` 命令默认以 text 格式输出数据：

```powershell
python -m cli_data_tool.cli export
```

也可以指定要导出的项目：

```powershell
python -m cli_data_tool.cli export --items one two
```

当前版本还不支持 JSON 输出。后续任务是增加 `--format json`，同时保持默认的 text 行为不变。

## 验证

在本目录执行：

```powershell
python -m pytest -q
python -m ruff check .
```
