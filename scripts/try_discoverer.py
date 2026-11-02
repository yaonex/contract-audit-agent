"""Exploration script for Discoverer (reentrancy).

Runs Discoverer N times on candidate sample contracts, prints findings and
grounding statistics, and saves a JSON report to reports/.
"""
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from audit_agent.agents.discoverer import Discoverer
from audit_agent.config import get_settings
from audit_agent.context.source import build_context
from audit_agent.llm.client import LLMClient
from audit_agent.llm.cost import CostTracker
from audit_agent.llm.types import CacheMode

DEFAULT_SAMPLES = [
    "samples/vulnerable/Bank.sol",
    "samples/safe/Bank.sol",
    "samples/vulnerable/Vault.sol",
]


def run_exploration(
    sample_paths: list[Path],
    repeats: int,
    disable_cache: bool,
    category: str,
    prompt_version: str,
) -> dict:
    settings = get_settings()
    if disable_cache:
        settings = settings.model_copy(update={"cache_mode": CacheMode.OFF})

    print(f"=== Discoverer Exploration ({category}, {prompt_version}) ===")
    print(f"Model: {settings.llm_model} | Base URL: {settings.llm_base_url}")
    print(f"Cache mode: {settings.cache_mode.value} | Repeats: {repeats}\n")

    summary_data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "model": settings.llm_model,
        "category": category,
        "prompt_version": prompt_version,
        "cache_mode": settings.cache_mode.value,
        "repeats": repeats,
        "samples": {},
    }

    for path in sample_paths:
        resolved = Path(path).resolve()
        if not resolved.exists():
            print(f"Error: Sample file not found: {path}", file=sys.stderr)
            continue

        print("------------------------------------------------------------")
        print(f"Target: {path}")
        print("------------------------------------------------------------")

        ctx = build_context(resolved)
        sample_runs = []

        for i in range(1, repeats + 1):
            tracker = CostTracker.from_settings(settings)
            client = LLMClient(settings, budget=tracker)
            discoverer = Discoverer(category, prompt_version, client=client)

            try:
                findings, info = discoverer.run(ctx)
            except Exception as e:  # noqa: BLE001
                print(f"  [Run {i}/{repeats}] FAILED: {e}")
                sample_runs.append({"run": i, "error": str(e)})
                continue

            cost = tracker.snapshot()
            run_dict = {
                "run": i,
                "model_returned": info.model_returned,
                "kept": info.kept,
                "dropped_outside_file": info.dropped_outside_file,
                "dropped_in_omitted": info.dropped_in_omitted,
                "dropped_invalid_range": info.dropped_invalid_range,
                "dropped_no_verified_quote": info.dropped_no_verified_quote,
                "dropped_duplicate": info.dropped_duplicate,
                "dropped_over_limit": info.dropped_over_limit,
                "names_cleared": info.names_cleared,
                "retries": info.retries,
                "truncations": info.truncations,
                "duration_s": round(info.duration_s, 2),
                "prompt_tokens": cost.prompt_tokens,
                "completion_tokens": cost.completion_tokens,
                "findings": [
                    {
                        "title": f.title,
                        "function": f.location.function,
                        "contract": f.location.contract,
                        "line_start": f.location.line_start,
                        "line_end": f.location.line_end,
                        "severity": f.severity.value,
                        "confidence": f.confidence,
                        "evidence": f.evidence,
                    }
                    for f in findings
                ],
            }
            sample_runs.append(run_dict)

            print(
                f"  [Run {i}/{repeats}] Kept: {info.kept}/{info.model_returned} | "
                f"Drops: out={info.dropped_outside_file}, omit={info.dropped_in_omitted}, "
                f"inv={info.dropped_invalid_range}, no_quote={info.dropped_no_verified_quote}, "
                f"dup={info.dropped_duplicate}, over={info.dropped_over_limit} | "
                f"Cleared: {info.names_cleared} | Retries: {info.retries} | "
                f"Tokens: in={cost.prompt_tokens} out={cost.completion_tokens} | Time: {info.duration_s:.2f}s"
            )
            for idx, f in enumerate(findings, start=1):
                where = f"{f.location.contract or ''}.{f.location.function or ''}".strip(".") or "top-level"
                print(
                    f"    #{idx} [{f.severity.value.upper()}] conf={f.confidence:.2f} "
                    f"lines={f.location.line_start}-{f.location.line_end} ({where})"
                )
                for ev in f.evidence:
                    print(f"       evidence: {ev}")

        summary_data["samples"][str(path)] = sample_runs

    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"try_discoverer_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    report_file.write_text(json.dumps(summary_data, indent=2, ensure_ascii=False))
    print(f"\nRaw report written to: {report_file}")
    return summary_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Exploration script for Discoverer.")
    parser.add_argument("--samples", nargs="+", default=DEFAULT_SAMPLES)
    parser.add_argument("-n", "--repeats", type=int, default=3)
    parser.add_argument("--enable-cache", action="store_true", default=False)
    parser.add_argument("--category", default="reentrancy")
    parser.add_argument("--prompt-version", default="reentrancy.v1")

    args = parser.parse_args()
    sample_paths = [Path(p) for p in args.samples]
    run_exploration(
        sample_paths,
        repeats=args.repeats,
        disable_cache=not args.enable_cache,
        category=args.category,
        prompt_version=args.prompt_version,
    )


if __name__ == "__main__":
    main()

