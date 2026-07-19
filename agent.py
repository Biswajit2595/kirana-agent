"""
The control loop: Observe -> reason -> act (tool call) -> feed result back
-> continue. This is intentionally thin -- it does not parse intent itself.
Every decision about WHICH tool to call is the model's; this file only
dispatches whatever it decides on to the matching Python function.

NOTE ON PROVIDER: this uses Groq's API (OpenAI-compatible), not the
Anthropic or Gemini APIs. Anthropic requires billing to be attached before
use; Gemini's free tier, as of testing, returns limit:0 on every free-tier
quota regardless of account/region until billing is linked too. Groq's free
tier works without a card. The architecture is provider-agnostic by design
-- tool_schemas.py, the DISPATCH table, and every tools/*.py module are
untouched. Only this file's "how do I call the model and get tool-call
results back" plumbing changed, for the third time, which is exactly the
point of keeping business logic out of this layer.
"""
import os
import json
import logging
from openai import OpenAI
from db import get_conn, InsufficientStock, NotFound, BelowCostSale
from tool_schemas import TOOLS
from tools import inventory, billing, khata, reporting, documents, preferences

log = logging.getLogger("kirana-agent")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1")
    return _client


# Ordered fallback chain -- tried in order. If the model in .env (or the
# first default) hits a rate limit (429) or a permission block (403), the
# code automatically moves to the next one instead of failing the turn and
# requiring a manual .env edit + restart. Ordered by daily token budget,
# largest first (per Groq's free-tier limits page) -- llama-3.3-70b-versatile
# has the SMALLEST daily cap (100K) of the group, so it goes last, not first.
# Override the whole chain via GROQ_MODEL_CHAIN="model-a,model-b" in .env.
_default_chain = "openai/gpt-oss-20b,qwen/qwen3.6-27b,llama3-groq-70b-8192-tool-use-preview,llama-3.3-70b-versatile"
MODEL_CHAIN = [m.strip() for m in os.environ.get("GROQ_MODEL_CHAIN", _default_chain).split(",") if m.strip()]
# Back-compat: an explicit single GROQ_MODEL still works, takes priority as the first try.
if os.environ.get("GROQ_MODEL"):
    m = os.environ["GROQ_MODEL"]
    MODEL_CHAIN = [m] + [x for x in MODEL_CHAIN if x != m]

DISPATCH = {
    "search_product": lambda conn, owner_id, **kw: inventory.search_product(conn, owner_id, **kw),
    "add_product": lambda conn, owner_id, **kw: inventory.add_product(conn, owner_id, **kw),
    "receive_stock": lambda conn, owner_id, **kw: inventory.receive_stock(conn, **kw),
    "check_stock": lambda conn, owner_id, **kw: inventory.check_stock(conn, **kw),
    "low_stock_report": lambda conn, owner_id, **kw: inventory.low_stock_report(conn, owner_id),
    "start_bill": lambda conn, owner_id, **kw: billing.start_bill(conn, owner_id, **kw),
    "add_bill_item": lambda conn, owner_id, **kw: billing.add_bill_item(conn, **kw),
    "remove_bill_item": lambda conn, owner_id, **kw: billing.remove_bill_item(conn, **kw),
    "edit_bill_item": lambda conn, owner_id, **kw: billing.edit_bill_item(conn, **kw),
    "preview_bill": lambda conn, owner_id, **kw: billing.preview_bill(conn, **kw),
    "finalize_bill": lambda conn, owner_id, **kw: billing.finalize_bill(conn, **kw),  # idempotency_key injected below
    "cancel_bill": lambda conn, owner_id, **kw: billing.cancel_bill(conn, **kw),
    "khata_add_credit": lambda conn, owner_id, **kw: khata.khata_add_credit(conn, owner_id, **kw),
    "khata_pay": lambda conn, owner_id, **kw: khata.khata_pay(conn, owner_id, **kw),
    "khata_balance": lambda conn, owner_id, **kw: khata.khata_balance(conn, owner_id, **kw),
    "daily_close": lambda conn, owner_id, **kw: reporting.daily_close(conn, owner_id, **kw),
    "sales_range": lambda conn, owner_id, **kw: reporting.sales_range(conn, owner_id, **kw),
    "generate_invoice_pdf": lambda conn, owner_id, **kw: documents.generate_invoice_pdf(conn, **kw),
    "generate_analysis_deck": lambda conn, owner_id, **kw: documents.generate_analysis_deck(conn, owner_id, **kw),
    "set_preference": lambda conn, owner_id, **kw: preferences.set_preference(conn, owner_id, **kw),
}

# tools whose retries must be idempotent -- keyed off the Telegram update_id.
# Any tool that mutates stock or money needs this, not just sales: a
# redelivered "receive_stock" would silently double-count incoming stock
# the same way a redelivered "finalize_bill" would double-decrement it.
IDEMPOTENT_TOOLS = {"finalize_bill", "khata_add_credit", "khata_pay", "receive_stock", "adjust_stock"}

SYSTEM_PROMPT_TEMPLATE = """You are the ops agent for an Indian kirana (grocery) store, reached over Telegram by the owner.
You run the store through tool calls only -- you never invent a product, price, GST rate, or stock quantity that isn't returned by a tool.

Rules:
- If a request is ambiguous (e.g. "atta" when multiple atta products exist), call search_product and ask the owner which one they mean, rather than guessing.
- A bill is built with start_bill / add_bill_item / edit_bill_item / remove_bill_item, and only becomes real when the owner confirms and you call finalize_bill. Stock is untouched until finalize_bill.
- finalize_bill can fail with an insufficient-stock error -- relay this plainly to the owner ("only 6 left, not 10"), do not silently reduce the quantity yourself.
- Never call khata_pay for a customer with no khata account -- if it fails, tell the owner and ask if they meant to start a new credit account instead (khata_add_credit).
- Currency is INR (₹). Be terse and direct in replies, the way a shopkeeper would want -- no corporate padding.
- CRITICAL: never state that a bill was finalized, stock was updated, a product was added, or any other store data changed unless you actually called the corresponding tool THIS turn and received a successful result back. If you're unsure what happened, say so and check with check_stock/preview_bill rather than asserting an outcome.
- If a single message mixes two different intents (e.g. adding a new product AND continuing an existing bill), handle them as two separate, sequential tool-call sequences -- do not blend numbers from one intent (like a quantity in a product name, e.g. "100g") into the other (like a bill quantity).

Known standing preferences for this shop (persisted across sessions -- honor them unless the owner overrides in this message):
{preferences}
"""


def _to_openai_tools():
    """Claude-style schemas (name/description/input_schema) -> OpenAI/Groq's
    tool format ({"type": "function", "function": {name, description,
    parameters}}). The JSON-schema body is passed straight through."""
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in TOOLS
    ]


OPENAI_TOOLS = _to_openai_tools()


def _run_tool(conn, owner_id, tool_name, tool_input):
    """Executes one tool call and returns a JSON-serializable result dict.
    Same exception -> plain-language mapping regardless of provider."""
    try:
        fn = DISPATCH[tool_name]
        return fn(conn, owner_id, **tool_input)
    except InsufficientStock as e:
        return {
            "error": "insufficient_stock",
            "message": f"Only {e.available:g} in stock, but {e.requested:g} were requested for product {e.product_id}.",
            "product_id": e.product_id, "requested": e.requested, "available": e.available,
        }
    except BelowCostSale as e:
        return {
            "error": "below_cost",
            "message": f"Selling price ₹{e.sell_price:g} is below cost price ₹{e.cost_price:g}. Ask the owner to confirm before proceeding (pass confirm_below_cost=true if they say yes).",
            "product_id": e.product_id, "sell_price": e.sell_price, "cost_price": e.cost_price,
        }
    except NotFound as e:
        return {"error": "not_found", "message": str(e)}
    except ValueError as e:
        return {"error": "invalid_request", "message": str(e)}
    except Exception:
        log.exception("unexpected tool error in %s", tool_name)
        return {"error": "internal_error", "message": "Something went wrong on our end — please try again, or tell the owner to check with support."}


def run_turn(owner_id, telegram_update_id, user_message, conversation_history=None):
    """
    One full agentic turn. conversation_history is a plain list of OpenAI
    chat-format message dicts ({"role": ..., "content": ...} etc.) from THIS
    chat session (may be empty right after /new chat -- that's fine,
    preferences are re-hydrated from the DB below regardless).

    Returns (final_text, updated_history, generated_files) -- generated_files
    is a plain list of file paths produced by any tool this turn (PDF/PPTX).
    """
    conn = get_conn()
    client = _get_client()
    prefs = preferences.get_preferences(conn, owner_id)  # <-- durable memory, independent of chat history
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        preferences=json.dumps(prefs, indent=2) if prefs else "(none set yet)"
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation_history or [])
    messages.append({"role": "user", "content": user_message})

    generated_files = []
    call_counter = 0
    max_malformed_retries = 2
    model_index = 0  # walks MODEL_CHAIN on rate-limit/permission errors

    while True:
        current_model = MODEL_CHAIN[model_index]
        try:
            response = client.chat.completions.create(
                model=current_model, messages=messages, tools=OPENAI_TOOLS, max_tokens=600,
            )
        except Exception as e:
            err_text = str(e)

            # Rate-limited or blocked on this model -- move to the next one
            # in the chain automatically instead of failing the turn. This
            # is what makes a per-model daily cap or an org-level block on
            # one model a non-issue rather than a manual .env-edit-and-restart
            # chore every time it happens.
            is_quota_or_permission_error = ("429" in err_text or "403" in err_text
                                             or "rate_limit" in err_text or "permission" in err_text.lower())
            if is_quota_or_permission_error and model_index + 1 < len(MODEL_CHAIN):
                log.warning("model %s unavailable (%s), falling back to %s",
                            current_model, err_text[:120], MODEL_CHAIN[model_index + 1])
                model_index += 1
                continue

            # Specifically handle the "model emitted a malformed tool call as
            # raw text" failure mode (Groq returns 400 tool_use_failed). This
            # is a model reliability quirk, not a bug in our request -- the
            # fix is to nudge the model to retry with a stricter instruction,
            # not to crash the whole turn and leave the owner with no reply.
            is_tool_use_failed = "tool_use_failed" in err_text
            if is_tool_use_failed and max_malformed_retries > 0:
                max_malformed_retries -= 1
                messages.append({
                    "role": "user",
                    "content": "(system note: your last response wasn't a valid tool call -- "
                               "call the tool using the proper function-calling mechanism only, "
                               "with no extra text around it.)",
                })
                continue

            log.exception("model call failed and could not be recovered (tried: %s)", MODEL_CHAIN[:model_index + 1])
            return ("Sorry, I hit an error processing that -- please try rephrasing, "
                    "or try again in a moment."), messages[1:], generated_files

        choice = response.choices[0]
        messages.append(choice.message.model_dump(exclude_none=True))

        if not choice.message.tool_calls:
            final_text = choice.message.content or ""
            # strip the leading system prompt before returning history -- it's
            # rebuilt fresh every turn from current preferences, never stored
            return final_text, messages[1:], generated_files

        for tc in choice.message.tool_calls:
            call_counter += 1
            tool_name = tc.function.name
            # For zero-argument tools (daily_close, low_stock_report), the
            # model sometimes emits "null" as its arguments instead of "{}" --
            # json.loads("null") returns Python None, which then breaks
            # fn(**tool_input). Coerce to an empty dict in that case.
            tool_input = json.loads(tc.function.arguments) or {}

            # Idempotency key derived from the Telegram update + a per-call
            # counter, so a REDELIVERED update reuses the same key on retry,
            # but two different mutating calls within one legitimate turn
            # don't collide with each other.
            if tool_name in IDEMPOTENT_TOOLS:
                tool_input["idempotency_key"] = f"{telegram_update_id}:{tool_name}:{call_counter}"

            result = _run_tool(conn, owner_id, tool_name, tool_input)
            if isinstance(result, dict) and "file_path" in result:
                generated_files.append(result["file_path"])

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })
