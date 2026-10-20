import pytest
from pydantic import BaseModel

from audit_agent.llm.cache import ReplayMiss, make_cache_key
from audit_agent.llm.client import LLMError
from audit_agent.llm.cost import BudgetExceeded
from audit_agent.llm.structured import (
    OutputTruncated, ScanReport, StructuredOutputError, iter_candidates, structured_chat,
)
from audit_agent.llm.types import ChatResult
from audit_agent.observability.tracer import Tracer, read_events
from audit_agent.schemas.finding import FindingDraft

BASE = [{"role": "user", "content": "q"}]
GOOD = '{"x": 1, "y": 2}'


class Point(BaseModel):
    x: int
    y: int


class Findings(BaseModel):
    """Same shape as the Discoverer wrapper: `findings` has NO default on purpose."""

    findings: list[Point]


class ScriptedClient:
    """Returns scripted replies in order; an Exception instance in the script is raised."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, messages, *, temperature=0.0, max_tokens=None, **_):
        self.calls.append({"messages": list(messages), "max_tokens": max_tokens})
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def reply(content, finish="stop", reasoning=None) -> ChatResult:
    return ChatResult(content=content, finish_reason=finish, reasoning_content=reasoning)


def objs(text: str) -> list[dict]:
    return [c.obj for c in iter_candidates(text)]


# ---- extraction -----------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    GOOD,
    f"Here you go:\n```json\n{GOOD}\n```\nHope it helps",
    f"```\n{GOOD}\n```",
    f"Sure! {GOOD} Let me know.",
    "The struct {balance, owner} is risky.\n" + GOOD,          # braces in prose come first
    '{"x": 1, "y": 2, "note": "braces } and { inside a string"}',
])
def test_finds_the_object(text):
    assert objs(text)[0]["x"] == 1


def test_nested_objects_are_not_separate_candidates():
    assert len(objs('{"x": 1, "y": 2, "loc": {"a": 1}}')) == 1


@pytest.mark.parametrize("text", ["{}", "function f() {}", "", "   ", "no json", "[1, 2]"])
def test_nothing_usable(text):
    assert objs(text) == []


def test_empty_solidity_body_is_ignored():
    assert objs(f"```solidity\nfunction f() {{}}\n```\n{GOOD}") == [{"x": 1, "y": 2}]


def test_blocks_labelled_with_other_languages_are_skipped():
    text = "```solidity\ns = '{\"x\": 99, \"y\": 99}';\n```\n" + GOOD
    assert objs(text) == [{"x": 1, "y": 2}]          # the lookalike inside the block is ignored


def test_layers_and_language_label_case():
    assert [c.layer for c in iter_candidates(f"```JSON\n{GOOD}\n```")] == ["fenced"]
    assert [c.layer for c in iter_candidates(GOOD)] == ["text"]


def test_same_object_in_block_and_text_is_one_candidate():
    assert len(objs(f"```json\n{GOOD}\n```\nrepeated: {GOOD}")) == 1


def test_scan_report_keeps_the_most_json_like_failure():
    report = ScanReport()
    list(iter_candidates('struct {a, b} then {"x": 1, "y"', report))
    assert report.parse_error is not None and "delimiter" in report.parse_error


# ---- success paths --------------------------------------------------------------------

def test_valid_first_try():
    c = ScriptedClient(reply(GOOD))
    r = structured_chat(c, BASE, Point)
    assert (r.value, r.attempts, r.layer) == (Point(x=1, y=2), 1, "text")
    assert len(c.calls) == 1


def test_fenced_json_is_accepted():
    r = structured_chat(ScriptedClient(reply(f"```json\n{GOOD}\n```")), BASE, Point)
    assert (r.value.y, r.layer) == (2, "fenced")


def test_first_valid_object_wins():
    text = '{"x": 1, "y": 2} and also {"x": 3, "y": 4}'
    assert structured_chat(ScriptedClient(reply(text)), BASE, Point).value == Point(x=1, y=2)


def test_invalid_candidate_is_skipped_within_the_same_reply():
    c = ScriptedClient(reply('{"x": "no", "y": 2} then {"x": 1, "y": 2}'))
    r = structured_chat(c, BASE, Point)
    assert (r.value, r.attempts) == (Point(x=1, y=2), 1)
    assert len(c.calls) == 1                         # no retry needed


def test_finding_draft_category_is_normalized_through_the_pipeline():
    body = (
        '```json\n{"category": "Access Control", "title": "t", "description": "d",'
        ' "location": {"file": "A.sol", "line_start": 1, "line_end": 2},'
        ' "severity": "high", "confidence": 0.7}\n```'
    )
    assert structured_chat(ScriptedClient(reply(body)), BASE, FindingDraft).value.category == "access_control"


# ---- wrapper contract: "nothing found" must be explicit -------------------------------

def test_explicit_empty_findings_is_a_valid_answer():
    r = structured_chat(ScriptedClient(reply('{"findings": []}')), BASE, Findings)
    assert r.value.findings == []


@pytest.mark.parametrize("bad", ["{}", '{"other": 1}', "all good, nothing to report"])
def test_missing_findings_is_never_read_as_nothing_found(bad):
    c = ScriptedClient(reply(bad), reply('{"findings": []}'))
    assert structured_chat(c, BASE, Findings).attempts == 2


def test_feedback_names_the_missing_field():
    c = ScriptedClient(reply('{"other": 1}'), reply('{"findings": []}'))
    structured_chat(c, BASE, Findings)
    assert "findings:" in c.calls[1]["messages"][-1]["content"]


# ---- feedback retries -----------------------------------------------------------------

def test_invalid_json_then_valid():
    c = ScriptedClient(reply('{"x": 1, "y"'), reply(GOOD))
    r = structured_chat(c, BASE, Point)
    assert r.attempts == 2
    assert c.calls[0]["messages"] == BASE
    retry = c.calls[1]["messages"]
    assert retry[:1] == BASE                                  # original prompt kept
    assert [m["role"] for m in retry[1:]] == ["assistant", "user"]
    assert retry[1]["content"] == '{"x": 1, "y"'              # model sees its own reply
    assert "invalid JSON" in retry[2]["content"]


def test_feedback_names_the_failing_field():
    c = ScriptedClient(reply('{"x": "abc", "y": 2}'), reply(GOOD))
    structured_chat(c, BASE, Point)
    assert "x:" in c.calls[1]["messages"][-1]["content"]


def test_unknown_category_feedback_lists_valid_values_and_recovers():
    bad = (
        '{"category": "made_up", "title": "t", "description": "d",'
        ' "location": {"file": "A.sol", "line_start": 1, "line_end": 2},'
        ' "severity": "high", "confidence": 0.7}'
    )
    c = ScriptedClient(reply(bad), reply(bad.replace("made_up", "reentrancy")))
    r = structured_chat(c, BASE, FindingDraft)
    assert "must be one of" in c.calls[1]["messages"][-1]["content"]
    assert r.value.category == "reentrancy"


def test_empty_reply_is_retried_with_nonempty_assistant_turn():
    c = ScriptedClient(reply(""), reply(GOOD))
    structured_chat(c, BASE, Point)
    assert c.calls[1]["messages"][1]["content"]
    assert "empty" in c.calls[1]["messages"][-1]["content"]


def test_reasoning_content_is_never_sent_back():
    c = ScriptedClient(reply("bad", reasoning="SECRET-THOUGHTS"), reply(GOOD))
    structured_chat(c, BASE, Point)
    assert "SECRET-THOUGHTS" not in str(c.calls[1]["messages"])


def test_retry_changes_the_request_so_cache_cannot_replay_the_bad_answer():
    c = ScriptedClient(reply("bad"), reply(GOOD))
    structured_chat(c, BASE, Point)
    keys = {make_cache_key({"messages": call["messages"]}, base_url="u") for call in c.calls}
    assert len(keys) == 2


def test_callers_messages_are_not_mutated():
    base = [{"role": "user", "content": "q"}]
    structured_chat(ScriptedClient(reply("bad"), reply(GOOD)), base, Point)
    assert base == [{"role": "user", "content": "q"}]


def test_gives_up_after_max_attempts():
    c = ScriptedClient(reply("bad"), reply("worse"), reply("still bad"))
    with pytest.raises(StructuredOutputError) as exc:
        structured_chat(c, BASE, Point, max_attempts=3)
    assert exc.value.attempts == 3 and exc.value.last_content == "still bad"
    assert not isinstance(exc.value, OutputTruncated)
    assert len(c.calls) == 3


# ---- truncation -----------------------------------------------------------------------

def test_truncation_raises_budget_and_keeps_messages():
    c = ScriptedClient(reply("", finish="length"), reply(GOOD))
    r = structured_chat(c, BASE, Point, max_tokens=100, max_tokens_cap=1000)
    assert r.attempts == 2
    assert [call["max_tokens"] for call in c.calls] == [100, 200]
    assert c.calls[1]["messages"] == BASE                    # NOT told "invalid JSON"


def test_truncation_at_cap_fails_immediately():
    c = ScriptedClient(reply('{"x": 1', finish="length"))
    with pytest.raises(OutputTruncated):
        structured_chat(c, BASE, Point, max_tokens=100, max_tokens_cap=100)
    assert len(c.calls) == 1                                  # repeating the same request is pointless


def test_truncation_on_the_last_attempt_is_reported_as_truncation():
    c = ScriptedClient(reply("", finish="length"))
    with pytest.raises(OutputTruncated):
        structured_chat(c, BASE, Point, max_attempts=1, max_tokens=100, max_tokens_cap=1000)


# ---- things that must pass through ----------------------------------------------------

@pytest.mark.parametrize(
    "err", [LLMError("down"), BudgetExceeded("token", 10, 5), ReplayMiss("k" * 20)]
)
def test_infrastructure_errors_are_not_swallowed_or_retried(err):
    c = ScriptedClient(err, reply(GOOD))
    with pytest.raises(type(err)):
        structured_chat(c, BASE, Point)
    assert len(c.calls) == 1


def test_argument_validation():
    with pytest.raises(ValueError):
        structured_chat(ScriptedClient(), BASE, Point, max_attempts=0)
    with pytest.raises(ValueError):
        structured_chat(ScriptedClient(), BASE, Point, max_tokens=10, max_tokens_cap=5)


# ---- tracing --------------------------------------------------------------------------

def test_trace_records_layer_and_extra_candidates(tmp_path):
    tracer = Tracer("r1", tmp_path)
    text = '{"x": 1, "y": 2} and {"x": 3, "y": 4}'
    structured_chat(ScriptedClient(reply(text)), BASE, Point, tracer=tracer)
    (e,) = read_events(tracer.path)
    assert (e.kind, e.name, e.status) == ("structured", "Point", "ok")
    assert e.attrs["attempts"] == 1
    assert e.attrs["layer"] == "text"
    assert e.attrs["candidates_checked"] == 1
    assert e.attrs["extra_candidates"] == 1


def test_trace_keeps_the_last_problem_on_failure(tmp_path):
    tracer = Tracer("r1", tmp_path)
    with pytest.raises(StructuredOutputError):
        structured_chat(ScriptedClient(reply("bad")), BASE, Point, max_attempts=1, tracer=tracer)
    (e,) = read_events(tracer.path)
    assert e.status == "error"
    assert "no JSON object" in e.attrs["last_problem"]