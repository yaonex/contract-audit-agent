# Agent Design

已使用真实 LLM 配置（`deepseek-flash`，禁用缓存）执行探索脚本 `scripts/try_discoverer.py`。以下为运行过程与观测事实的完整汇报：

---

### 一、探索脚本运行输出（原样输出）

```
=== Discoverer Exploration (reentrancy, reentrancy.v1) ===
Model: deepseek-flash | Base URL: https://api.deepseek.com
Cache mode: off | Repeats: 3

------------------------------------------------------------
Target: samples/vulnerable/Bank.sol
------------------------------------------------------------
  [Run 1/3] Kept: 1/1 | Drops: out=0, omit=0, inv=0, no_quote=0, dup=0, over=0 | Cleared: 0 | Retries: 0 | Tokens: in=882 out=303 | Time: 3.14s
    #1 [CRITICAL] conf=0.99 lines=11-17 (Bank.withdraw)
       evidence: L14: (bool ok, ) = msg.sender.call{value: amount}("");
       evidence: L15: require(ok, "transfer failed");
       evidence: L16: balances[msg.sender] = 0;
  [Run 2/3] Kept: 1/1 | Drops: out=0, omit=0, inv=0, no_quote=0, dup=0, over=0 | Cleared: 0 | Retries: 0 | Tokens: in=882 out=291 | Time: 2.21s
    #1 [CRITICAL] conf=0.98 lines=11-17 (Bank.withdraw)
       evidence: L14: (bool ok, ) = msg.sender.call{value: amount}("");
       evidence: L16: balances[msg.sender] = 0;
       evidence: L12: uint256 amount = balances[msg.sender];
  [Run 3/3] Kept: 1/1 | Drops: out=0, omit=0, inv=0, no_quote=0, dup=0, over=0 | Cleared: 0 | Retries: 0 | Tokens: in=882 out=395 | Time: 2.85s
    #1 [CRITICAL] conf=0.99 lines=11-17 (Bank.withdraw)
       evidence: L12: uint256 amount = balances[msg.sender];
       evidence: L14: (bool ok, ) = msg.sender.call{value: amount}("");
       evidence: L16: balances[msg.sender] = 0;
------------------------------------------------------------
Target: samples/safe/Bank.sol
------------------------------------------------------------
  [Run 1/3] Kept: 0/0 | Drops: out=0, omit=0, inv=0, no_quote=0, dup=0, over=0 | Cleared: 0 | Retries: 0 | Tokens: in=882 out=41 | Time: 1.07s
  [Run 2/3] Kept: 0/0 | Drops: out=0, omit=0, inv=0, no_quote=0, dup=0, over=0 | Cleared: 0 | Retries: 0 | Tokens: in=882 out=35 | Time: 0.66s
  [Run 3/3] Kept: 0/0 | Drops: out=0, omit=0, inv=0, no_quote=0, dup=0, over=0 | Cleared: 0 | Retries: 0 | Tokens: in=882 out=80 | Time: 0.77s
------------------------------------------------------------
Target: samples/vulnerable/Vault.sol
------------------------------------------------------------
  [Run 1/3] Kept: 0/0 | Drops: out=0, omit=0, inv=0, no_quote=0, dup=0, over=0 | Cleared: 0 | Retries: 0 | Tokens: in=867 out=214 | Time: 1.93s
  [Run 2/3] Kept: 0/0 | Drops: out=0, omit=0, inv=0, no_quote=0, dup=0, over=0 | Cleared: 0 | Retries: 0 | Tokens: in=867 out=125 | Time: 1.31s
  [Run 3/3] Kept: 0/0 | Drops: out=0, omit=0, inv=0, no_quote=0, dup=0, over=0 | Cleared: 0 | Retries: 0 | Tokens: in=867 out=102 | Time: 1.41s

Raw report written to: reports/try_discoverer_20261102_230827.json
```

---

### 二、运行结果对比与分析（对齐第 8 节预期）

| 样本合约 | 重复次数 | 预期表现 | 实际观测结果 | 说明 |
|---|---|---|---|---|
| `vulnerable/Bank.sol` | 3 次 | 1 条重入，函数 `withdraw`，范围与 11–17 重叠 | 3 次均为 1 条（`conf=0.98~0.99`，`critical`，范围严格为 11–17，函数 `withdraw`） | 3 次均成功提取了 L14（外部调用）与 L16（状态更新）代码作为核对证据，0 丢弃 |
| `safe/Bank.sol` | 3 次 | 0 条（空列表） | 3 次均为 0 条（模型直接输出 `{"findings": []}`） | 准确识别出已按 Checks-Effects-Interactions 修复，无误报 |
| `vulnerable/Vault.sol` | 3 次 | 0 条（空列表） | 3 次均为 0 条（模型直接输出 `{"findings": []}`） | 包含未授权访问漏洞，但模型准确遵循指令，未报非重入漏洞 |

#### 性能与稳定性观测指标：
1. **格式与重试**：3 个样本 × 3 轮 = 9 次调用中，`retries=0`、`truncations=0`，模型一次性输出符合 `DiscovererOutput` schema 的合法 JSON。
2. **耗时与吞吐**：单次发现耗时 0.66s ~ 3.14s，输入 Token 稳定在 867 ~ 882（包含提示词与源码）。
3. **接地后处理表现**：在漏洞样本中，模型提取的代码引用全部通过了行号范围映射与清洗验证，证据行精准标出真实代码行号（如 `L14`、`L15`、`L16`）；函数名 `withdraw` 与合约名 `Bank` 均在源码中精确匹配（`names_cleared=0`）。
4. **原始报告落盘**：完整结构化 JSON 数据已保存于 `reports/try_discoverer_20261102_230827.json`。

---
