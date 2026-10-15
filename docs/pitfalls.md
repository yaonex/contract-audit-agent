# Pitfalls Log
Record real problems hit during development (LangGraph reducers, checkpointer
serialization, model tool-call quirks, sandbox issues) with cause and fix.

## LLM 篡改了合约怎么办？

这是一个真实存在的风险，也是这类系统典型的"作弊"方式：

LLM 可能把原合约复制一份并"改"成有漏洞的版本放进 PoC，然后攻击这个改过的副本，测试通过，但原合约根本没问题；
或者 PoC 里直接用 vm.store/vm.deal 等作弊码把状态设成攻击后的样子，断言自然成立，什么也没证明。

这是 Verifier 设计里必须防的，防法是**确定性的机制，不能靠 prompt 叫 LLM "不要作弊"**：

1. 合约源码只读挂载：沙箱里原合约文件设为只读，PoC 只能 import，不能修改；
2. 校验 PoC 导入的是原文件：检查 PoC 的 import 路径指向原合约，且没有内联重新定义同名合约；
3. 限制作弊码：扫描 PoC 源码，禁止对被测合约的存储用 vm.store 之类直接篡改状态的调用（部分作弊码如 vm.deal 给攻击者发钱是正常的，需要白名单）；
4. 对照实验（强烈建议）：同一个 PoC，在"打过补丁的版本"上应该失败。如果有补丁版就能证明 PoC 测的是漏洞本身，不是别的。MVP 可以不做，但这是最强的验证手段，值得在面试里提。

## 项目目标时什么？

审计工具永远不能证明"完全没有漏洞"，所以目标不是给合约发安全证书。更准确的目标是：

在可控成本下尽可能多地找出真实漏洞（高 Recall），并诚实标注每个发现的证据强度（confirmed / unverified），同时用 PoC 把误报压下去（高 Precision）。

这也是面试时一个很好的表述："我的系统不输出'安全'结论，只输出'有证据的发现'和'未验证的怀疑'。"它解释了为什么评测看 Precision / Recall，而不是"判对没有"。






