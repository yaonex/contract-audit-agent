import copy
import json
from pathlib import Path

import pytest

from audit_agent.agents.discoverer import Discoverer
from audit_agent.context.source import build_context
from audit_agent.llm.cost import BudgetExceeded
from audit_agent.llm.types import ChatResult, Usage
from audit_agent.schemas.finding import FindingSource, Severity

SAMPLES_DIR = Path(__file__).resolve().parents[3] / "samples"


class ScriptedClient:
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


def final(text: str, finish: str = "stop") -> ChatResult:
    return ChatResult(content=text, finish_reason=finish, usage=Usage(prompt_tokens=10, completion_tokens=10))


@pytest.fixture
def bank_ctx():
    return build_context(SAMPLES_DIR / "vulnerable" / "Bank.sol")


def test_discoverer_successful_run(bank_ctx):
    payload = {
        "findings": [
            {
                "title": "Reentrancy in withdraw",
                "description": "External call before state update",
                "line_start": 11,
                "line_end": 17,
                "function": "withdraw",
                "contract": "Bank",
                "quotes": ['14 | (bool ok, ) = msg.sender.call{value: amount}("");', "balances[msg.sender] = 0;"],
                "severity": "critical",
                "confidence": 0.95,
            }
        ]
    }
    client = ScriptedClient(final(json.dumps(payload)))
    discoverer = Discoverer("reentrancy", "reentrancy.v1", client=client)

    findings, info = discoverer.run(bank_ctx)
    assert len(findings) == 1
    assert findings[0].category == "reentrancy"
    assert findings[0].source == FindingSource.DISCOVERER
    assert findings[0].severity == Severity.CRITICAL
    assert info.model_returned == 1
    assert info.kept == 1
    assert info.retries == 0
    assert info.truncations == 0


def test_discoverer_format_retry_and_success(bank_ctx):
    good_payload = {
        "findings": [
            {
                "title": "Reentrancy in withdraw",
                "description": "External call before state update",
                "line_start": 11,
                "line_end": 17,
                "function": "withdraw",
                "contract": "Bank",
                "quotes": ["balances[msg.sender] = 0;"],
                "severity": "high",
                "confidence": 0.9,
            }
        ]
    }
    # First attempt: invalid JSON; second attempt: valid DiscovererOutput
    client = ScriptedClient(final("I found a bug in withdraw"), final(json.dumps(good_payload)))
    discoverer = Discoverer("reentrancy", "reentrancy.v1", client=client)

    findings, info = discoverer.run(bank_ctx)
    assert len(findings) == 1
    assert info.retries == 1
    assert info.kept == 1
    assert len(client.calls) == 2


def test_discoverer_budget_exceeded_propagates_unswallowed(bank_ctx):
    client = ScriptedClient(BudgetExceeded("token", 10_000, 5_000))
    discoverer = Discoverer("reentrancy", "reentrancy.v1", client=client)

    with pytest.raises(BudgetExceeded):
        discoverer.run(bank_ctx)


def test_discoverer_all_dropped_returns_empty_and_info_reflects_reason(bank_ctx):
    fabricated = {
        "findings": [
            {
                "title": "Fabricated",
                "description": "Hallucinated lines",
                "line_start": 80,
                "line_end": 90,
                "quotes": ["some fake code"],
                "severity": "high",
                "confidence": 0.9,
            }
        ]
    }
    client = ScriptedClient(final(json.dumps(fabricated)))
    discoverer = Discoverer("reentrancy", "reentrancy.v1", client=client)

    findings, info = discoverer.run(bank_ctx)
    assert findings == []
    assert info.model_returned == 1
    assert info.kept == 0
    assert info.dropped_outside_file == 1


def test_discoverer_truncation_increases_budget_and_counts(bank_ctx):
    good_payload = {
        "findings": [
            {
                "title": "Reentrancy",
                "description": "desc",
                "line_start": 11,
                "line_end": 17,
                "quotes": ["balances[msg.sender] = 0;"],
                "severity": "medium",
                "confidence": 0.7,
            }
        ]
    }
    client = ScriptedClient(final("partial", finish="length"), final(json.dumps(good_payload)))
    discoverer = Discoverer("reentrancy", "reentrancy.v1", client=client)

    findings, info = discoverer.run(bank_ctx)
    assert len(findings) == 1
    assert info.truncations == 1
    assert info.kept == 1
    assert len(client.calls) == 2
