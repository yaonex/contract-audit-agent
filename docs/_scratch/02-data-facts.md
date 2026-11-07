# 数据契约事实报告

## 1. 模型清单 (schemas/, static_analysis/, context/)

| 类名 | 类别与位置 | 用途 | 关键字段 (名称: 类型) | 引用方 (生产者 / 消费者) |
|---|---|---|---|---|
| `CodeLocation` | Pydantic (`schemas/finding.py:34`) | 代码物理位置锚点 | `file: str`, `line_start: int`, `line_end: int`, `contract: str\|None`, `function: str\|None` | 生产: `findings_parser.py:218`, `grounding.py:136` / 消费: `FindingDraft`, `Finding` |
| `FindingDraft` | Pydantic (`schemas/finding.py:48`) | 面向 LLM 的原始发现草稿契约 | `category: str`, `title: str`, `description: str`, `location: CodeLocation`, `evidence: list[str]`, `severity: Severity`, `confidence: float` | 生产: 仅单测 / 消费: `Finding` 继承此基类 |
| `DiscovererItem` | Pydantic (`schemas/finding.py:72`) | Discoverer 模型输出的扁平单条项 | `title: str`, `description: str`, `line_start: int`, `line_end: int`, `function: str\|None`, `contract: str\|None`, `quotes: list[str]`, `severity: Severity`, `confidence: float` | 生产: LLM 响应解析 (`discoverer.py:65`) / 消费: `grounding.py:102` |
| `DiscovererOutput` | Pydantic (`schemas/finding.py:121`) | Discoverer 模型响应外层信封 | `findings: list[DiscovererItem]` | 生产: `Agent.run` (`discoverer.py:65`) / 消费: `discoverer.py:67` |
| `Finding` | Pydantic (`schemas/finding.py:127`) | 系统标准漏洞实体 (继承 FindingDraft) | 继承字段 + `id: str`, `status: FindingStatus`, `source: FindingSource`, `rule_id: str\|None` | 生产: `findings_parser.py:214`, `grounding.py:132` / 消费: `StaticAnalysisResult`, `AuditReport` |
| `IntegrityCheck` | Pydantic (`schemas/poc.py:28`) | 单项 PoC 防作弊检查结果 | `name: str`, `passed: bool`, `detail: str` | 生产: 未找到 / 消费: `PocAttempt.integrity_checks` |
| `PocAttempt` | Pydantic (`schemas/poc.py:36`) | 单次 PoC 运行尝试记录 | `round_no: int`, `poc_source: str`, `outcome: PocOutcome`, `integrity_checks: list[IntegrityCheck]` | 生产: 未找到 / 消费: `VerificationResult.attempts` |
| `VerificationResult` | Pydantic (`schemas/poc.py:51`) | PoC 验证最终结论 | `finding_id: str`, `status: FindingStatus`, `attempts: list[PocAttempt]`, `rejection_basis: RejectionBasis\|None` | 生产/消费: 未在业务代码中实例化 |
| `RunMeta` | Pydantic (`schemas/report.py:16`) | 审计运行元信息 | `run_id: str`, `model: str`, `prompt_version: str`, `tool_versions: dict[str, str]` | 生产/消费: `AuditReport.meta` |
| `CostSummary` | Pydantic (`schemas/report.py:27`) | Token 与资金成本快照 | `prompt_tokens: int`, `completion_tokens: int`, `cost_usd: float`, `llm_calls: int` | 生产: `CostTracker.snapshot` (`cost.py:157`) / 消费: `AuditReport.cost` |
| `StaticAnalysisInfo` | Pydantic (`schemas/report.py:47`) | 静态分析执行统计与状态 | `status: StaticAnalysisStatus`, `error: str\|None`, `n_results: int`, `n_kept: int` | 生产: `slither_runner.py:338` / 消费: `StaticAnalysisResult.info`, `AuditReport.static_analysis` |
| `DiscoveryInfo` | Pydantic (`schemas/report.py:62`) | LLM 发现与过滤统计 | `model_returned: int`, `kept: int`, `dropped_outside_file: int`, `dropped_no_verified_quote: int` | 生产: `ground_findings` (`grounding.py:173`) / 消费: `AuditReport.discovery` |
| `ContextInfo` | Pydantic (`schemas/report.py:80`) | 源码送模覆盖度元数据 | `file: str`, `total_lines: int`, `included_ranges: list[tuple[int, int]]`, `truncated: bool` | 生产: `SourceContext.to_info` (`source.py:106`) / 消费: `AuditReport.context` |
| `AuditReport` | Pydantic (`schemas/report.py:95`) | 顶层审计报告整体对象 | `meta: RunMeta`, `target: str`, `cost: CostSummary`, `static_analysis: StaticAnalysisInfo`, `findings: list[Finding]` | 生产/消费: 未在业务流水线中组装实例化 |
| `Category` | Pydantic (`schemas/taxonomy.py:17`) | 分类法单条定义 | `id: str`, `name: str`, `swc: list[str]`, `slither_detectors: list[str]` | 生产: `Taxonomy.categories` / 消费: `taxonomy.py:54` |
| `Taxonomy` | Pydantic (`schemas/taxonomy.py:27`) | 分类法完整规范 | `version: int`, `categories: list[Category]` | 生产: `load_taxonomy` (`taxonomy.py:72`) / 消费: `findings_parser.py:71`, `FindingDraft` 校验 |
| `StaticAnalysisResult` | Pydantic (`static_analysis/slither_runner.py:91`) | 静态分析对外最终结果 | `info: StaticAnalysisInfo`, `findings: list[Finding]`, `tool_versions: dict[str, str]` | 生产: `run_slither` (`slither_runner.py:354`) / 消费: 外部调用方 |
| `_Outcome` | dataclass (`static_analysis/slither_runner.py:97`) | Slither 执行原始结果私有类 | `status: StaticAnalysisStatus`, `error: str\|None`, `parsed: ParsedSlither\|None` | 生产: `_execute` (`slither_runner.py:248`) / 消费: `run_slither` (`slither_runner.py:337`) |
| `ParsedSlither` | dataclass (`static_analysis/findings_parser.py:58`) | Slither JSON 纯解析中间结构 | `findings: list[Finding]`, `n_results: int`, `n_filtered: int`, `n_unusable: int` | 生产: `parse_slither_json` (`findings_parser.py:66`) / 消费: `slither_runner.py:337` |
| `Selection` | dataclass (`context/budget.py:25`) | 预算行切分结果 | `included: tuple[tuple[int, int], ...]`, `text: str`, `est_tokens: int` | 生产: `select_lines` (`budget.py:39`) / 消费: `source.py:165` |
| `SourceContext` | dataclass (`context/source.py:61`) | 审计目标带行号上下文 | `file: str`, `text: str`, `total_lines: int`, `included_ranges: tuple[tuple[int, int], ...]`, `imports: tuple[str, ...]` | 生产: `build_context` (`source.py:200`) / 消费: `discoverer.py:59`, `grounding.py:82` |


## 2. Finding 的字段清单

| 字段名 | 类型 | 含义 | 来源 (LLM / Slither / 后处理计算) | Matcher (评测匹配) 是否使用 |
|---|---|---|---|---|
| `id` | `str` | 发现唯一标识 (12位 hex) | 后处理系统生成 (`schemas/finding.py:130`) | 否 |
| `category` | `str` | 漏洞分类 ID | Slither: 映射自规则 (`findings_parser.py:215`)；Discoverer: 绑定自分片参数 (`grounding.py:133`) | **是** (`eval/metrics/matcher.py:1`, `finding.py:143`) |
| `title` | `str` | 标题简述 (<=200字符) | Slither: 模板拼接 (`findings_parser.py:211`)；Discoverer: LLM 输出 (`grounding.py:134`) | 否 |
| `description` | `str` | 详细成因描述 | Slither: 检测器描述 (`findings_parser.py:212`)；Discoverer: LLM 输出 (`grounding.py:135`) | 否 |
| `location.file` | `str` | 相对审计根目录的文件路径 | Slither: 相对路径计算 (`findings_parser.py:202`)；Discoverer: `context.file` (`grounding.py:137`) | **是** (`eval/datasets/labels/schema.json:8`, `finding.py:143`) |
| `location.line_start` | `int` | 起始行号 (>=1) | Slither: AST 范围起始 (`findings_parser.py:205`)；Discoverer: LLM 输出 (`grounding.py:138`) | **是** (`eval/metrics/matcher.py:1` 行容差计算) |
| `location.line_end` | `int` | 结束行号 (>=1) | Slither: AST 范围结束 (`findings_parser.py:205`)；Discoverer: LLM 输出 (`grounding.py:139`) | **是** (`eval/metrics/matcher.py:1` 行容差计算) |
| `location.contract` | `str\|None` | 所属合约名 | Slither: AST contract 节点 (`findings_parser.py:203`)；Discoverer: LLM 输出经后处理校验清空 (`grounding.py:140`) | 否 |
| `location.function` | `str\|None` | 所属函数名 | Slither: enclosing function 节点 (`findings_parser.py:204`)；Discoverer: LLM 输出经后处理校验清空 (`grounding.py:141`) | **是** (`eval/metrics/matcher.py:1`, `eval/datasets/labels/schema.json:13`) |
| `evidence` | `list[str]` | 关键代码语句佐证 | Slither: node 语句提取 (`findings_parser.py:225`)；Discoverer: 后处理核实 quotes 格式化生成的引用 (`grounding.py:143`) | 否 |
| `severity` | `Severity` | 严重级别 | Slither: impact 转换 (`findings_parser.py:226`)；Discoverer: LLM 输出 (`grounding.py:144`) | 可选辅助 (`eval/datasets/labels/schema.json:18`) |
| `confidence` | `float` | 判定置信度 (0.0~1.0) | Slither: 固定映射 High=0.9/Med=0.6/Low=0.3 (`findings_parser.py:46`)；Discoverer: LLM 输出 (`grounding.py:145`) | 否 (去重排序使用) |
| `status` | `FindingStatus` | 验证状态 (默认 candidate) | 后处理系统初始赋默认值 (`schemas/finding.py:131`) | 否 |
| `source` | `FindingSource` | 发现产出来源 | 后处理系统写死 (`findings_parser.py:228` 写 SLITHER；`grounding.py:146` 写 DISCOVERER) | 否 |
| `rule_id` | `str\|None` | 规则编号 (Slither detector 名) | Slither: detector check 名称 (`findings_parser.py:229`)；Discoverer: 固定置 `None` (`grounding.py:147`) | 否 |

## 3. Discoverer 内部时序

1. **入口启动**: `agent/src/audit_agent/agents/discoverer.py:59` (`Discoverer.run`)
   - 输入: `context: SourceContext` -> 输出: `tuple[list[Finding], DiscoveryInfo]`
2. **加载 System Prompt**: `agent/src/audit_agent/agents/prompts/__init__.py:5` (`load_prompt`)
   - 输入: `prompt_version: str` (`"reentrancy.v1"`) -> 输出: `str` (`discoverer.py:47`)
3. **构造 User Message**: `agent/src/audit_agent/agents/discoverer.py:12` (`build_user_message`)
   - 输入: `context: SourceContext` -> 输出: `str` (拼接文件相对路径、截断范围提示、import 清单与行号代码块)
4. **组装初始消息**: `discoverer.py:61-64`
   - 输入: system/user 文本 -> 输出: `messages: list[dict[str, Any]]`
5. **进入 Agent 循环**: `agent/src/audit_agent/agents/base.py:202` (`Agent.run`)
   - 输入: `messages: list[dict[str, Any]]`, `schema: type[DiscovererOutput]` -> 输出: `AgentResult[DiscovererOutput]`
6. **调用 LLM 接口**: `agent/src/audit_agent/llm/client.py:108` (`LLMClient.chat`)
   - 输入: `messages: list[dict[str, Any]]`, `tools: list` (空列表，`discoverer.py:50`) -> 输出: `ChatResult` (`llm/types.py:35`)
7. **跳过 Tool Loop**: `agent/src/audit_agent/agents/base.py:288`
   - 判定: `reply.tool_calls` 为空，跳过工具执行分支，直接进入响应解析 (`base.py:303`)
8. **解析 DiscovererOutput**: `agent/src/audit_agent/llm/structured.py:160` (`parse_reply`)
   - 输入: `reply.content: str`, `schema: type[DiscovererOutput]` -> 输出: `ParseSuccess[DiscovererOutput]` (失败则触发格式回喂重试，`base.py:327`)
9. **后处理代码锚定**: `agent/src/audit_agent/agents/grounding.py:80` (`ground_findings`)
   - 输入: `output: DiscovererOutput`, `context: SourceContext`, `category: str` 等 -> 输出: `tuple[list[Finding], DiscoveryInfo]`

## 4. grounding.py 检查与修正动作清单

1. **行号逻辑有效性**: 检查 `line_start > line_end` (`grounding.py:103`)。
   - 失败行为: **丢弃** 该项，`DiscoveryInfo.dropped_invalid_range += 1`。
2. **行号可见性与越界检查**: 调用 `context.range_status(line_start, line_end)` (`grounding.py:107`)。
   - 失败行为: 超出文件总行数标记为 `OUTSIDE_FILE` 时 **丢弃**，`dropped_outside_file += 1`；落在预算省略区间标记为 `IN_OMITTED` 时 **丢弃**，`dropped_in_omitted += 1`。
3. **代码原句引用验证 (`_verify_quotes`)**: 剥离行号前缀并折叠空白后，在 `[line_start, line_end]` 源码拼接串中精确搜索子串 (`grounding.py:65`)，匹配成功则反解实际行号记录为 `L{actual_line}: {cleaned}`，保留最多 3 条。
   - 失败行为: 若该项所有 quotes 均未能匹配通过 (`not verified_evidence`)，**丢弃** 该项，`dropped_no_verified_quote += 1` (`grounding.py:118-120`)。
4. **函数名幻觉校验**: 用正则词边界 `\b{func_name}\b` 在全文件源码中查找 (`grounding.py:123`)。
   - 失败行为: 未找到时不丢弃发现，**降级修正** 为 `func_name = None`，`names_cleared += 1`。
5. **合约名幻觉校验**: 用正则词边界 `\b{contract_name}\b` 在全文件源码中查找 (`grounding.py:128`)。
   - 失败行为: 未找到时不丢弃发现，**降级修正** 为 `contract_name = None`，`names_cleared += 1`。
6. **组装 Finding 实体**: 由通过检查的 DiscovererItem 字段实例化 `Finding`，赋予 `source=DISCOVERER`、`rule_id=None` (`grounding.py:132-149`)。
7. **完全重复去重**: 基于 `finding.fingerprint()` (`category|file|func_or_Lstart`) 查重 (`grounding.py:151-162`)。
   - 碰撞行为: **丢弃** 重复项并记 `dropped_duplicate += 1`；新旧项仅保留 `confidence` 更高者（**不修改置信度数值**）。
8. **排序与配额截断**: 按 `(-confidence, line_start)` 排序，超出 `MAX_DISCOVERY_FINDINGS = 10` 的项全部 **丢弃**，记 `dropped_over_limit` (`grounding.py:164-171`)。

## 5. SourceContext 与 Finding/Report 的行号约定

- **基数约定**: 全链路统一从 **1** 开始 (1-based)。
  - `SourceContext`: 渲染时通过 `enumerate(lines, start=1)` 标记行号 (`context/source.py:143`)；预算包含区间定义为 1-based inclusive (`context/budget.py:27`)。
  - `Finding`: `CodeLocation` 强制约束 `line_start >= 1`，`line_end >= 1` (`schemas/finding.py:36-37`)。
  - `AuditReport`: `ContextInfo.included_ranges` 明确标明为 1-based, inclusive (`schemas/report.py:86`)。
- **转换责任人**: 
  - 源码渲染端 (`source.py:143`) 负责切分原始文本行并生成 1-based 文本行前缀 `f"{number:>{width}} | {line}"`。
  - LLM 提示词内阅读的是 1-based 行号，直接输出 1-based 行号，`grounding.py:32` 反解正则提取该行号，两端无基数转换责任方。
  - Slither 输出本即为 1-based 行号 (`findings_parser.py:205`)，直接取 `min`/`max`，无需基数转换。
- **偏移处理**: 
  - 全流程**无**针对 0-based 与 1-based 的 `+/- 1` 偏移换算。
  - 唯一的边界修正：`context/source.py:102-103` (`clamp`) 针对无尾部换行符的文件收敛 Slither 误报超出文件尾部 1 行的边界值 (`min(end, self.total_lines)`)。

## 6. FindingDraft 的来历与 DiscovererItem 差异

1. **Git 提交记录**:
   - 引入提交: `85de930` (2026-10-13 11:54:04 +0800), 提交信息: `feat(schemas): taxonomy loader and Finding/FindingDraft schemas`。
   - 原始设计意图: 注释定义为 `what the LLM must produce (small, validated contract)` (`schemas/finding.py:3, 49`)，原规划作为统一由 LLM 填充的模型，后续通过 `Finding.from_draft` 附加系统字段 (`schemas/finding.py:136`)。
2. **DiscovererItem 的引入**:
   - 引入提交: `6e144dd` (2026-11-03 07:24:04 +0800), 提交信息: `feat(agent): add reentrancy Discoverer with grounding and prompt v1`。
3. **两者核心差异**:
   - **结构扁平度**: `FindingDraft` 嵌套了 `location: CodeLocation` 对象 (`schemas/finding.py:54`)；`DiscovererItem` 展平为顶层字段 `line_start`, `line_end`, `function`, `contract` (`schemas/finding.py:77-80`)，且省去了 `file`。
   - **分类字段归属**: `FindingDraft` 要求 LLM 输出 `category` 并在前置校验器中匹配分类法 (`schemas/finding.py:51, 59-69`)；`DiscovererItem` 无 `category` 字段，改由特定分类 Discoverer 的系统后处理统一注入 (`grounding.py:133`)。
   - **佐证数据形态**: `FindingDraft` 为自由文本切片 `evidence: list[str]` (`schemas/finding.py:55`)；`DiscovererItem` 为未加工源码字面引用 `quotes: list[str]` (`schemas/finding.py:81`)，用于必须通过后处理核查。
   - **输入宽松容错**: `DiscovererItem` 为应对 LLM 不规范输出，增加了对字符型数字 (`_validate_line_numbers`)、非标大小写 severity (`_normalize_severity`) 及浮点字符串 confidence 裁剪的容错校验器 (`schemas/finding.py:85-118`)；`FindingDraft` 无上述宽容校验逻辑。
   - **外层信封包装**: `DiscovererItem` 配备外层信封 `DiscovererOutput` (`schemas/finding.py:121`)；`FindingDraft` 没有外层信封模型。

