# 工作区与 CLI 操作

让 stdio 客户端启动 `execarchify mcp --workspace <主仓绝对路径>`。工具调用使用该工作树的索引，首次查询也可同步索引。指定 `--no-watch` 时，MCP 进程只在有查询时触发同步。

## CLI 回退

在目标 Git 工作树中运行以下命令，或在查询子命令后使用 `--workspace <主仓绝对路径>`。示例里的标识、路径、组和 Git 修订需替换为目标仓的实际值。

```sh
execarchify context
execarchify context --architecture
execarchify resolve "团队术语" --group "Runtime" --repo root --limit 8
execarchify context --id "src/codec.py#normalize"
execarchify trace "src/codec.py#normalize" --direction out --depth 2
execarchify impact
execarchify impact --base "<旧提交>" --head "<新提交>"
```

固定提交里的源码可用 `git -C <该仓路径> show '<提交>:<仓内路径>'` 核对。工作树上的大段上下文可从本地文件查看。MCP 和 CLI 都不可用时，可以用 Git、源码和构建清单完成当前调查，并注明依赖范围和仍待核对的路径。

## 配置和导出

- 只需主仓时直接查询即可。需要显式选择公共仓或持续后台同步时，在目标仓运行 `execarchify init .`，按需加上每个 `--shared-repo <路径>`。再次运行 `init` 会替换公共仓选择。使用 `status` 查看当前配置和观察进程。
- 需要团队维护术语映射、分组或已知信号时，在目标主仓的 `.execarchify.json` 使用 `terms`、`layers` 或 `relations`。第一个命中的路径规则决定分组。关系可使用主仓相对路径、`path#qualified_symbol` 或带 `repo`、`path`、可选 `symbol` 的对象。按实际依赖语义选择 `forward`、`reverse`、`both` 或 `none`。固定公共仓版本用主仓 gitlink 或 `pins` 中的提交 ID。
- 需要交互查看时用 `execarchify serve`。需要 Archify 文件时用 `execarchify export --format archify --output architecture.json`。
