"""
Pure GST math. Deliberately has zero dependency on DB/agent/telegram so it
can be trusted in isolation before it's wired into anything.

Rule: intra-state sale => CGST + SGST, each exactly half the total GST rate.
Rounding: round each line to 2 decimal paise at the line level (not just at
the bill total) — this is what Indian GST invoices actually do, and it's
also what stops rounding drift from accumulating across many lines.
"""
from decimal import Decimal, ROUND_HALF_UP


def _round2(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_line_gst(unit_price: float, qty: float, gst_rate: float) -> dict:
    """
    unit_price: pre-tax sell price per unit (this codebase treats sell_price
                as the pre-tax/base price; GST is added on top — adjust here
                if your product data models MRP as tax-inclusive instead).
    gst_rate:   e.g. 5, 12, 18 (percent). 0 is valid (loose atta/rice/produce).
    """
    price = Decimal(str(unit_price))
    quantity = Decimal(str(qty))
    rate = Decimal(str(gst_rate))

    base = price * quantity
    total_gst = base * rate / Decimal("100")
    cgst = total_gst / 2
    sgst = total_gst / 2
    line_total = base + total_gst

    return {
        "base_amount": _round2(base),
        "cgst_amt": _round2(cgst),
        "sgst_amt": _round2(sgst),
        "gst_rate": float(rate),
        "line_total": _round2(line_total),
    }


def compute_bill_totals(line_items: list[dict]) -> dict:
    """line_items: list of dicts each already containing base_amount/cgst_amt/sgst_amt/line_total."""
    subtotal = sum(Decimal(str(i["base_amount"])) for i in line_items)
    cgst_total = sum(Decimal(str(i["cgst_amt"])) for i in line_items)
    sgst_total = sum(Decimal(str(i["sgst_amt"])) for i in line_items)
    grand_total = subtotal + cgst_total + sgst_total
    return {
        "subtotal": _round2(subtotal),
        "cgst_total": _round2(cgst_total),
        "sgst_total": _round2(sgst_total),
        "grand_total": _round2(grand_total),
    }
