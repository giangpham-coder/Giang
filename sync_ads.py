#!/usr/bin/env python3
"""Wayfair Advertising LISTING_REPORT -> upsert vao ads_listing_daily (Supabase).
Chay 1 lan/ngay. Luon keo lai 14 ngay gan nhat vi attribution 14 ngay con thay doi.
Env: WAYFAIR_CLIENT_ID, WAYFAIR_CLIENT_SECRET, SUPABASE_URL, SUPABASE_KEY
Tuy chon: ADS_DAYS (mac dinh 14)
"""
import csv, io, os, sys, time
from datetime import date, timedelta

import requests
from supabase import create_client
from sync_inventory_live import get_access_token  # dung lai ham auth da test

BASE = "https://api.wayfair.io/advertising/v1"
DAYS = int(os.environ.get("ADS_DAYS", "14"))


def num(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None  # NO DAILY CAP, NO LIFETIME BUDGET...


def integer(v):
    n = num(v)
    return int(n) if n is not None else None


def dt(v):
    v = (v or "").strip()
    return v if len(v) == 10 and v[4] == "-" else None  # NO END DATE -> None


def boolean(v):
    return (v or "").strip().lower() == "true"


def request_report(headers, start, end):
    body = {
        "name": f"ads_listing_{start}_{end}",
        "reportType": "LISTING_REPORT",
        "program": "WSP",
        "groupBy": "DAY",
        "fileType": "CSV",
        "attributionWindow": 14,
        "filters": {"startDate": str(start), "endDate": str(end)},
    }
    r = requests.post(f"{BASE}/reports", headers=headers, json=body, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Generate loi {r.status_code}: {r.text}")
    return r.json()["id"]


def wait_report(headers, rid, max_wait=1800):
    waited = 0
    while waited < max_wait:
        time.sleep(5)
        waited += 5
        s = requests.get(f"{BASE}/reports", headers=headers, params={"reportId": rid}, timeout=30)
        if s.status_code == 429:
            time.sleep(int(s.headers.get("Retry-After", "10")))
            continue
        if not s.ok:
            raise RuntimeError(f"Poll loi {s.status_code}: {s.text}")
        j = s.json()
        if j.get("status") == "COMPLETED":
            return j["url"]
        if j.get("status") in ("FAILED", "ERROR"):
            raise RuntimeError(f"Report that bai: {j}")
    raise RuntimeError("Het thoi gian cho report")


def to_row(r):
    return {
        "report_date": dt(r["Date"]),
        "campaign_id": r["campaign_id"],
        "listing": r["listing"],
        "is_b2b": boolean(r["isB2b"]),
        "campaign_name": r["campaign_name"],
        "campaign_status": r["campaign_status"],
        "campaign_is_active": boolean(r["campaign_is_active"]),
        "daily_cap": num(r["campaign_daily_cap_USD"]),
        "lifetime_budget": num(r["campaign_lifetime_budget_USD"]),
        "campaign_start_date": dt(r["campaign_start_date"]),
        "campaign_end_date": dt(r["campaign_end_date"]),
        "bidding_strategy": r["bidding_strategy"],
        "target_roas_pct": num(r["target_roas_percentage"]),
        "product_name": r["product_name"],
        "default_bid": num(r["product_default_bid"]),
        "product_status": r["product_status"],
        "part_numbers": r["first_10_part_numbers"],
        "class_name": r["Class Name"],
        "store_url": r["store_url"],
        "clicks": integer(r["clicks"]),
        "impressions": integer(r["impressions"]),
        "spend": num(r["spend_USD"]),
        "cpc": num(r["cost_per_click_USD"]),
        "ctr_pct": num(r["click_through_rate_%"]),
        "sales_14d": num(r["attributed_retail_sales_window_view_through_USD_Day_14"]),
        "wholesale_cost_14d": num(r["attributed_wholesale_cost_window_view_through_USD_Day_14"]),
        "orders_14d": integer(r["attributed_orders_window_view_through_Day_14"]),
        "units_14d": integer(r["attributed_units_window_view_through_Day_14"]),
        "cost_per_order_14d": num(r["cost_per_attributed_order_window_view_through_USD_Day_14"]),
        "retail_roas_14d": num(r["retail_roas_window_view_through_Day_14"]),
        "wholesale_roas_14d": num(r["wholesale_cost_roas_window_view_through_Day_14"]),
        "updated_at": "now()",
    }


def main():
    cid = os.environ.get("WAYFAIR_CLIENT_ID")
    sec = os.environ.get("WAYFAIR_CLIENT_SECRET")
    if not cid or not sec:
        print("Thieu WAYFAIR_CLIENT_ID / WAYFAIR_CLIENT_SECRET", file=sys.stderr)
        sys.exit(1)
    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    token = get_access_token(cid, sec)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=DAYS - 1)
    print(f"[ads] Keo report {start} -> {end}")

    rid = request_report(headers, start, end)
    url = wait_report(headers, rid)
    raw = requests.get(url, timeout=120).content.decode("utf-8-sig")

    dedup = {}
    for r in csv.DictReader(io.StringIO(raw)):
        row = to_row(r)
        if not row["report_date"] or not row["listing"]:
            continue
        key = (row["report_date"], row["campaign_id"], row["listing"], row["is_b2b"])
        dedup[key] = row
    rows = list(dedup.values())

    for i in range(0, len(rows), 500):
        supabase.table("ads_listing_daily").upsert(
            rows[i:i + 500], on_conflict="report_date,campaign_id,listing,is_b2b"
        ).execute()
    print(f"[ads] Upsert {len(rows)} dong vao ads_listing_daily.")


if __name__ == "__main__":
    main()
