# Architecture

### 0. 数据流转全景架构图

```
                       输入: Bank.sol (原始合约路径)
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   【分支 A: 静态分析】                       【分支 B: LLM 发现者】
   slither_runner.py                         context/source.py
   (运行 Slither 子进程)                       (加 1-based 行号，切片预算)
              │                                         │
              ▼                                         ▼
   findings_parser.py                        SourceContext (纯文本带行号)
   (纯函数解析 JSON)                                     │
              │                                         ▼
              │                              agents/discoverer.py
              │                              (注入专项提示词, 调 LLM)
              │                                         │
              │                                         ▼
              │                              DiscovererOutput (扁平 JSON)
              │                                         │
              │                                         ▼
              │                              agents/grounding.py
              │                              (代码引用对账、行号核实、去重)
              │                                         │
              ▼                                         ▼
    Finding[] (source=slither)              Finding[] (source=discoverer)
              │                                         │
              └────────────────────┬────────────────────┘
                                   ▼
              【当前已实现代码终点 / 汇聚池: Finding[] (status=CANDIDATE)】
                                   │
                                   ▼ (待实现阶段)
              ┌─────────────────────────────────────────┐
              │  3. 验证者 Verifier (动态攻防验证)       │
              │     · LLM 生成 Foundry 测试用例         │
              │     · Docker 沙箱运行 `forge test`      │
              │     · 产出 PocAttempt[]                 │
              │     · 构造受约束的 VerificationResult   │
              └────────────────────┬────────────────────┘
                                   │
                                   ▼ (待实现阶段)
              ┌─────────────────────────────────────────┐
              │  4. 仲裁者 Judge (多源裁决与去重)       │
              │     · 交叉比对 Slither 与 LLM 结果      │
              │     · 状态回写: confirmed / unverified  │
              │     · 评定全局最终置信度与严重度        │
              └────────────────────┬────────────────────┘
                                   │
                                   ▼ (待实现阶段)
              ┌─────────────────────────────────────────┐
              │  5. 报告生成 Report (报告装配与导出)    │
              │     · 装配 AuditReport 顶层实体         │
              │     · 渲染 Markdown，导出 JSON          │
              └─────────────────────────────────────────┘
```

---

### 详细步骤与数据跳跃详解

---

#### 阶段一：目标接入与上下文准备（已实现）

* **输入**：用户传入的 `.sol` 文件路径（`pathlib.Path`）。
* **核心类与代码**：`agent/src/audit_agent/context/source.py` 与 `budget.py`。
* **数据跳跃 1**：
  * **调用**：`build_context(path, max_tokens=20_000)`。
  * **动作**：
    1. 读文件、处理 UTF-8 BOM 编码。
    2. 按换行符切分，通过 `enumerate(lines, start=1)` 为每行强制加上 ` 14 | ` 格式的前缀。
    3. 如果文件估算 Token 超过预算（`context_max_tokens`），`budget.py` 启动“保留头部（声明/变量）+ 保留尾部（核心逻辑）+ 中间插入 `[... omitted ...]` 标记”的安全裁剪。
  * **产出实体**：`SourceContext`（dataclass）
    * 关键字段：`file: str`（相对路径）、`text: str`（带行号的文本）、`total_lines: int`、`included_ranges: tuple[tuple[int, int]]`（展示给模型的区间）。

---

#### 阶段二：双路独立漏洞挖掘（已实现）

##### 【分支 A：Slither 静态分析流水线】
* **输入**：合约文件绝对路径。
* **核心类与代码**：`static_analysis/slither_runner.py` 与 `findings_parser.py`。
* **数据跳跃 2-A1**：
  * **动作**：`slither_runner.run_slither` 启动子进程，运行 `slither Bank.sol --json slither.json --fail-none`。若遇编译失败、超时或环境缺 solc，降级捕获并不抛异常，记录 `StaticAnalysisInfo.status`。
* **数据跳跃 2-A2**：
  * **动作**：`findings_parser.parse_slither_json` 纯函数解析原始 JSON。
  * 规则映射：从 `Taxonomy` 分类表里把 Slither 的 detector check（如 `reentrancy-eth`）映射为统一类别（`reentrancy`）；把 `impact` 映射为 `Severity` 枚举。
* **产出实体**：`StaticAnalysisResult`（包含 `info: StaticAnalysisInfo`, `findings: list[Finding]`）
  * 产出的每个 Finding 初始状态为：`source = FindingSource.SLITHER`, `status = FindingStatus.CANDIDATE`。

##### 【分支 B：Discoverer 语义分析与质检流水线】
* **输入**：阶段一产生的 `SourceContext`。
* **核心类与代码**：`agents/discoverer.py`、`llm/client.py`、`agents/grounding.py`。
* **数据跳跃 2-B1（组装 Prompt）**：
  * 系统提示词加载版本化的 `agents/prompts/discoverer/reentrancy.v1.md`。
  * 用户提示词嵌入带有 `14 | ` 前缀的代码，并附带视野边界告知（“第 X-Y 行已被裁剪”、“外部 import 未包含”）。
* **数据跳跃 2-B2（模型交互与格式守门）**：
  * 调用 `Agent.run(messages, schema=DiscovererOutput)`。
  * 模型返回 JSON。`llm/structured.py` 用 Pydantic 校验 `DiscovererOutput`。如果格式错，自动将错误提示回喂给大模型重试（最多 2 次）。
  * 获得中间实体：`DiscovererOutput`（包含 `findings: list[DiscovererItem]`）。
* **数据跳跃 2-B3（静态质检代码锚定 Grounding）**：
  * 进入纯函数 `grounding.ground_findings(output, context, category="reentrancy")`。
  * **质检 1（行号越界）**：`line_start > line_end` 或落在 `omitted` 区间，**直接丢弃**。
  * **质检 2（原文引用对账）**：核对大模型的 `quotes`。引用的代码必须在 `[line_start, line_end]` 源码区间内能搜到精确子串。**如果一条都搜不到（凭空编造），直接丢弃该漏洞**。
  * **质检 3（标识符降级）**：如果大模型猜的 `function`、`contract` 名字在源码不存在，不丢弃漏洞，仅清空字段（赋 `None`）。
  * **质检 4（去重截断）**：相同行/函数的重复发现保留置信度最高者，全文件最多保留 10 条。
* **产出实体**：`tuple[list[Finding], DiscoveryInfo]`
  * 转换出来的每个 Finding 标记为：`source = FindingSource.DISCOVERER`, `status = FindingStatus.CANDIDATE`。

---

#### 阶段三：EVM 动态攻防验证（待实现）

* **输入**：上述两路汇聚的候选列表 `list[Finding]`（当前为 `status=CANDIDATE`）。
* **核心契约设计（已在 `schemas/poc.py` 锁定）**：
* **数据跳跃 3**：
  1. **构建攻击用例**：Verifier Agent 读取漏洞描述与相关代码，生成一个 Foundry 测试合约（Solidity 代码）。
  2. **执行沙箱测试**：调用沙箱工具（`tools/forge_runner.py`），在无网络的 Docker 容器中执行 `forge test`。
  3. **记录过程尝试**：每一次执行生成一个不可伪造的记录 `PocAttempt`：
     * `outcome`: `compile_error` | `runtime_error` | `assertion_failed` | `passed` | `timeout` 等。
     * `integrity_checks`: 反作弊检查（如：是否真正 import 了原始合约，而非自己伪造了一个假合约）。
  4. **下达裁决结论**：根据多次尝试最终组装为 `VerificationResult`：
     * **`CONFIRMED`（确认有漏洞）**：必须至少有一次 `PocAttempt` 的结果是 `PASSED`，且**必须通过反作弊完整性检查**。
     * **`UNVERIFIED`（未证实）**：PoC 编译失败、执行超时或没写出来（**核心原则：未证实 ≠ 不是漏洞**，不能当假阳性丢掉）。
     * **`REJECTED`（确认为虚警）**：PoC 成功编译并运行完毕，但断言失败（`assertion_failed`），证明假设的攻击路径在逻辑上行不通。

---

#### 阶段四：跨源仲裁与去重（待实现）

* **输入**：多路的原始 `Finding` 列表与 Verifier 的 `VerificationResult` 列表。
* **核心类与设计**：`agents/judge.py`。
* **数据跳跃 4**：
  1. **状态回写**：依据 `finding_id`，将 `VerificationResult.status` 回写覆盖到 `Finding.status`（从 `CANDIDATE` 变为 `CONFIRMED` / `UNVERIFIED` / `REJECTED`）。
  2. **多源对齐与去重（Matcher）**：
     * 如果 Slither 报了 `Bank.sol:withdraw` 重入，LLM 也报了 `Bank.sol:withdraw` 重入：
     * Judge 将两条 Finding 归一合并，保留更丰富的分析证据，并大幅**提升综合置信度**（双重印证）。
  3. **分歧调解**：
     * 如果 Slither 报警但 LLM 认为安全，或者 PoC 结果为 `REJECTED`，对假阳性进行降噪剔除。

---

#### 阶段五：报告组装与格式化输出（待实现）

* **输入**：裁决后的最终 `list[Finding]`、各阶段统计指标。
* **核心类与设计**：`schemas/report.py`（模型已就绪）、`report/markdown.py`、`report/json_export.py`。
* **数据跳跃 5**：
  * **组装顶层大对象 `AuditReport`**：
    * `meta: RunMeta`：记录运行时间、模型名（如 `deepseek-flash`）、提示词版本（`reentrancy.v1`）、Slither/solc 真实编译器版本。
    * `cost: CostSummary`：从 `CostTracker` 快照获取总 token 消耗、真实美元成本、LLM 调用次数。
    * `static_analysis: StaticAnalysisInfo`：记录 Slither 检出数、过滤数、执行时长。
    * `discovery: DiscoveryInfo`：记录 LLM 检出数、地基对账丢弃数（越界/无引用/超限）、重试次数。
    * `context: ContextInfo`：记录代码审计覆盖率（是否发生裁剪、丢弃了哪些行）。
    * `findings: list[Finding]`：最终确立的漏洞清单（带确认状态、严重度、修复建议与代码证据）。
  * **最终导出**：
    * 借助 Jinja2 模板 `report.md.j2` 渲染生成面向人类的审计报告 `Audit_Report.md`。
    * 序列化导出标准化机器可读的 `report.json`。

---

### 核心亮点小结

1. **类型隔离设计（Draft vs System Finding）**：
   不可信的模型输出（`DiscovererItem` / `DiscovererOutput`）永远在最外层被 Pydantic + Grounding 严格净化；模型永远接触不到系统的内部主键、置信状态字段（`id`, `status`, `source`），防范状态越权。
2. **零成本文本质检 + 高成本 EVM 沙箱打靶（双漏斗防御）**：
   `grounding.py` 用毫秒级确定性正则和字符串检索干掉 100% 的**低级文本幻觉**；`Verifier` 启动真实 Docker 容器与 Foundry 字节码执行干掉**深层业务逻辑误报**，实现了成本与精度的最优平衡。
3. **“PoC 失败 ≠ 不是漏洞”的状态机不变量**：
   验证失败只降级为 `UNVERIFIED`（人工重点复核），绝不轻易判为 `REJECTED`，彻底规避了因为 Agent 编写 PoC 能力不足而将真实致命漏洞当成误报丢弃的安全灾难。