# Feature Task: JSON export

为 `export` 命令增加 `--format` 参数，支持 `text` 和 `json` 两种输出格式。

## 验收条件

1. 不传 `--format` 时，保持当前 text 输出行为不变。
2. `--format text` 与默认行为一致。
3. `--format json` 输出合法 JSON。
4. JSON 输出至少包含 `items` 和 `count` 字段。
5. 传入不支持的格式时，CLI 给出清晰错误并返回失败状态。
6. 相关测试通过。
7. README 中补充 JSON 使用示例。
