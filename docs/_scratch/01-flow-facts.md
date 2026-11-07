# 现状事实报告

## 1. 实现状态表

| 模块 / 文件 | 实现状态 | 说明 |
|---|---|---|
| `pipeline/` | 空桩(仅占位或 TODO) | `graph.py:1`、`state.py:1`、`nodes.py:1` 均仅有 1 行模块 docstring，无实质代码。 |
| `static_analysis/` | 已实现 | `slither_runner.py:274` 封装 Slither 执行与错误分类，`findings_parser.py:66` 纯函数解析 JSON 为 Finding。 |
| `context/` | 部分实现 | `source.py:200` 与 `budget.py:39` 实现了行号处理与 token 截断；`slicer.py:1`、`code_parser.py:1`、`call_graph.py:1` 仅有 1 行 docstring。 |
| `rag/` | 空桩(仅占位或 TODO) | `embedder.py:1`、`ingest.py:1`、`retriever.py:1`、`store.py:1` 均仅有 1 行模块 docstring。 |
| `agents/` | 部分实现 | `base.py:106`、`discoverer.py:29`、`grounding.py:80` 已实现；`verifier.py:1`、`judge.py:1` 仅有 1 行 docstring。 |
| `tools/` | 部分实现 | `registry.py:126` 实现工具注册与分发；`code_reader.py:1`、`forge_runner.py:1`、`kb_search.py:1` 仅有 1 行 docstring。 |
| `llm/` | 已实现 | `client.py:46`、`structured.py:231`、`models.py:44`、`cache.py:67`、`cost.py:69` 完整实现调用、重试、缓存与预算。 |
| `schemas/` | 已实现 | `finding.py:127`、`poc.py:51`、`report.py:95`、`taxonomy.py:27` 完整定义了 Finding、PoC、报告与分类标准模型。 |
| `report/` | 空桩(仅占位或 TODO) | `markdown.py:1`、`json_export.py:1` 仅有 1 行 docstring；`templates/report.md.j2:3` 仅含 TODO 占位符。 |
| `observability/` | 部分实现 | `tracer.py:72` 实现 JSONL 格式的 Span 与 Blob 追踪；`logger.py:1` 仅有 1 行 docstring。 |
| `cli.py` | 空桩(仅占位或 TODO) | `cli.py:9-22` 定义了 `audit`、`eval`、`index-kb` 命令，函数体均仅执行 `typer.echo("[todo] ...")`。 |

## 2. 当前可运行的真实调用链

- 调用链：`agent/src/audit_agent/cli.py:app` -> `agent/src/audit_agent/cli.py:audit` -> `typer.echo` (`agent/src/audit_agent/cli.py:12`)。
- 能跑到哪一步为止：执行至 `agent/src/audit_agent/cli.py:12` 打印 `[todo] audit <path>` 后直接返回退出。`cli.py:1-26` 未 import 也未调用任何内部业务模块（pipeline、agents、static_analysis 等均未被触发）。

## 3. 数据流转

沿上述可运行调用链：
1. CLI 命令行参数 -> `agent/src/audit_agent/cli.py:audit`:
   - 输入类型：`pathlib.Path`（参数 `path`，`cli.py:10`）。
   - 输出类型：`None` (`cli.py:10`)。
2. 终端信息输出 -> `typer.echo` (`agent/src/audit_agent/cli.py:12`):
   - 输入类型：`str`（内容为 `f"[todo] audit {path}"`，`cli.py:12`）。
   - 输出类型：`None`。

注：`SourceContext`、`Finding`、`AuditReport` 等业务数据流在 `cli.py` 当前执行链中未发生流转。

## 4. LangGraph 现状

- 是否已有 `graph.py` / `state.py` / `nodes.py` 的实际内容：未找到。
  - `agent/src/audit_agent/pipeline/graph.py:1` 仅有 1 行注释。
  - `agent/src/audit_agent/pipeline/state.py:1` 仅有 1 行注释。
  - `agent/src/audit_agent/pipeline/nodes.py:1` 仅有 1 行注释。
- state 的字段：未找到。
- 已注册的节点：未找到。
- 边（含条件边）：未找到。

## 5. 失败与降级路径

1. **Slither 失败**：
   - 处理位置：`agent/src/audit_agent/static_analysis/slither_runner.py:248-261` (`_execute`) 及 `338-358` (`run_slither`)。
   - 处理逻辑与返回值：超时、工具缺失或编译失败均被 `classify_failure` 分类捕获（除目标文件不存在抛 `FileNotFoundError` 外不向上抛出异常，`slither_runner.py:285, 291`），降级返回 `StaticAnalysisResult` (`slither_runner.py:354`)；其 `info.status` 记录失败原因枚举（`slither_runner.py:339`），`findings` 返回空列表 `[]` (`slither_runner.py:356`)。
2. **LLM 输出校验失败**：
   - 处理位置：`agent/src/audit_agent/llm/structured.py:282-298` (`structured_chat`) 及 `agent/src/audit_agent/agents/base.py:317-328` (`Agent._run_step`)。
   - 处理逻辑与返回值：Pydantic 校验失败返回 `ParseFailure` (`structured.py:209`)。重试限额内通过 `feedback_user_message` 将校验错误追加进消息体回喂模型重试（`structured.py:293`、`base.py:327`）。重试耗尽时抛出 `StructuredOutputError`（`structured.py:296`、`base.py:320`），Agent 循环不予吞异常，写入 trace blob 后直接向上 re-raise (`base.py:33, 220`)。
3. **LLM 超时 / 预算超限**：
   - LLM 单次超时：OpenAI SDK 抛出超时异常，在 `agent/src/audit_agent/llm/client.py:156-157` 捕获并封装抛出 `LLMError`；`agent/src/audit_agent/agents/base.py:33, 220` 不捕获此异常，写入 trace blob 后向上 re-raise。
   - Agent 整体超时：`agent/src/audit_agent/agents/base.py:232-234` 在轮次间检查截止时间，超时直接返回 `AgentResult(stop_reason=StopReason.TIMEOUT, content=None, value=None)`；工具排队执行时超时返回 `ToolResult(error_kind="skipped")` (`base.py:335-337`)。
   - 预算超限：`agent/src/audit_agent/llm/cost.py:109-116` (`CostTracker.check`) 在调用前检查 token 和成本，超限抛出 `BudgetExceeded` (`cost.py:61`)；`client.py:153` 与 `base.py:33, 220` 均不吞该异常，直接向上抛出。

## 6. 疑点清单

1. `FindingDraft` 与 `Finding.from_draft` 成为死代码：
   `agent/src/audit_agent/schemas/finding.py:48, 136` 定义了面向 LLM 的契约 `FindingDraft` 与工厂方法 `from_draft`；但实际 `Discoverer` 使用的是 `DiscovererOutput` / `DiscovererItem` (`schemas/finding.py:72, 121`；`agents/discoverer.py:65`)，并在 `agent/src/audit_agent/agents/grounding.py:132-149` 直接实例化 `Finding`，`FindingDraft` 与 `from_draft` 在业务调用链中未被使用。
2. `structured_chat` 独立存在但未在 Agent 中复用：
   `agent/src/audit_agent/llm/structured.py:231` 实现了完整的结构化重试函数 `structured_chat`；但 `agent/src/audit_agent/agents/base.py:317-328` 内部另行实现了校验与重试逻辑，`structured_chat` 仅在测试中被调用。
3. 提示词版本命名风格不一致：
   `agent/src/audit_agent/schemas/report.py:19` 中 `RunMeta.prompt_version` 默认值为 `"reentrancy-v1"`（连字符）；而 `agent/src/audit_agent/agents/discoverer.py:35` 及对应文件名 `agent/src/audit_agent/agents/prompts/discoverer/reentrancy.v1.md` 使用 `"reentrancy.v1"`（点号）。
4. `StaticAnalysisResult` 与 `AuditReport` 字段结构脱节：
   `agent/src/audit_agent/static_analysis/slither_runner.py:91` 中 `StaticAnalysisResult` 将 `tool_versions` 置于顶层；而 `agent/src/audit_agent/schemas/report.py:22, 99` 中 `AuditReport.static_analysis` 仅为 `StaticAnalysisInfo`，`tool_versions` 归属于 `RunMeta`，两者字段归属不一致且未见组装映射代码。
5. `read_events` 未被业务代码调用：
   `agent/src/audit_agent/observability/tracer.py:135` 实现了 `read_events`，但除测试外无业务代码引用。
6. `cli.py` 与所有业务模块完全未连接：
   `agent/src/audit_agent/cli.py:1-26` 未 import 任何内部业务模块，命令行参数无法进入任何分析流程。
