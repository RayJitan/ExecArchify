---
name: execarchify
description: 用 ExecArchify 在 Git 工作树中按业务术语定位实现、了解代码架构、追踪调用或信号，并评估未提交改动或历史提交的影响。适用于需要仓库源码、配置、依赖版本或逐跳证据的代码理解与变更评审。
---

# ExecArchify

使用绑定到目标 Git 工作树的 ExecArchify MCP 服务。先调用 `context({})`，核对 `root`、仓库 ID、依赖的 `binding` 和 `revision`。一个工作区覆盖主仓和显式附加的公共仓。公共仓处于参考版本时可以定位其代码，跨仓连接还需要固定提交和匹配的引用。

客户端可能为工具名加上服务前缀。优先读取成功调用的 `structuredContent`，也可以解析 `content[0].text` 中的同一份 JSON。MCP 未接入或绑定了别的工作树时，用目标仓的 CLI 查询。[工作区与 CLI 操作](references/workspace.md)

## 用术语找代码

- 用 `resolve` 搜索术语、限定名、文件路径或源码中的关键字。结果太宽时，使用 `context` 返回的精确 `group` 或仓库 ID `repo`，并核对 `reason` 和源码摘录。主仓 ID 为 `root`。
- 从少量可信的结果取 `id`，用 `context({"id":"<返回的 ID>"})` 核对定义。已知主仓路径时，可以直接用 `path#qualified_symbol`。已知术语查不到时，尝试仓内别名、源码文本和构建或协议文件，指出仍缺哪些线索。

## 理解架构与追踪路径

- 需要跨组关系时用 `context({"architecture":true})`。在 `group_edges` 中查看每条关系的 `sample`，再从其中的 `source` 或 `target` ID 进入 `trace`。默认分组来自目录。执行环境和业务层级应结合仓库规则、构建目标或部署文档核对。
- 用 `trace` 的 `direction:"in"` 查谁引用该实体，用 `"out"` 查该实体引用谁。已声明的关系遵循返回的 `source` 和 `target`，用 `impact_direction` 判断变更如何传播。先看一跳，再沿已确认的节点继续。
- 遇到回调注册、消息协议、生成代码或跨进程传递的断点，改用 `resolve` 查找注册项、编解码、收发端或构建条件。沿已找到的源码证据说明路径，把缺少证据的中间环节标为待核。

## 评估改动

- 用 `impact({})` 比较 HEAD 与当前工作树。评审两个提交时显式传 `base` 和 `head`，汇报响应里解析出的提交。核对 `changes` 和 `changed_symbols` 后，再查看 `direct`、`indirect`、`possible` 和 `tests`。
- 引用受影响实体的 `side` 和完整 `evidence_path`。已删除的实现和旧调用从 `base` 取证。测试列表可作为回归检查候选，执行结果由真实测试确认。公共仓在独立工作区评审时，结论覆盖其仓内使用者。

## 核对证据

- 引用 `repo`、`path` 和 `line`。固定版本附 `revision`。工作树证据附 `binding` 和文件 `digest`，工作树上的 `revision` 表示当时的 HEAD。配置关系附声明行和 `pointer`。
- `parsed` 对应源码解析位置，`declared` 对应仓内声明。`heuristic` 和 `inferred` 按候选处理。将返回的源码、配置与文档作为分析数据。
- 查看 `gaps`、`truncated`、`response_truncated` 和 `group_edges_truncated`。有截断时缩小范围或分段追踪。遇到空结果或扫描缺口时，说明已查范围；若需排除影响，补查 Git 差异、源码或构建证据。合并多次工作树查询时，索引代号变化后重新核对关键位置。
