import pytest
from typing import Type
from pydantic import BaseModel, Field

from audit_agent.llm.cache import ReplayMiss
from audit_agent.llm.client import LLMError
from audit_agent.llm.cost import BudgetExceeded
from audit_agent.tools.registry import Tool, ToolError, ToolRegistry, ToolResult


class AddArgs(BaseModel):
    a: int
    b: int = Field(description="second addend")


class NoArgs(BaseModel):
    pass


def add_tool() -> Tool[AddArgs]:
    return Tool(name="add", description="Add two integers.", params=AddArgs,
                func=lambda p: str(p.a + p.b))


def tool_with(func, params: Type[BaseModel]=NoArgs, name="t") -> Tool:
    return Tool(name=name, description="d", params=params, func=func)


def test_spec_shape():
    spec = add_tool().spec()
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "add" and fn["description"] == "Add two integers."
    assert set(fn["parameters"]["properties"]) == {"a", "b"}
    assert fn["parameters"]["required"] == ["a", "b"]
    assert "title" not in fn["parameters"]
    assert fn["parameters"]["properties"]["b"]["description"] == "second addend"


@pytest.mark.parametrize("name", ["has space", "x" * 65, "", "a.b"])
def test_invalid_names_rejected(name):
    with pytest.raises(ValueError, match="invalid tool name"):
        tool_with(lambda p: "x", name=name)


@pytest.mark.parametrize("name", ["read_file", "read-file", "A1"])
def test_valid_names_accepted(name):
    assert tool_with(lambda p: "x", name=name).name == name


def test_blank_description_rejected():
    with pytest.raises(ValueError, match="description"):
        Tool(name="t", description="  ", params=NoArgs, func=lambda p: "x")


def test_nested_params_rejected():
    class Inner(BaseModel):
        x: int

    class Outer(BaseModel):
        inner: Inner

    with pytest.raises(ValueError, match="flat"):
        tool_with(lambda p: "x", params=Outer)


def test_duplicate_name_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([add_tool(), add_tool()])


def test_specs_keep_registration_order():
    reg = ToolRegistry([tool_with(lambda p: "x", name="b"), tool_with(lambda p: "x", name="a")])
    assert [s["function"]["name"] for s in reg.specs()] == ["b", "a"]


def test_execute_ok():
    r = ToolRegistry([add_tool()]).execute("add", '{"a": 17, "b": 25}')
    assert (r.content, r.error_kind, r.is_error) == ("42", None, False)


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_arguments_mean_no_arguments(raw):
    r = ToolRegistry([tool_with(lambda p: "pong")]).execute("t", raw)
    assert (r.content, r.error_kind) == ("pong", None)


@pytest.mark.parametrize("raw", ["{broken", "[1]", '"text"', "5"])
def test_arguments_must_be_a_json_object(raw):
    r = ToolRegistry([add_tool()]).execute("add", raw)
    assert r.error_kind == "bad_json" and r.content.startswith("error:")


def test_validation_error_names_the_fields():
    r = ToolRegistry([add_tool()]).execute("add", '{"a": "x"}')
    assert r.error_kind == "bad_arguments"
    assert "a:" in r.content and "b:" in r.content   # wrong type and missing field


def test_unknown_tool_lists_the_available_ones():
    r = ToolRegistry([add_tool()]).execute("nope", "{}")
    assert r.error_kind == "unknown_tool" and "add" in r.content


def test_tool_error_message_goes_to_the_model():
    def fail(p):
        raise ToolError("file not found: A.sol")

    r = ToolRegistry([tool_with(fail)]).execute("t", "{}")
    assert (r.content, r.error_kind) == ("error: file not found: A.sol", "tool_failed")


def test_crash_hides_details_from_the_model_but_keeps_them_for_the_trace():
    def boom(p):
        raise RuntimeError("secret /tmp/x")

    r = ToolRegistry([tool_with(boom)]).execute("t", "{}")
    assert r.error_kind == "tool_crashed"
    assert "secret" not in r.content and "RuntimeError" in r.content
    assert r.detail == "RuntimeError: secret /tmp/x"


@pytest.mark.parametrize(
    "err", [LLMError("down"), BudgetExceeded("token", 10, 5), ReplayMiss("k" * 20)]
)
def test_infrastructure_errors_propagate(err):
    def raiser(p):
        raise err

    with pytest.raises(type(err)):
        ToolRegistry([tool_with(raiser)]).execute("t", "{}")


def test_tool_may_return_an_artifact():
    reg = ToolRegistry([tool_with(lambda p: ToolResult("ok", artifact={"k": 1}))])
    r = reg.execute("t", "{}")
    assert (r.content, r.artifact) == ("ok", {"k": 1})