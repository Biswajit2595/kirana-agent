"""
Tool schemas for the model. Descriptions are kept as short as possible
WITHOUT dropping behavior-critical guardrail language (e.g. "refuses if
insufficient stock" stays -- that's what stops the model from retrying
silently). This file is resent in full on every single API call, so its
size is a direct, controllable cost against a daily token budget.
"""

TOOLS = [
    {
        "name": "search_product",
        "description": "Fuzzy-search products by name/brand. Use for ambiguous requests (e.g. 'atta') to ask which one, instead of guessing.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "add_product",
        "description": "Register a new SKU. gst_rate must be 0, 5, 12, or 18.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "unit": {"type": "string", "enum": ["kg","g","l","ml","packet","dozen","piece"]},
                "gst_rate": {"type": "number"}, "cost_price": {"type": "number"}, "sell_price": {"type": "number"},
                "brand": {"type": "string"}, "is_loose": {"type": "boolean"}, "hsn_code": {"type": "string"},
                "reorder_level": {"type": "number"},
            },
            "required": ["name", "unit", "gst_rate", "cost_price", "sell_price"],
        },
    },
    {
        "name": "receive_stock",
        "description": "Record incoming stock. Increments qty, optionally updates cost price.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "integer"}, "qty": {"type": "number"}, "new_cost_price": {"type": "number"}},
            "required": ["product_id", "qty"],
        },
    },
    {
        "name": "check_stock",
        "description": "Get current quantity for a product.",
        "input_schema": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]},
    },
    {
        "name": "low_stock_report",
        "description": "List products at or below reorder level.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "start_bill",
        "description": "Begin a draft bill. Nothing charged/decremented yet.",
        "input_schema": {"type": "object", "properties": {"customer_name": {"type": "string"}}},
    },
    {
        "name": "add_bill_item",
        "description": "Add a line to a draft bill (stock untouched until finalize_bill). May return below_cost error -- ask owner to confirm, retry with confirm_below_cost=true. May return stock_warning=true -- mention it, but the real check is at finalize_bill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"}, "product_id": {"type": "integer"}, "qty": {"type": "number"},
                "confirm_below_cost": {"type": "boolean"},
            },
            "required": ["bill_id", "product_id", "qty"],
        },
    },
    {
        "name": "remove_bill_item",
        "description": "Remove a line item from a draft bill.",
        "input_schema": {"type": "object", "properties": {"bill_id": {"type": "integer"}, "item_id": {"type": "integer"}}, "required": ["bill_id", "item_id"]},
    },
    {
        "name": "edit_bill_item",
        "description": "Change quantity of an existing draft line item.",
        "input_schema": {"type": "object", "properties": {"bill_id": {"type": "integer"}, "item_id": {"type": "integer"}, "new_qty": {"type": "number"}}, "required": ["bill_id", "item_id", "new_qty"]},
    },
    {
        "name": "preview_bill",
        "description": "Show current draft contents and running GST total.",
        "input_schema": {"type": "object", "properties": {"bill_id": {"type": "integer"}}, "required": ["bill_id"]},
    },
    {
        "name": "finalize_bill",
        "description": "Commit the bill: decrements stock atomically, refuses if ANY line has insufficient stock. Relay a stock-shortfall error plainly ('only 6 left'); never silently retry with a smaller quantity.",
        "input_schema": {
            "type": "object",
            "properties": {"bill_id": {"type": "integer"}, "payment_mode": {"type": "string", "enum": ["cash","upi","card"]}, "payment_ref": {"type": "string"}},
            "required": ["bill_id", "payment_mode"],
        },
    },
    {
        "name": "cancel_bill",
        "description": "Cancel a draft bill (fails if already finalized).",
        "input_schema": {"type": "object", "properties": {"bill_id": {"type": "integer"}}, "required": ["bill_id"]},
    },
    {
        "name": "khata_add_credit",
        "description": "Add to a customer's credit (khata) balance. Auto-creates the customer if new.",
        "input_schema": {"type": "object", "properties": {"customer_name": {"type": "string"}, "amount": {"type": "number"}, "ref_bill_id": {"type": "integer"}}, "required": ["customer_name", "amount"]},
    },
    {
        "name": "khata_pay",
        "description": "Record a khata payment. Fails if customer has no account -- do NOT auto-create, ask the owner instead.",
        "input_schema": {"type": "object", "properties": {"customer_name": {"type": "string"}, "amount": {"type": "number"}}, "required": ["customer_name", "amount"]},
    },
    {
        "name": "khata_balance",
        "description": "Look up a customer's khata balance.",
        "input_schema": {"type": "object", "properties": {"customer_name": {"type": "string"}}, "required": ["customer_name"]},
    },
    {
        "name": "daily_close",
        "description": "Summarize a date's finalized sales: total, tax, payment split, top items.",
        "input_schema": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"}}},
    },
    {
        "name": "sales_range",
        "description": "Summarize sales between two dates.",
        "input_schema": {"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}, "required": ["start_date", "end_date"]},
    },
    {
        "name": "generate_invoice_pdf",
        "description": "Produce a GST-correct PDF invoice for a finalized bill.",
        "input_schema": {"type": "object", "properties": {"bill_id": {"type": "integer"}}, "required": ["bill_id"]},
    },
    {
        "name": "generate_analysis_deck",
        "description": "Produce a PPTX sales analysis deck with charts for a date range.",
        "input_schema": {"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}, "required": ["start_date", "end_date"]},
    },
    {
        "name": "set_preference",
        "description": "Save a standing owner preference (e.g. default payment, shop GSTIN). Persists across /new sessions.",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]},
    },
]
