"""Manual admission check for a candidate model. NOT part of `make test`.

Usage (from repo root):  make verify-model MODEL=<model-name>

It calls the real API (small cost), prints what it observed, and, only if every HARD check
passes, prints the line to paste into SUPPORTED_MODELS. A human makes the final call.

Hard checks (must pass):
  1. basic call returns content and token usage          (cost budget depends on usage)
  2. tool calling works                                  (agent loop depends on it)
  3. tool-result round trip works                        (message format accepted on turn 2)
  4. JSON-only output validates as FindingDraft          (Pydantic validate + retry depends on it)
Observations (sampled, recorded into the entry):
  - parallel_tools:    did any sample return >1 tool_call in one turn?
  - reasoning_content: did any reply carry a reasoning field?

The probe bypasses the whitelist by injecting model_info into LLMClient. The placeholder
values in that injected info are never used for decisions.
"""
import argparse
import json
import re
import sys
from datetime import date

from pydantic import ValidationError

from audit_agent.config import get_settings
from audit_agent.llm.client import ChatResult, LLMClient, LLMError
from audit_agent.llm.models import SupportedModel
from audit_agent.observability.tracer import Tracer
from audit_agent.schemas.finding import FindingDraft
from audit_agent.schemas.taxonomy import load_taxonomy

ADD_TOOL = {
    "type": "function",
    "function": {
        "name": "add",
        "description": "Add two integers and return the sum.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
}
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for one city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class Probe:
    """Wraps the client and remembers every reply (to observe reasoning_content)."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.results: list[ChatResult] = []

    def chat(self, messages, **kwargs) -> ChatResult:
        result = self.client.chat(messages, **kwargs)
        self.results.append(result)
        return result


def check_basic(p: Probe) -> tuple[bool, str]:
    r = p.chat([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=50)
    ok = bool(r.content) and r.usage.prompt_tokens > 0 and r.usage.completion_tokens > 0
    return ok, f"content={r.content!r} usage=({r.usage.prompt_tokens}, {r.usage.completion_tokens})"


def _first_tool_turn(p: Probe):
    messages = [{"role": "user", "content": "What is 17 + 25? You must use the add tool."}]
    return messages, p.chat(messages, tools=[ADD_TOOL])


def check_tool_call(p: Probe) -> tuple[bool, str]:
    _, r = _first_tool_turn(p)
    if not r.tool_calls:
        return False, f"no tool_calls returned (content={r.content!r})"
    tc = r.tool_calls[0]
    try:
        args = json.loads(tc.arguments)
        ok = tc.name == "add" and isinstance(args.get("a"), int) and isinstance(args.get("b"), int)
    except json.JSONDecodeError:
        return False, f"arguments are not valid JSON: {tc.arguments!r}"
    return ok, f"name={tc.name} arguments={tc.arguments}"


def check_tool_roundtrip(p: Probe) -> tuple[bool, str]:
    messages, r = _first_tool_turn(p)
    if not r.tool_calls:
        return False, "no tool call to round-trip"
    tc = r.tool_calls[0]
    # reasoning_content is deliberately NOT sent back (see SupportedModel.reasoning_content).
    messages += [
        {
            "role": "assistant",
            "content": r.content,
            "tool_calls": [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }],
        },
        {"role": "tool", "tool_call_id": tc.id, "content": "42"},
    ]
    final = p.chat(messages, tools=[ADD_TOOL])
    return "42" in (final.content or ""), f"final content={final.content!r}"


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def check_json(p: Probe, trials: int = 3) -> tuple[bool, str]:
    ids = ", ".join(sorted(load_taxonomy().ids))
    prompt = (
        "You are a smart contract auditor. Report the vulnerability in the code below as ONE "
        "JSON object and nothing else.\n"
        f"Fields: category (one of: {ids}), title, description, "
        "location {file, line_start, line_end, function}, evidence (list of strings), "
        "severity (critical|high|medium|low|info), confidence (number 0..1).\n"
        "File Bank.sol, the function spans lines 10-12:\n"
        '```solidity\nfunction withdraw() external {\n'
        '  (bool ok,) = msg.sender.call{value: balances[msg.sender]}("");\n'
        "  require(ok); balances[msg.sender] = 0;\n}\n```"
    )
    passed, errors = 0, []
    for _ in range(trials):
        # temperature > 0 on purpose: at 0 the samples would be near-identical and
        # tell us nothing about how often the model breaks the format.
        r = p.chat([{"role": "user", "content": prompt}], temperature=0.7)
        try:
            FindingDraft.model_validate_json(_strip_fence(r.content or ""))
            passed += 1
        except ValidationError as exc:
            errors.append(str(exc).splitlines()[0][:120])
    detail = f"{passed}/{trials} valid" + (f"; first error: {errors[0]}" if errors else "")
    return passed == trials, detail


def observe_parallel(p: Probe, trials: int = 3) -> bool:
    messages = [{
        "role": "user",
        "content": "Get the weather for Tokyo and for Paris. Call the tool for both cities "
                   "in the same turn.",
    }]
    return any(
        len(p.chat(messages, tools=[WEATHER_TOOL], temperature=0.7).tool_calls) > 1
        for _ in range(trials)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Admission check for a candidate model.")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    settings = get_settings().model_copy(update={"llm_model": args.model})
    if not settings.llm_api_key.get_secret_value():
        print("AUDIT_LLM_API_KEY is empty; set it in .env first.")
        return 2

    run_id = "verify-" + re.sub(r"[^A-Za-z0-9_-]", "_", args.model)
    tracer = Tracer(run_id, settings.trace_dir)
    placeholder = SupportedModel(
        name=args.model, parallel_tools=False, reasoning_content=False, verified_on=date.today()
    )
    probe = Probe(LLMClient(settings, tracer=tracer, model_info=placeholder))

    print(f"model={args.model}  base_url={settings.llm_base_url}\n")
    hard = [
        ("basic call + usage", check_basic),
        ("tool calling", check_tool_call),
        ("tool-result round trip", check_tool_roundtrip),
        ("JSON -> FindingDraft", check_json),
    ]
    all_ok = True
    for name, fn in hard:
        try:
            ok, detail = fn(probe)
        except LLMError as exc:
            ok, detail = False, f"API error: {exc}"
        all_ok &= ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}\n       {detail}")

    try:
        parallel = observe_parallel(probe)
    except LLMError as exc:
        print(f"parallel probe failed: {exc}")
        parallel = False 
    reasoning = any(r.reasoning_content for r in probe.results)
    print(f"\n[observed] parallel_tools={parallel}  reasoning_content={reasoning}")
    print(f"[trace]    full requests/responses: {settings.trace_dir / run_id}/blobs/")

    if not all_ok:
        print("\nRESULT: at least one hard check failed. Do NOT add this model.")
        return 1
    print("\nRESULT: all hard checks passed. After the manual steps, paste into SUPPORTED_MODELS:\n")
    print(
        f'    SupportedModel(name="{args.model}", parallel_tools={parallel}, '
        f"reasoning_content={reasoning}, verified_on=date({date.today():%Y, %-m, %-d})),"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())