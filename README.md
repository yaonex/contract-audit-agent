# contract-audit-agent

Smart contract security audit agent: Slither triage -> context engineering ->
RAG-backed Discoverer agents -> PoC-based Verifier -> Judge -> MD/JSON report,
with a benchmark-driven evaluation harness.

- `agent/`  Python: LLM orchestration (LangGraph + custom tool loop), RAG, Slither, Foundry PoC
- `server/` Go: task queue, concurrent scheduler, SSE progress (Phase 2)
- `eval/`   Benchmarks, baselines, metrics, ablations
- `proto/`  gRPC contract shared by Python and Go

## Quick start (Phase 1)
```bash
cp .env.example .env
make setup
make audit FILE=samples/vulnerable/Reentrancy.sol
```

See `docs/` for architecture, agent design, and evaluation results.
