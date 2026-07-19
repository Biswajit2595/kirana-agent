# Supermarket Ops Agent

**Bot handle:** [@biswa_kirana_bot](https://t.me/biswa_kirana_bot)

## Harness & why

Claude Agent SDK-style tool-orchestration loop, implemented against **Groq's
API** (OpenAI-compatible), rather than Anthropic's or Google's. This was a
cost-driven decision made during development, in this order:
Anthropic's API requires a billing method attached before any use; Google
Gemini's free tier, as tested from India on multiple accounts (including a
fresh personal Gmail account with no organizational restrictions), returned
`limit: 0` on every free-tier quota metric regardless of setup — Google
appears to require billing linkage even for nominally "free tier" access.
Groq's free tier works without a card and was the first provider that
actually let the agent run.

The architecture is provider-agnostic by design: `tool_schemas.py`, every
`tools/*.py` module, and the DB layer are untouched by any of these swaps.
Only `agent.py`'s "how do I call the model and parse a tool-call response"
plumbing changed each time — every major provider's function/tool-calling
API takes the same essential shape (name + description + JSON-schema
parameters), so each swap was mechanical, not a redesign. `bot.py` needed
zero changes for the final swap, since `run_turn()`'s return signature
(`text, history, generated_files`) was already provider-agnostic from the
prior swap. No LangGraph-style node-per-command state machine either way:
the "state machine" here is just SQL rows (`bills.status`), not code.

**Known trade-off, disclosed deliberately:** Groq's free tier has daily
per-model token caps (100K-200K depending on model). `agent.py` mitigates
this with a fallback chain across four models ordered by budget size, tool
schemas trimmed to their essential guardrail language, conversation history
capped per turn, and `max_tokens` kept tight -- but a genuinely production
deployment would use a paid tier rather than stitching together free-tier
budgets across providers. This was a deliberate cost decision for a
take-home assignment, not an architectural blind spot: the tool-orchestration
design itself is provider- and budget-agnostic, as demonstrated by three
clean swaps with zero changes to business logic.

## Control loop

```
Telegram update arrives (carries a unique update_id)
  -> load/create owner row for this chat_id
  -> hydrate preferences from DB (NOT from chat history — see Memory below)
  -> hand {system prompt + preferences, tool schemas, message} to Claude
  -> loop: model reasons -> calls tool(s) -> tool result fed back -> model
     reasons again -> ... -> final text response
  -> reply sent to Telegram, plus any generated file (PDF/PPTX)
```

See `agent.py::run_turn` for the literal loop — it's ~40 lines, deliberately.
All business logic lives in `tools/*.py`, not here.

## Skill / tool design

| Skill file | Tools | What it enforces |
|---|---|---|
| `tools/inventory.py` | add_product, search_product, check_stock, low_stock_report, receive_stock, adjust_stock | No `delete_product` tool exists at all — corrections go through signed, logged `adjust_stock` only |
| `tools/billing.py` | start_bill, add_bill_item, edit/remove_bill_item, preview_bill, **finalize_bill**, cancel_bill | Draft/finalize split — stock is untouched until `finalize_bill`, which is the one function doing the atomic oversell-guard decrement |
| `tools/khata.py` | khata_add_credit, khata_pay, khata_balance | `khata_pay` refuses (doesn't auto-create) if the customer has no account — the guardrail the brief calls out explicitly |
| `tools/reporting.py` | daily_close, sales_range | Reads only from `finalized` bills — drafts/cancellations are correctly invisible |
| `tools/documents.py` | generate_invoice_pdf, generate_analysis_deck | Real reportlab/python-pptx+matplotlib output, not text-as-a-file |
| `tools/preferences.py` | get_preferences, set_preference | The actual memory mechanism — see below |

Model-facing schemas live in `tool_schemas.py`, kept separate from the
Python implementations so the "what can the model see/do" surface is
auditable in one file.

## How each hard part was solved

- **Grounding** — every product/price/stock fact the model states comes from
  a tool return value. The system prompt explicitly forbids inventing these,
  and there's no code path where the model could fabricate one even if it tried.

- **Oversell guard** — `finalize_bill` (billing.py) decrements stock with
  `UPDATE products SET qty = qty - ? WHERE id = ? AND qty >= ?` and checks
  `rowcount`. The check and the write are the same atomic SQL statement —
  there's no read-then-write gap for a race to exploit. Proven under real
  concurrent threads in `tests/test_oversell_and_concurrency.py`.

- **GST correctness** — `gst.py` is a pure function, zero dependencies,
  unit-tested in isolation (`tests/test_gst.py`, 5 cases covering 0/5/12/18%
  slabs and rounding) before it was ever wired into billing.

- **Multi-turn bills** — `bills.status` is `draft` until `finalize_bill`
  runs. All edits before that (`add/edit/remove_bill_item`) only touch
  `bill_items`, never `products.qty`.

- **Idempotency** — every mutating tool that matters (`finalize_bill`,
  `khata_add_credit`, `khata_pay`) is wrapped by `db.idempotent()`, keyed on
  `f"{telegram_update_id}:{tool_name}:{call_index}"`. A retried Telegram
  update replays the cached result instead of re-executing. Proven in
  `test_retried_finalize_does_not_double_decrement`.

- **Concurrency** — `db.transaction()` uses `BEGIN IMMEDIATE`, which grabs
  SQLite's write lock up front rather than failing optimistically later.
  Combined with the oversell guard's atomic UPDATE, this is the same
  mechanism solving both problems. Proven with real threads racing on the
  last unit of stock (`test_two_concurrent_bills_racing_last_unit_only_one_wins`)
  and with a stock-in racing a sale on the same product
  (`test_stock_in_racing_a_sale_never_corrupts_quantity`) — the brief names
  this exact case, and the ledger sum is asserted to match regardless of
  which write lands first.

- **Guardrails** — below-cost sales are **blocked**, not just flagged:
  `add_bill_item` raises `BelowCostSale` unless called with
  `confirm_below_cost=True`, which the model only sets after the owner
  explicitly confirms. There is no delete tool for stock. `khata_pay`
  refuses unknown customers rather than auto-creating them. A soft
  `stock_warning` is returned at add-time as an early heads-up, but the
  real, authoritative oversell enforcement remains `finalize_bill`'s atomic
  UPDATE — draft-time stock can always go stale before finalize.

- **Error handling** — tool exceptions are mapped to plain-language
  messages (`agent.py`'s dispatch loop) rather than leaking raw exception
  text or stack traces to the owner via the model. Unexpected errors are
  logged server-side and returned as a generic "something went wrong."

- **Real artifacts** — `generate_invoice_pdf` builds an actual GST-breakup
  table with reportlab; `generate_analysis_deck` renders matplotlib charts
  to PNG and embeds them in a python-pptx deck. Neither is a text dump with
  a file extension.

- **Memory across sessions** — `preferences` is a DB table keyed by
  `owner_id`, hydrated at the start of *every* turn (`agent.py`, before the
  system prompt is built) — independent of `CONVERSATIONS` (the in-memory
  chat history dict), which is what `/new` actually clears. This is the
  detail that makes memory survive a new chat: it was never in the
  conversation to begin with.

## Demo script

The recording follows this exact sequence — every message maps to a
requirement in §6 of the brief, with the edit and oversell-guard steps
folded into the same bill deliberately, to demonstrate both hard parts in
one connected flow rather than two separate detours:

```
new item: Amul Butter 100g, GST 12%, MRP 62, cost 50
new item: Maggi 70g, GST 12%, MRP 14, cost 10
50 Amul Butter came in
50 Maggi came in
make a bill: 3 Amul Butter, 4 Maggi
drop the Maggi, make it 1000 Amul Butter
UPI                                        <- refused: oversell guard
make it 6 Amul Butter instead
UPI                                        <- succeeds
send me that bill as a PDF
put ₹500 on Ramesh's credit
Ramesh paid ₹300
Ramesh's balance?
make this week's sales analysis deck
always assume UPI unless I say cash
/new
make a bill: 1 Amul Butter and finalize it  <- no payment stated, defaults to UPI, proving memory survives /new
```

## Test coverage

57 tests across 9 files, all passing:

| File | Covers |
|---|---|
| `test_gst.py` (5) | GST math at 0/5/12/18% slabs, rounding |
| `test_oversell_and_concurrency.py` (5) | Oversell guard, idempotent retries, real multi-threaded races (sale vs sale, stock-in vs sale) |
| `test_billing_guardrails.py` (2) | Below-cost block, stock-warning-vs-real-enforcement split |
| `test_billing_edge_cases.py` (9) | Cancel/finalize state transitions, empty bills, edit/remove line items, not-found handling, mixed-GST-rate bill matching the brief's own example |
| `test_khata_and_preferences.py` (4) | Khata refuses unknown customer, full credit/payment cycle, preferences surviving a simulated `/new`, disambiguation candidates |
| `test_inventory_edge_cases.py` (10) | Low-stock boundary conditions, negative-adjustment refusal, search matching, direct check_stock coverage |
| `test_reporting.py` (6) | Daily close/sales range aggregation, draft/cancelled exclusion, payment-mode split, top-items ordering |
| `test_documents.py` (6) | PDF/PPTX actually created and non-trivial in size, correct refusal on draft/nonexistent bills, graceful zero-sales handling |
| `test_agent_dispatch.py` (10) | Tool schema ↔ dispatch sync, OpenAI-format conversion correctness, model fallback chain integrity, exception → plain-language error mapping (never leaks internals), regression test for a live None-arguments bug |

```bash
python3 -m pytest tests/ -v
```

## Running it

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -v          # verify the hard parts before touching Telegram
cp .env.example .env                  # fill in TELEGRAM_BOT_TOKEN, GROQ_API_KEY
python3 bot.py
```

## Deploy

1. Create a bot with [@BotFather](https://t.me/BotFather), get the token.
2. Push this repo to Railway or Fly.io, set `TELEGRAM_BOT_TOKEN` and
   `GROQ_API_KEY` as env vars, deploy.
3. `kirana.db` (SQLite) needs a persistent volume on your host — without one,
   restart wipes the store, which fails the "survives a restart" requirement.
4. Message the bot handle, run through the demo script above.

## Known scope cuts

FEFO batch tracking and voice-note ordering (both §7 stretch goals) were not
built — both would touch the core stock/billing model rather than bolting on
cleanly, and weren't worth the risk against the graded hard parts above.
