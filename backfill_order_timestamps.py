#!/usr/bin/env python3
"""Backfill bang order_timestamps cho cac ngay UTC cu. CHI ghi vao order_timestamps,
KHONG dong toi Google Sheet / daily_orders.

Env bat buoc: WF_ENV=production, WF_CLIENT_ID, WF_CLIENT_SECRET, SUPABASE_URL, SUPABASE_SERVICE_KEY
Tuy chon:
  BACKFILL_FROM  YYYY-MM-DD (UTC, mac dinh 2026-09-01)
  BACKFILL_TO    YYYY-MM-DD (UTC, mac dinh hom qua UTC)
"""
import datetime as dt
import os
import sys
import time

import wayfair_orders_sync as wos


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    d_from = dt.date.fromisoformat(os.getenv("BACKFILL_FROM", "2026-09-01"))
    d_to = dt.date.fromisoformat(os.getenv("BACKFILL_TO") or str(today - dt.timedelta(days=1)))
    print(f"[backfill] {d_from} -> {d_to} (UTC)")

    token = wos.get_token()
    day, total, failed = d_from, 0, []
    while day <= d_to:
        start = dt.datetime(day.year, day.month, day.day)
        end = start + dt.timedelta(days=1)
        try:
            cg = wos.safe_fetch(token, "getCastleGatePurchaseOrders", start, end)
            time.sleep(3.0)
            ds = wos.safe_fetch(token, "getDropshipPurchaseOrders", start, end)
            rows = wos.timestamp_rows(cg, "castlegate") + wos.timestamp_rows(ds, "dropship")
            wos.push_timestamps(rows)
            total += len(rows)
            print(f"[backfill] {day}: {len(rows)} PO")
        except Exception as e:
            failed.append(str(day))
            print(f"[backfill] {day} LOI: {type(e).__name__}: {e}", file=sys.stderr)
            try:
                token = wos.get_token()   # co the token het han
            except Exception:
                pass
        day += dt.timedelta(days=1)
        time.sleep(2.0)

    print(f"[backfill] xong, tong {total} PO. Ngay loi: {failed or 'khong'}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
