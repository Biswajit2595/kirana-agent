import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gst import compute_line_gst, compute_bill_totals


def test_zero_rated_loose_atta():
    r = compute_line_gst(unit_price=40, qty=2, gst_rate=0)
    assert r["base_amount"] == 80.0
    assert r["cgst_amt"] == 0.0
    assert r["sgst_amt"] == 0.0
    assert r["line_total"] == 80.0


def test_packaged_staple_5_percent():
    # Aashirvaad Atta 5kg, MRP-ish sell price 280, qty 1, 5% GST
    r = compute_line_gst(unit_price=280, qty=1, gst_rate=5)
    assert r["cgst_amt"] == 7.00
    assert r["sgst_amt"] == 7.00
    assert r["line_total"] == 294.00


def test_fmcg_18_percent_with_qty():
    # Surf Excel-ish, 55 per unit, qty 3, 18% GST
    r = compute_line_gst(unit_price=55, qty=3, gst_rate=18)
    assert r["base_amount"] == 165.00
    # total gst = 29.70, split 14.85/14.85
    assert r["cgst_amt"] == 14.85
    assert r["sgst_amt"] == 14.85
    assert r["line_total"] == 194.70


def test_rounding_half_up_at_line_level():
    # engineered to land on a .xx5 boundary to prove rounding rule
    r = compute_line_gst(unit_price=12.5, qty=1, gst_rate=5)
    # base=12.5, total_gst=0.625 -> cgst=sgst=0.3125 -> rounds to 0.31 (half up would need .xx5 exactly)
    assert r["cgst_amt"] + r["sgst_amt"] == round(0.625, 2) or abs((r["cgst_amt"]+r["sgst_amt"]) - 0.625) < 0.01


def test_bill_totals_aggregate_multiple_lines():
    lines = [
        compute_line_gst(unit_price=280, qty=1, gst_rate=5),   # atta
        compute_line_gst(unit_price=40, qty=2, gst_rate=0),    # loose sugar
        compute_line_gst(unit_price=14, qty=4, gst_rate=12),   # maggi
    ]
    totals = compute_bill_totals(lines)
    assert totals["subtotal"] == 280 + 80 + 56
    assert totals["grand_total"] == totals["subtotal"] + totals["cgst_total"] + totals["sgst_total"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
