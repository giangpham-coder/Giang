#!/usr/bin/env python3
"""Wayfair Advertising API -> Supabase. Chay 1 lan/ngay (workflow rieng sync_ads.yml).

Keo 3 loai report (da test that, tong khop nhau):
  - LISTING_REPORT / WSP          -> ads_listing_daily   (product targeting, theo listing)
  - KEYWORD_PRODUCT_REPORT / SPN  -> ads_keyword_daily   (keyword targeting, theo tu khoa)
  - CAMPAIGN_REPORT / WSP         -> ads_campaign_daily  (tong theo campaign, co ca 2 loai)

Ngay trong report theo gio My (khop Partner Home) nen ngay ket thuc = hom qua
theo gio Pacific (ngay do da ket thuc han). Luon keo lai ADS_DAYS ngay (mac dinh 21)
vi sales/orders attribution 14 ngay con tang sau khi ngay ket thuc.

Env: WAYFAIR_CLIENT_ID, WAYFAIR_CLIENT_SECRET, SUPABASE_URL, SUPABASE_KEY
Tuy chon: ADS_DAYS (mac dinh 21; backfill co the dat 60-90, tu chia khuc 30 ngay)
"""
import csv, io, os, sys, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from supabase import create_client
import sync_inventory_live as wf  # dung lai ham auth da test

BASE = "https://api.wayfair.io/advertising/v1"
DAYS = int(os.environ.get("ADS_DAYS", "21"))
CHUNK_DAYS = 30
REPORT_TZ = ZoneInfo("America/Los_Angeles")


# ---------------- Chuyen doi gia tri ----------------
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


def text(v):
    return (v or "").strip()


def metrics(r):
    """Cac cot so lieu chung cua ca 3 loai report."""
    return {
        "clicks": integer(r.get("clicks")),
        "impressions": integer(r.get("impressions")),
        "spend": num(r.get("spend_USD")),
        "cpc": num(r.get("cost_per_click_USD")),
        "ctr_pct": num(r.get("click_through_rate_%")),
        "sales_14d": num(r.get("attributed_retail_sales_window_view_through_USD_Day_14")),
        "wholesale_cost_14d": num(r.get("attributed_wholesale_cost_window_view_through_USD_Day_14")),
        "orders_14d": integer(r.get("attributed_orders_window_view_through_Day_14")),
        "units_14d": integer(r.get("attributed_units_window_view_through_Day_14")),
        "cost_per_order_14d": num(r.get("cost_per_attributed_order_window_view_through_USD_Day_14")),
        "retail_roas_14d": num(r.get("retail_roas_window_view_through_Day_14")),
        "wholesale_roas_14d": num(r.get("wholesale_cost_roas_window_view_through_Day_14")),
    }


def campaign_cols(r):
    return {
        "campaign_name": text(r.get("campaign_name")),
        "campaign_status": text(r.get("campaign_status")),
        "campaign_is_active": boolean(r.get("campaign_is_active")),
        "daily_cap": num(r.get("campaign_daily_cap_USD")),
        "lifetime_budget": num(r.get("campaign_lifetime_budget_USD")),
        "campaign_start_date": dt(r.get("campaign_start_date")),
        "campaign_end_date": dt(r.get("campaign_end_date")),
    }


# ---------------- Row builders ----------------
def listing_row(r):
    row = {
        "report_date": dt(r.get("Date")),
        "campaign_id": text(r.get("campaign_id")),
        "listing": text(r.get("listing")),
        "is_b2b": boolean(r.get("isB2b")),
        **campaign_cols(r),
        "bidding_strategy": text(r.get("bidding_strategy")),
        "target_roas_pct": num(r.get("target_roas_percentage")),
        "product_name": text(r.get("product_name")),
        "default_bid": num(r.get("product_default_bid")),
        "product_status": text(r.get("product_status")),
        "part_numbers": text(r.get("first_10_part_numbers")),
        "class_name": text(r.get("Class Name")),
        "store_url": text(r.get("store_url")),
        **metrics(r),
        "updated_at": "now()",
    }
    return row if row["report_date"] and row["listing"] else None


def campaign_row(r):
    row = {
        "report_date": dt(r.get("Date")),
        "campaign_id": text(r.get("campaign_id")),
        "is_b2b": boolean(r.get("isB2b")),
        "targeting_type": text(r.get("targeting_type")),
        **campaign_cols(r),
        "bidding_strategy": text(r.get("bidding_strategy")),
        "target_roas_pct": num(r.get("target_roas_percentage")),
        "store_url": text(r.get("store_url")),
        **metrics(r),
        "updated_at": "now()",
    }
    return row if row["report_date"] and row["campaign_id"] else None


def keyword_row(r):
    m = metrics(r)
    m["ecpm"] = num(r.get("effective_cost_per_thousand_impressions_USD"))
    row = {
        "report_date": dt(r.get("Date")),
        "campaign_id": text(r.get("campaign_id")),
        "listing": text(r.get("listing")),
        "keyword_id": text(r.get("keyword_id")),
        "keyword_match_type": text(r.get("keyword_match_type")),
        "keyword_value": text(r.get("keyword_value")),
        "keyword_default_bid": num(r.get("keyword_default_bid")),
        **campaign_cols(r),
        "product_name": text(r.get("product_name")),
        "class_name": text(r.get("Class Name")),
        "store_url": text(r.get("store_url")),
        **m,
        "updated_at": "now()",
    }
    return row if row["report_date"] and row["campaign_id"] else None


REPORTS = {
    "listing": dict(reportType="LISTING_REPORT", program="WSP", table="ads_listing_daily",
                    conflict="report_date,campaign_id,listing,is_b2b", build=listing_row),
    "campaign": dict(reportType="CAMPAIGN_REPORT", program="WSP", table="ads_campaign_daily",
                     conflict="report_date,campaign_id,is_b2b,targeting_type", build=campaign_row),
    "keyword": dict(reportType="KEYWORD_PRODUCT_REPORT", program="SPN", table="ads_keyword_daily",
                    conflict="report_date,campaign_id,listing,keyword_id,keyword_match_type", build=keyword_row),
}


# ---------------- Goi API ----------------
def auth_headers(cid, sec):
    token = wf.get_access_token(cid, sec)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def request_report(headers, name, cfg, start, end):
    body = {
        "name": f"ads_{name}_{start}_{end}",
        "reportType": cfg["reportType"],
        "program": cfg["program"],
        "groupBy": "DAY",
        "fileType": "CSV",
        "attributionWindow": 14,
        "filters": {"startDate": str(start), "endDate": str(end)},
    }
    r = requests.post(f"{BASE}/reports", headers=headers, json=body, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Generate loi {r.status_code}: {r.text}")
    return r.json()["id"]


def wait_reports(headers, ids, max_wait=1800):
    """Poll tat ca report cung luc. Tra ve (urls, loi)."""
    pending, urls, errors = dict(ids), {}, {}
    waited = 0
    while pending and waited < max_wait:
        time.sleep(5)
        waited += 5
        for name, rid in list(pending.items()):
            s = requests.get(f"{BASE}/reports", headers=headers, params={"reportId": rid}, timeout=30)
            if s.status_code == 429:
                time.sleep(int(s.headers.get("Retry-After", "10")))
                continue
            if not s.ok:
                errors[name] = f"Poll loi {s.status_code}: {s.text}"
                pending.pop(name)
                continue
            j = s.json()
            st = j.get("status")
            if st == "COMPLETED":
                urls[name] = j["url"]
                pending.pop(name)
            elif st in ("FAILED", "ERROR"):
                errors[name] = f"Report that bai: {j}"
                pending.pop(name)
    for name in pending:
        errors[name] = "Het thoi gian cho report"
    return urls, errors


def load_rows(name, url):
    cfg = REPORTS[name]
    raw = requests.get(url, timeout=120).content.decode("utf-8-sig")
    keys = cfg["conflict"].split(",")
    dedup = {}
    for r in csv.DictReader(io.StringIO(raw)):
        row = cfg["build"](r)
        if row:
            dedup[tuple(row[k] for k in keys)] = row
    return list(dedup.values())


def upsert(supabase, name, rows):
    cfg = REPORTS[name]
    for i in range(0, len(rows), 500):
        supabase.table(cfg["table"]).upsert(rows[i:i + 500], on_conflict=cfg["conflict"]).execute()


def date_chunks(start, end, size):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=size - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def main():
    cid = os.environ.get("WAYFAIR_CLIENT_ID")
    sec = os.environ.get("WAYFAIR_CLIENT_SECRET")
    if not cid or not sec:
        print("Thieu WAYFAIR_CLIENT_ID / WAYFAIR_CLIENT_SECRET", file=sys.stderr)
        sys.exit(1)
    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    headers = auth_headers(cid, sec)

    # Ngay Pacific da ket thuc han (Partner Home + report API deu theo ngay My)
    end = datetime.now(REPORT_TZ).date() - timedelta(days=1)
    start = end - timedelta(days=DAYS - 1)
    print(f"[ads] Keo {start} -> {end} ({DAYS} ngay, theo gio Pacific)")

    failures = []
    for c_start, c_end in date_chunks(start, end, CHUNK_DAYS):
        ids = {}
        for name, cfg in REPORTS.items():
            try:
                ids[name] = request_report(headers, name, cfg, c_start, c_end)
            except Exception as e:
                failures.append(name)
                print(f"[ads] {name}: {e}", file=sys.stderr)
        urls, errors = wait_reports(headers, ids)
        for name, msg in errors.items():
            failures.append(name)
            print(f"[ads] {name}: {msg}", file=sys.stderr)
        for name, url in urls.items():
            try:
                rows = load_rows(name, url)
                upsert(supabase, name, rows)
                spend = sum(r["spend"] or 0 for r in rows)
                print(f"[ads] {c_start}->{c_end} {name}: upsert {len(rows)} dong vao {REPORTS[name]['table']} (spend ${spend:,.2f})")
            except Exception as e:
                failures.append(name)
                print(f"[ads] {name}: {e}", file=sys.stderr)

    if failures:
        print(f"[ads] Co loi o: {sorted(set(failures))}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
