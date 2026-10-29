import copy
import json

import pytest
from pydantic import BaseModel

from audit_agent.agents.base import Agent, StopReason
from audit_agent.config import Settings
from audit_agent.llm.cache import ReplayMiss
from audit_agent.llm.client import LLMError
from audit_agent.llm.cost import BudgetExceeded
from audit_agent.llm.structured import OutputTruncated, StructuredOutputError
from audit_agent.llm.types import ChatResult, ToolCall
from audit_agent.observability.tracer import Tracer, read_events
from audit_agent.tools.registry import Tool, ToolError, ToolRegistry, ToolResult

USER = [{"role": "user", "content": "go"}]


class AddArgs(BaseModel):
    a: int
    b: int


class PathArgs(BaseModel):
    path: str


class NoArgs(BaseModel):
    pass


class Point(BaseModel):
    x: int
    y: int


# ---- fakes ----------------------------------------------------------------------------

class ScriptedClient:
    """Returns scripted replies in order; an Exception in the script is raised.
    Messages are deep-copied: the loop keeps mutating its own list after each call."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, messages, *, tools=None, temperature=0.0, max_tokens=None, **_):
        self.calls.append(
            {"messages": copy.deepcopy(messages), "tools": tools, "max_tokens": max_tokens}
        )
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def call(call_id, name, args) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args if isinstance(args, str) else json.dumps(args))


def tools_reply(*calls, content=None, finish="tool_calls", reasoning=None) -> ChatResult:
    return ChatResult(content=content, tool_calls=list(calls), finish_reason=finish,
                      reasoning_content=reasoning)


def final(text, reasoning=None) -> ChatResult:
    return ChatResult(content=text, finish_reason="stop", reasoning_content=reasoning)


def _boom(p):
    raise RuntimeError("secret /tmp/x")


def _read_file(p):
    if p.path == "missing.sol":
        raise ToolError("file not found: missing.sol")
    return ToolResult(f"contents of {p.path}", artifact={"path": p.path})


def crash_tool() -> Tool:
    return Tool(name="crash", description="Always crashes.", params=NoArgs, func=_boom)


def make_registry(*extra: Tool) -> ToolRegistry:
    return ToolRegistry([
        Tool(name="add", description="Add two integers.", params=AddArgs,
             func=lambda p: str(p.a + p.b)),
        Tool(name="read_file", description="Read a file.", params=PathArgs, func=_read_file),
        *extra,
    ])


def make_agent(client, registry=None, **kw) -> Agent:
    return Agent(client, registry or make_registry(), **kw)


# ---- main paths -----------------------------------------------------------------------

def test_final_answer_without_tools():
    client = ScriptedClient(final("all good"))
    r = make_agent(client).run(USER)
    assert (r.stop_reason, r.content, r.rounds, r.model_calls) == (StopReason.FINAL, "all good", 0, 1)
    assert {t["function"]["name"] for t in client.calls[0]["tools"]} == {"add", "read_file"}
    assert r.messages[-1] == {"role": "assistant", "content": "all good"}


def test_tool_round_trip():
    client = ScriptedClient(tools_reply(call("c1", "add", {"a": 17, "b": 25})), final("42"))
    r = make_agent(client).run(USER)
    assert (r.content, r.rounds, r.model_calls) == ("42", 1, 2)
    second = client.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "tool"]
    assert second[1]["tool_calls"][0]["id"] == "c1"
    assert second[2] == {"role": "tool", "tool_call_id": "c1", "content": "42"}
    assert [(t.name, t.error_kind) for t in r.tool_calls] == [("add", None)]


def test_several_calls_in_one_reply_run_in_order_and_are_all_answered():
    client = ScriptedClient(
        tools_reply(call("c1", "add", {"a": 1, "b": 2}), call("c2", "add", {"a": 3, "b": 4})),
        final("done"),
    )
    r = make_agent(client).run(USER)
    tool_msgs = [m for m in client.calls[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
    assert [m["content"] for m in tool_msgs] == ["3", "7"]
    assert r.rounds == 1                              # one model reply = one round


ERROR_CASES = [
    ("nope", {}, "unknown_tool"),
    ("add", "{broken", "bad_json"),
    ("add", {"a": "x"}, "bad_arguments"),
    ("read_file", {"path": "missing.sol"}, "tool_failed"),
    ("crash", {}, "tool_crashed"),
]


@pytest.mark.parametrize(("name", "args", "kind"), ERROR_CASES)
def test_every_failure_is_answered_and_the_agent_continues(name, args, kind):
    client = ScriptedClient(tools_reply(call("c1", name, args)), final("recovered"))
    r = make_agent(client, make_registry(crash_tool())).run(USER)
    assert r.content == "recovered"
    tool_msg = client.calls[1]["messages"][-1]
    assert tool_msg["tool_call_id"] == "c1" and tool_msg["content"].startswith("error:")
    assert r.tool_calls[0].error_kind == kind


def test_model_sees_which_field_was_wrong():
    client = ScriptedClient(tools_reply(call("c1", "add", {"a": "x", "b": 1})), final("ok"))
    make_agent(client).run(USER)
    assert "a:" in client.calls[1]["messages"][-1]["content"]


def test_crash_does_not_leak_internal_details_to_the_model():
    client = ScriptedClient(tools_reply(call("c1", "crash", {})), final("ok"))
    make_agent(client, make_registry(crash_tool())).run(USER)
    content = client.calls[1]["messages"][-1]["content"]
    assert "secret" not in content and "RuntimeError" in content


# ---- limits ---------------------------------------------------------------------------

def test_max_rounds_is_a_hard_stop_that_does_not_run_the_extra_call():
    ran: list[int] = []

    def counting(p):
        ran.append(p.a)
        return "ok"

    reg = ToolRegistry([Tool(name="count", description="d", params=AddArgs, func=counting)])
    client = ScriptedClient(*[tools_reply(call(f"c{i}", "count", {"a": i, "b": 0})) for i in range(3)])
    r = make_agent(client, reg, max_tool_rounds=2).run(USER)
    assert (r.stop_reason, r.rounds, r.model_calls) == (StopReason.MAX_ROUNDS, 2, 3)
    assert ran == [0, 1]                              # the third request was NOT executed
    assert r.messages[-1]["role"] == "tool"           # transcript ends in a valid state
    assert r.value is None


def test_timeout_is_checked_before_the_first_call():
    client = ScriptedClient()
    r = make_agent(client, timeout_s=0, clock=FakeClock()).run(USER)
    assert (r.stop_reason, r.model_calls) == (StopReason.TIMEOUT, 0)
    assert client.calls == []


def test_timeout_during_a_round_skips_the_rest_but_answers_every_call():
    clock = FakeClock()

    def slow(p):
        clock.now += 100
        return "slow done"

    reg = ToolRegistry([Tool(name="slow", description="d", params=NoArgs, func=slow)])
    client = ScriptedClient(tools_reply(call("c1", "slow", {}), call("c2", "slow", {})))
    r = make_agent(client, reg, timeout_s=50, clock=clock).run(USER)
    assert (r.stop_reason, r.model_calls) == (StopReason.TIMEOUT, 1)
    tool_msgs = [m for m in r.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
    assert "not executed" in tool_msgs[1]["content"]
    assert [t.error_kind for t in r.tool_calls] == [None, "skipped"]


def test_truncated_reply_runs_no_tool_keeps_messages_and_doubles_the_budget():
    ran: list[int] = []
    reg = ToolRegistry([Tool(name="count", description="d", params=AddArgs,
                             func=lambda p: ran.append(1) or "ok")])
    client = ScriptedClient(
        tools_reply(call("c1", "count", {"a": 1, "b": 0}), finish="length"), final("ok")
    )
    r = make_agent(client, reg, max_tokens=100, max_tokens_cap=1000).run(USER)
    assert [c["max_tokens"] for c in client.calls] == [100, 200]
    assert client.calls[1]["messages"] == USER
    assert (ran, r.rounds, r.content) == ([], 0, "ok")


def test_truncation_at_the_cap_raises():
    client = ScriptedClient(final("partial").model_copy(update={"finish_reason": "length"}))
    with pytest.raises(OutputTruncated):
        make_agent(client, max_tokens=100, max_tokens_cap=100).run(USER)


# ---- history hygiene ------------------------------------------------------------------

def test_unparseable_arguments_are_not_replayed_into_history():
    client = ScriptedClient(tools_reply(call("c1", "add", "{broken")), final("ok"))
    make_agent(client).run(USER)
    second = client.calls[1]["messages"]
    assert second[1]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert "not a valid JSON" in second[2]["content"]


def test_reasoning_content_is_never_sent_back():
    client = ScriptedClient(
        tools_reply(call("c1", "add", {"a": 1, "b": 1}), reasoning="SECRET-THOUGHTS"), final("ok")
    )
    make_agent(client).run(USER)
    assert "SECRET-THOUGHTS" not in str(client.calls[1]["messages"])


def test_long_tool_output_is_clipped_and_the_artifact_is_collected():
    big = Tool(name="big", description="d", params=NoArgs,
               func=lambda p: ToolResult("x" * 20000, artifact={"k": 1}))
    client = ScriptedClient(tools_reply(call("c1", "big", {})), final("ok"))
    r = make_agent(client, ToolRegistry([big]), tool_output_max_chars=1000).run(USER)
    content = client.calls[1]["messages"][-1]["content"]
    assert len(content) < 1200 and "truncated" in content
    assert [(a.tool, a.value) for a in r.artifacts] == [("big", {"k": 1})]
    assert r.tool_calls[0].output_chars == 20000


def test_callers_messages_are_not_mutated():
    msgs = [{"role": "user", "content": "go"}]
    make_agent(ScriptedClient(tools_reply(call("c1", "add", {"a": 1, "b": 1})), final("ok"))).run(msgs)
    assert msgs == [{"role": "user", "content": "go"}]


# ---- structured final answer ----------------------------------------------------------

def test_structured_final_answer_is_validated():
    r = make_agent(ScriptedClient(final('{"x": 1, "y": 2}'))).run(USER, schema=Point)
    assert (r.stop_reason, r.value) == (StopReason.FINAL, Point(x=1, y=2))


def test_invalid_final_answer_continues_the_same_conversation_without_using_tool_rounds():
    client = ScriptedClient(
        tools_reply(call("c1", "add", {"a": 1, "b": 1})), final("not json"), final('{"x": 1, "y": 2}')
    )
    r = make_agent(client, max_tool_rounds=1).run(USER, schema=Point)
    assert (r.value, r.rounds, r.model_calls) == (Point(x=1, y=2), 1, 3)
    third = client.calls[2]["messages"]
    assert [m["role"] for m in third] == ["user", "assistant", "tool", "assistant", "user"]
    assert third[3]["content"] == "not json" and "Problems" in third[4]["content"]


def test_format_retries_are_limited():
    client = ScriptedClient(final("bad"), final("bad"))
    with pytest.raises(StructuredOutputError) as exc:
        make_agent(client, max_format_retries=1).run(USER, schema=Point)
    assert len(client.calls) == 2 and exc.value.last_content == "bad"


def test_tool_calls_take_precedence_over_answer_looking_content():
    client = ScriptedClient(
        tools_reply(call("c1", "add", {"a": 1, "b": 1}), content='{"x": 9, "y": 9}'),
        final('{"x": 1, "y": 2}'),
    )
    assert make_agent(client).run(USER, schema=Point).value == Point(x=1, y=2)


# ---- infrastructure errors ------------------------------------------------------------

@pytest.mark.parametrize(
    "err", [LLMError("down"), BudgetExceeded("token", 10, 5), ReplayMiss("k" * 20)]
)
def test_infrastructure_errors_propagate_and_the_transcript_is_still_written(tmp_path, err):
    tracer = Tracer("r1", tmp_path)
    client = ScriptedClient(tools_reply(call("c1", "add", {"a": 1, "b": 2})), err)
    with pytest.raises(type(err)):
        make_agent(client, tracer=tracer).run(USER)
    agent_span = next(e for e in read_events(tracer.path) if e.kind == "agent")
    assert agent_span.status == "error"
    transcript = (tmp_path / agent_span.attrs["transcript_blob"]).read_text(encoding="utf-8")
    assert '"tool_call_id": "c1"' in transcript


def test_infrastructure_error_inside_a_tool_is_not_turned_into_a_tool_message():
    def broke(p):
        raise BudgetExceeded("token", 10, 5)

    reg = ToolRegistry([Tool(name="b", description="d", params=NoArgs, func=broke)])
    with pytest.raises(BudgetExceeded):
        make_agent(ScriptedClient(tools_reply(call("c1", "b", {}))), reg).run(USER)


# ---- tracing --------------------------------------------------------------------------

def test_trace_has_an_agent_span_with_tool_children(tmp_path):
    tracer = Tracer("r1", tmp_path)
    client = ScriptedClient(
        tools_reply(call("c1", "add", {"a": 1, "b": 2}), call("c2", "crash", {}), call("c3", "nope", {})),
        final("ok"),
    )
    make_agent(client, make_registry(crash_tool()), tracer=tracer, name="disc").run(USER)
    events = read_events(tracer.path)
    agent = next(e for e in events if e.kind == "agent")
    tools = {e.name: e for e in events if e.kind == "tool"}
    assert agent.name == "disc" and agent.attrs["stop_reason"] == "final"
    assert (agent.attrs["rounds"], agent.attrs["model_calls"]) == (1, 2)
    assert all(t.parent_id == agent.span_id for t in tools.values())
    assert tools["add"].status == "ok" and "error_kind" not in tools["add"].attrs
    assert tools["crash"].status == "error" and "RuntimeError" in (tools["crash"].error or "")
    assert tools["nope"].status == "ok" and tools["nope"].attrs["error_kind"] == "unknown_tool"


# ---- construction ---------------------------------------------------------------------

def test_from_settings_uses_the_configured_limits():
    s = Settings(_env_file=None, max_tool_rounds=3, agent_timeout_s=12.5)  # type: ignore[call-arg]
    agent = Agent.from_settings(ScriptedClient(), make_registry(), s)
    assert (agent.max_tool_rounds, agent.timeout_s) == (3, 12.5)


def test_constructor_validation():
    with pytest.raises(ValueError):
        make_agent(ScriptedClient(), max_tokens=10, max_tokens_cap=5)
    with pytest.raises(ValueError):
        make_agent(ScriptedClient(), max_tool_rounds=-1)