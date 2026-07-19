import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def sales_range(conn, owner_id, start_date, end_date):
    """start_date/end_date: 'YYYY-MM-DD'. Only counts finalized bills --
    drafts and cancellations are correctly invisible here."""
    totals = conn.execute(
        """SELECT COALESCE(SUM(grand_total),0) as total_sales,
                  COALESCE(SUM(cgst_total),0) + COALESCE(SUM(sgst_total),0) as tax_collected,
                  COUNT(*) as bill_count
           FROM bills
           WHERE owner_id=? AND status='finalized'
             AND date(finalized_at) BETWEEN date(?) AND date(?)""",
        (owner_id, start_date, end_date),
    ).fetchone()

    by_mode = conn.execute(
        """SELECT payment_mode, COALESCE(SUM(grand_total),0) as total
           FROM bills WHERE owner_id=? AND status='finalized'
             AND date(finalized_at) BETWEEN date(?) AND date(?)
           GROUP BY payment_mode""",
        (owner_id, start_date, end_date),
    ).fetchall()

    top_items = conn.execute(
        """SELECT bi.product_name_snapshot as product, SUM(bi.qty) as qty_sold,
                  SUM(bi.line_total) as revenue
           FROM bill_items bi
           JOIN bills b ON b.id = bi.bill_id
           WHERE b.owner_id=? AND b.status='finalized'
             AND date(b.finalized_at) BETWEEN date(?) AND date(?)
           GROUP BY bi.product_id ORDER BY qty_sold DESC LIMIT 10""",
        (owner_id, start_date, end_date),
    ).fetchall()

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_sales": totals["total_sales"],
        "tax_collected": round(totals["tax_collected"], 2),
        "bill_count": totals["bill_count"],
        "by_payment_mode": {r["payment_mode"] or "unspecified": r["total"] for r in by_mode},
        "top_items": [dict(r) for r in top_items],
    }


def daily_close(conn, owner_id, date=None):
    import datetime
    date = date or datetime.date.today().isoformat()
    return sales_range(conn, owner_id, date, date)
