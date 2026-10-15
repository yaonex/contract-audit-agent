# 所有 Schema 如何串起来

## 1. 全景图

```
                knowledge/taxonomy.yaml
                        │ 加载
                        ▼
                 Taxonomy / Category          (类别词典：合法取值清单)
                        │ 校验 category
                        ▼
 LLM 输出 ──►  FindingDraft ───────────┐
 (Discoverer)   · category              │ 系统补全 id/status/source
                · location ─► CodeLocation
                · severity (Enum)       ▼
                · confidence          Finding ◄────────────────┐
                                        │                      │
                                        │ finding_id (松散引用) │ 状态回写（待实现）
                                        ▼                      │
 Verifier 产出 ──────────────►  VerificationResult ────────────┘
                                 · status (FindingStatus)
                                 · attempts ─► PocAttempt[]
                                 │              · outcome (PocOutcome)
                                 │              · integrity_checks ─► IntegrityCheck[]
                                 └ rejection_basis (RejectionBasis)

 最终汇总 ──────────────────►  AuditReport
                                · meta ─► RunMeta
                                · cost ─► CostSummary
                                · findings ─► Finding[]
```

## 2. 一次审计中，数据如何流动

用一个具体例子走一遍：

1. **启动**：加载 `taxonomy.yaml`，得到合法类别清单。
2. **Discoverer 产出**：LLM 返回 JSON，经 `FindingDraft` 校验。校验器会把 `"Access Control"` 规范成 `access_control`，非法类别报错并回喂给 LLM 重试。
3. **系统补全**：`Finding.from_draft(draft, source)` 生成 `Finding`，系统赋 `id`、`status=candidate`。**LLM 无法伪造这些字段。**
4. **Verifier 验证**：针对每个 `Finding`，循环生成 PoC，每次执行记一条 `PocAttempt`（含失败类型、摘要、日志路径、完整性检查）。
5. **下结论**：构造 `VerificationResult`，此刻校验器守门，违反"PoC 失败≠不是漏洞"的结论构造不出来。
6. **状态回写**：把结论的 `status` 写回对应 `Finding.status`（**这个函数还没写，是已知缺口**，通过 `finding_id` 对应）。
7. **汇总**：所有 `Finding` 装进 `AuditReport`，附上 `RunMeta` 和 `CostSummary`，输出 JSON 与 Markdown。

## 3. 三类对象，三种"身份"

理解这个有助于你以后判断新字段该放哪：

| 身份 | 对象 | 谁创建 | 可信度 |
|---|---|---|---|
| **LLM 的声称** | `FindingDraft` | LLM | 不可信，必须校验 |
| **系统的记录** | `Finding`、`PocAttempt`、`CostSummary`、`RunMeta` | 代码 | 可信 |
| **带证据的裁决** | `VerificationResult` | Verifier，经校验器守门 | 约束保证自洽 |

这个划分就是"为什么拆 Draft 和 Finding"的深层原因：**把不可信的输入和可信的内部状态在类型上隔开**。

## 4. 三个"枚举 + 状态"的关系

容易混淆，单独列一下：

- `FindingStatus`（Finding 的生命周期：candidate → confirmed/unverified/rejected）；
- `PocOutcome`（**一次**执行的结果，6 种，是过程数据）；
- `RejectionBasis`（rejected 的依据，是理由）。

`PocOutcome` 是原料，`FindingStatus` 是对多次原料加工后的结论，二者不要混用。

## 5. 目前存在的松散点（主动指出）

1. **`finding_id` 是字符串引用，没有强校验**，可能出现 `VerificationResult` 指向不存在的 Finding。后面在 pipeline 的 state 里统一管理时需要保证一致。
2. **两份 status**（Finding 和 VerificationResult 各一份）要保证同步，1.4 补回写函数。
3. **`AuditReport` 里没有放 `VerificationResult`**，目前只有 Finding 的最终状态。如果报告要展示"PoC 源码和尝试过程"，需要加字段。这属于报告内容的取舍，等 1.4 随 Verifier 一起定。

---

## 面试官可能追问

- **你的 Pydantic 校验器和 Verifier 里的判断有什么区别？** 要点：校验器守不变量（数据自洽，谁构造都必须合法），Verifier 做决策；类比数据库 CHECK 约束与 Service 层逻辑，前者防止脏数据落地。
- **为什么 LLM 输出契约要单独一个类？** 要点：把不可信输入与内部可信状态在类型层面隔离，系统字段不让模型生成，降低校验失败率和被伪造的风险。
- **这些数据模型在状态流转里怎么配合？** 要点：Draft（声称）→ Finding（系统记录）→ PocAttempt（过程证据）→ VerificationResult（受约束的裁决）→ 回写 Finding → AuditReport 汇总。

---
