import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent
from tool_schemas import TOOLS
from db import InsufficientStock, NotFound, BelowCostSale


def test_every_declared_tool_has_exactly_one_dispatch_entry():
    """If this ever fails, the model can 'see' a tool it can't actually call
    (or vice versa) -- a silent, hard-to-notice class of bug."""
    schema_names = sorted(t["name"] for t in TOOLS)
    dispatch_names = sorted(agent.DISPATCH.keys())
    assert schema_names == dispatch_names, (
        f"mismatch -- in schema but not dispatch: {set(schema_names) - set(dispatch_names)}, "
        f"in dispatch but not schema: {set(dispatch_names) - set(schema_names)}"
    )


def test_openai_tools_conversion_preserves_every_tool_and_its_parameters():
    converted = agent.OPENAI_TOOLS
    assert len(converted) == len(TOOLS)
    by_name = {t["function"]["name"]: t for t in converted}
    for original in TOOLS:
        conv = by_name[original["name"]]
        assert conv["type"] == "function"
        assert conv["function"]["description"] == original["description"]
        assert conv["function"]["parameters"] == original["input_schema"]


def test_model_chain_is_never_empty_and_has_no_duplicates():
    assert len(agent.MODEL_CHAIN) > 0
    assert len(agent.MODEL_CHAIN) == len(set(agent.MODEL_CHAIN)), "MODEL_CHAIN should not contain duplicate entries"


def test_run_tool_maps_insufficient_stock_to_plain_language():
    def fake_finalize(conn, owner_id, **kw):
        raise InsufficientStock(product_id=5, requested=10, available=3)
    agent.DISPATCH["finalize_bill"], original = fake_finalize, agent.DISPATCH["finalize_bill"]
    try:
        result = agent._run_tool(None, None, "finalize_bill", {})
        assert result["error"] == "insufficient_stock"
        assert "3" in result["message"] and "10" in result["message"]
        assert "Traceback" not in result["message"], "must never leak raw internals to the owner"
    finally:
        agent.DISPATCH["finalize_bill"] = original


def test_run_tool_maps_below_cost_to_plain_language_with_confirm_hint():
    def fake_add(conn, owner_id, **kw):
        raise BelowCostSale(product_id=5, sell_price=8, cost_price=10)
    agent.DISPATCH["add_bill_item"], original = fake_add, agent.DISPATCH["add_bill_item"]
    try:
        result = agent._run_tool(None, None, "add_bill_item", {})
        assert result["error"] == "below_cost"
        assert "confirm_below_cost" in result["message"]
    finally:
        agent.DISPATCH["add_bill_item"] = original


def test_run_tool_maps_not_found_to_plain_language():
    def fake_check(conn, owner_id, **kw):
        raise NotFound("product 999 not found")
    agent.DISPATCH["check_stock"], original = fake_check, agent.DISPATCH["check_stock"]
    try:
        result = agent._run_tool(None, None, "check_stock", {})
        assert result["error"] == "not_found"
    finally:
        agent.DISPATCH["check_stock"] = original


def test_run_tool_never_leaks_raw_exception_internals_on_unexpected_error():
    """The exact hardening item from earlier -- an unexpected exception must
    become a generic, safe message, never a stack trace or table name."""
    def fake_broken(conn, owner_id, **kw):
        raise RuntimeError("SQLITE_CONSTRAINT: UNIQUE constraint failed: products.internal_secret_column")
    agent.DISPATCH["low_stock_report"], original = fake_broken, agent.DISPATCH["low_stock_report"]
    try:
        result = agent._run_tool(None, None, "low_stock_report", {})
        assert result["error"] == "internal_error"
        assert "SQLITE_CONSTRAINT" not in result["message"]
        assert "internal_secret_column" not in result["message"]
    finally:
        agent.DISPATCH["low_stock_report"] = original


def test_run_tool_maps_value_error_to_invalid_request():
    def fake_cancel(conn, owner_id, **kw):
        raise ValueError("only draft bills can be cancelled")
    agent.DISPATCH["cancel_bill"], original = fake_cancel, agent.DISPATCH["cancel_bill"]
    try:
        result = agent._run_tool(None, None, "cancel_bill", {})
        assert result["error"] == "invalid_request"
        assert result["message"] == "only draft bills can be cancelled"
    finally:
        agent.DISPATCH["cancel_bill"] = original


def test_run_tool_handles_none_input_for_zero_argument_tools():
    """Regression test for a real bug found in live testing: some models
    emit 'null' as the arguments string for zero-parameter tool calls
    (daily_close, low_stock_report), and json.loads('null') -> None, which
    crashed fn(**tool_input) with 'argument after ** must be a mapping, not
    NoneType'. The actual fix lives in agent.py's run_turn loop (coercing
    None -> {} right after json.loads, before _run_tool is ever called) --
    this test proves that once coerced, dispatch succeeds instead of
    raising the specific TypeError we saw live."""
    coerced_input = None or {}  # mirrors run_turn's `json.loads(...) or {}` line
    assert coerced_input == {}

    def fake_low_stock(conn, owner_id, **kw):
        # proves the call succeeds with the coerced {} -- would raise
        # "argument after ** must be a mapping, not NoneType" if uncoerced
        return {"low_stock": []}

    agent.DISPATCH["low_stock_report"], original = fake_low_stock, agent.DISPATCH["low_stock_report"]
    try:
        result = agent._run_tool(None, None, "low_stock_report", coerced_input)
        assert result == {"low_stock": []}
    finally:
        agent.DISPATCH["low_stock_report"] = original


def test_json_loads_null_string_would_break_dispatch_without_the_or_empty_dict_fix():
    """Documents the exact root cause: json.loads('null') == None, and
    fn(**None) raises TypeError. This is what some models literally send as
    'arguments' for a zero-parameter tool call."""
    import json
    raw_arguments = "null"
    naive_parse = json.loads(raw_arguments)
    assert naive_parse is None, "confirms this is genuinely what the broken input looked like"

    # the fix applied in agent.py's run_turn:
    fixed_parse = json.loads(raw_arguments) or {}
    assert fixed_parse == {}

    # proves the fixed version is safely spreadable, the naive one is not
    def sink(**kw):
        return kw
    sink(**fixed_parse)  # must not raise
    try:
        sink(**naive_parse)
        assert False, "expected this to raise -- if it doesn't, Python's behavior changed"
    except TypeError:
        pass


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
