#!/usr/bin/env python3
"""
wayfair_orders_sync.py
----------------------
Keo don Wayfair (CG + DS) cua NGAY HOM TRUOC (theo UTC) -> Google Sheet tab "Orders" -> Supabase.

Khung ngay: [hom_qua 00:00:00 UTC, hom_nay 00:00:00 UTC).
  - Chay 01:30 UTC (= 08:30 sang VN) thi ngay hom truoc UTC da dong so (00:00 UTC = 07:00 VN).
  - Don nao poDate da qua ngay moi UTC -> mai moi lay.
  - Muon lay 1 ngay cu the: set WF_TARGET_DATE=YYYY-MM-DD (UTC).

Sheet: dien theo TEN COT o dong header (dong 1) cua tab dich. Cot API khong co -> de trong.
Supabase: upsert bang daily_orders cho dashboard.
"""

import os
import re
import sys
import json
import time
import datetime as dt
from typing import Any, Optional

import requests
import gspread
from google.oauth2.service_account import Credentials

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
ENV = os.getenv("WF_ENV", "sandbox").lower()
CLIENT_ID = os.getenv("WF_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("WF_CLIENT_SECRET", "")

WF_TARGET_DATE = os.getenv("WF_TARGET_DATE", "").strip()   # optional YYYY-MM-DD (UTC)
PAGE_LIMIT = int(os.getenv("WF_PAGE_LIMIT", "25"))

SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "Orders")        # dien thang vao tab Orders
GSA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GSA_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "daily_orders")
PUSH_SUPABASE = os.getenv("WF_PUSH_SUPABASE", "1") == "1"

if ENV == "production":
    GRAPHQL_URL = "https://api.wayfair.com/v1/graphql"
    OAUTH_AUDIENCE = "https://api.wayfair.com"
else:
    GRAPHQL_URL = "https://sandbox.api.wayfair.com/v1/graphql"
    OAUTH_AUDIENCE = "https://sandbox.api.wayfair.com"

OAUTH_URL = "https://sso.auth.wayfair.com/oauth/token"

DEFAULT_HEADERS = [
    "Revenue", "Warehouse Name", "Castlegate Rate", "Store Name", "PO Number",
    "PO Date", "Must Ship By", "Backorder Date", "Order Status", "Item Number",
    "Item Name", "Quantity", "Wholesale Price", "Ship Method", "Carrier Name",
    "Shipping Account", "Ship To Name", "Ship To Address 1", "Ship To Address 2",
    "Ship To City", "Ship To State", "Ship To Zip", "Ship To Country", "Customer Email",
]

HEADER_TO_KEY = {
    "revenue": "revenue", "warehouse name": "warehouse_name",
    "castlegate rate": "castlegate_rate", "store name": "store_name",
    "po number": "po_number", "po date": "po_date_display",
    "must ship by": "must_ship_display", "backorder date": "backorder_date",
    "order status": "order_status", "item number": "item_number",
    "item name": "item_name", "quantity": "quantity",
    "wholesale price": "wholesale_price", "ship method": "ship_method",
    "carrier name": "carrier_name", "shipping account": "shipping_account",
    "ship to name": "ship_to_name",
    "ship to address 1": "ship_to_address1", "ship to address": "ship_to_address1",
    "ship to address1": "ship_to_address1",
    "ship to address 2": "ship_to_address2", "ship to address2": "ship_to_address2",
    "ship to city": "ship_to_city", "ship to state": "ship_to_state",
    "ship to zip": "ship_to_zip", "ship to zip code": "ship_to_zip",
    "ship to postal code": "ship_to_zip", "ship to country": "ship_to_country",
    "customer email": "customer_email",
}

CARRIER_NAMES = {
    "FDEG": "FedEx", "FDE": "FedEx", "FEDX": "FedEx", "FDEN": "FedEx",
    "UPSN": "UPS", "UPSG": "UPS", "UPS": "UPS",
    "USPS": "USPS", "ONTRAC": "OnTrac", "LASX": "LaserShip",
}


def _need(value: str, name: str) -> str:
    if not value:
        print(f"[config] THIEU bien '{name}'. Kiem tra file .env.", file=sys.stderr)
        sys.exit(1)
    return value


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", str(h).strip().lower())


def _num(x):
    return int(x) if float(x).is_integer() else round(float(x), 2)


def _parse_dt(iso: str) -> Optional[dt.datetime]:
    """Tra ve datetime NAIVE (coi nhu UTC)."""
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00").split(".")[0].split("+")[0])
    except Exception:
        return None


def _iso_date(iso: str) -> str:
    return (str(iso)[:10]) if iso else ""


def _mdy(iso: str, pad: bool) -> str:
    d = _parse_dt(iso)
    if not d:
        return ""
    return d.strftime("%m/%d/%Y") if pad else f"{d.month}/{d.day}/{d.year}"


def _bump_iso(iso: str) -> str:
    d = _parse_dt(iso) or dt.datetime.utcnow()
    return (d + dt.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def target_window() -> tuple[dt.datetime, dt.datetime]:
    """Tra ve (start, end) NAIVE UTC cho ngay can lay (mac dinh: hom qua UTC)."""
    if WF_TARGET_DATE:
        day = dt.date.fromisoformat(WF_TARGET_DATE)
    else:
        day = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    start = dt.datetime(day.year, day.month, day.day)   # 00:00:00 UTC (naive)
    return start, start + dt.timedelta(days=1)


# ─────────────────────────────────────────────────────────────
# 1. AUTH
# ─────────────────────────────────────────────────────────────
def get_token() -> str:
    _need(CLIENT_ID, "WF_CLIENT_ID")
    _need(CLIENT_SECRET, "WF_CLIENT_SECRET")
    r = requests.post(OAUTH_URL, json={
        "grant_type": "client_credentials", "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET, "audience": OAUTH_AUDIENCE,
    }, headers={"Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    print(f"[auth] token OK ({ENV})")
    return r.json()["access_token"]


# ─────────────────────────────────────────────────────────────
# 2. GRAPHQL
# ─────────────────────────────────────────────────────────────
def products_fragment() -> str:
    fields = ["partNumber", "sku", "name", "quantity", "price", "totalCost"]
    if ENV == "production":
        fields.append("isCancelled")
    return "products { " + " ".join(fields) + " }"


def build_query(root_field: str) -> str:
    return f"""
query {root_field}($limit: Int32, $hasResponse: Boolean, $fromDate: IsoDateTime, $poNumbers: [String], $sortOrder: SortOrder) {{
  {root_field}(limit: $limit, hasResponse: $hasResponse, fromDate: $fromDate, poNumbers: $poNumbers, sortOrder: $sortOrder) {{
    id
    poNumber
    poDate
    orderId
    supplierId
    supplierName
    salesChannelName
    orderType
    estimatedShipDate
    customerName
    customerEmail
    customerAddress1
    customerAddress2
    customerCity
    customerState
    customerPostalCode
    customerCountry
    warehouse {{ name }}
    shippingInfo {{ shipSpeed carrierCode }}
    shipTo {{ name address1 address2 city state country postalCode }}
    {products_fragment()}
  }}
}}
"""


def gql(token: str, query: str, variables: dict) -> Any:
    r = requests.post(GRAPHQL_URL, json={"query": query, "variables": variables},
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"}, timeout=45)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(body['errors'])[:600]}")
    return body["data"]


def fetch_day(token: str, root_field: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Lay don co poDate trong [start, end) theo UTC. Phan trang ASC + tu dung khi qua 'end'."""
    query = build_query(root_field)
    out, seen, stop = [], set(), False
    cursor = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    for _ in range(200):
        data = gql(token, query, {"limit": PAGE_LIMIT, "fromDate": cursor, "sortOrder": "ASC"})
        batch = data.get(root_field) or []
        if not batch:
            break
        for po in batch:
            d = _parse_dt(po.get("poDate"))
            if d is None:
                continue
            if d >= end:            # sang ngay moi UTC -> bo & danh dau dung
                stop = True
                continue
            if d < start:
                continue
            if po["poNumber"] in seen:
                continue
            seen.add(po["poNumber"])
            out.append(po)
        if stop or len(batch) < PAGE_LIMIT:
            break
        cursor = _bump_iso(max(po["poDate"] for po in batch))
        time.sleep(3.0)
    print(f"[fetch] {root_field}: {len(out)} PO trong khung ngay")
    return out


# ─────────────────────────────────────────────────────────────
# 3. FLATTEN
# ─────────────────────────────────────────────────────────────
def to_rows(pos: list[dict], source: str) -> list[dict]:
    rows = []
    for po in pos:
        ship = po.get("shipTo") or {}
        wh = po.get("warehouse") or {}
        shp = po.get("shippingInfo") or {}
        carrier_code = (shp.get("carrierCode") or "")
        # Cot D (Store Name): CG -> "SingleChannel", DS -> "Wayfair" (hoac "Wayfair.ca" neu Canada)
        _chan = (po.get("salesChannelName") or "")
        _is_ca = ("canad" in _chan.lower()) or (".ca" in _chan.lower())
        if source == "castlegate":
            store_name = "SingleChannel"
        else:
            store_name = "Wayfair.ca" if _is_ca else "Wayfair"
        for p in (po.get("products") or []):
            if p.get("isCancelled"):
                continue
            qty = _num(p.get("quantity") or 0)
            price = _num(p.get("price") or 0)
            revenue = _num(p["totalCost"]) if p.get("totalCost") is not None else _num(float(qty) * float(price))
            rows.append({
                "revenue": revenue,
                "warehouse_name": wh.get("name") or po.get("supplierName", "") or "",
                "castlegate_rate": "",
                "store_name": store_name,
                "po_number": po.get("poNumber", "") or "",
                "po_date_display": _mdy(po.get("poDate", ""), pad=False),
                "must_ship_display": _mdy(po.get("estimatedShipDate", ""), pad=True),
                "backorder_date": "",
                "order_status": "",
                "item_number": p.get("partNumber", "") or "",
                "item_name": p.get("name", "") or "",
                "quantity": qty,
                "wholesale_price": price,
                "ship_method": "",
                "carrier_name": CARRIER_NAMES.get(carrier_code.upper(), carrier_code),
                "shipping_account": "",
                "ship_to_name": ship.get("name") or po.get("customerName", "") or "",
                "ship_to_address1": ship.get("address1") or po.get("customerAddress1", "") or "",
                "ship_to_address2": ship.get("address2") or po.get("customerAddress2", "") or "",
                "ship_to_city": ship.get("city") or po.get("customerCity", "") or "",
                "ship_to_state": ship.get("state") or po.get("customerState", "") or "",
                "ship_to_zip": ship.get("postalCode") or po.get("customerPostalCode", "") or "",
                "ship_to_country": ship.get("country") or po.get("customerCountry", "") or "",
                "customer_email": po.get("customerEmail", "") or "",
                "po_date_iso": _iso_date(po.get("poDate", "")),
                "source": source,
                "sku": p.get("sku", "") or "",
            })
    return rows


# ─────────────────────────────────────────────────────────────
# 4. GOOGLE SHEET
# ─────────────────────────────────────────────────────────────
def _gsa_info() -> dict:
    if GSA_FILE:
        with open(GSA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    if GSA_JSON:
        return json.loads(GSA_JSON)
    print("[config] THIEU GOOGLE_SERVICE_ACCOUNT_FILE hoac _JSON.", file=sys.stderr)
    sys.exit(1)


def sheet_client():
    creds = Credentials.from_service_account_info(
        _gsa_info(), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds)


def _key(po: str, item: str) -> str:
    return f"{po}::{item}"


def _col_letter(idx0: int) -> str:
    """0-based col index -> chu cot A1 (0->A, 12->M...)."""
    n, s = idx0 + 1, ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def write_sheet(rows: list[dict]) -> None:
    """CHI CHEN dong moi xuong duoi (khong xoa/ghi de). Revenue = cong thuc =Qty*WholesalePrice."""
    _need(SHEET_ID, "GOOGLE_SHEET_ID")
    gc = sheet_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=5000, cols=max(30, len(DEFAULT_HEADERS)))

    existing = ws.get_all_values()
    header_exists = bool(existing and any(c.strip() for c in existing[0]))
    header = existing[0] if header_exists else DEFAULT_HEADERS
    data = existing[1:] if header_exists else []
    norm_header = [_norm(h) for h in header]

    def idx_of(key):
        return next((i for i, nh in enumerate(norm_header) if HEADER_TO_KEY.get(nh) == key), None)

    po_idx, item_idx = idx_of("po_number"), idx_of("item_number")
    qty_idx, price_idx, rev_idx = idx_of("quantity"), idx_of("wholesale_price"), idx_of("revenue")

    # key da co san trong tab -> khong chen lai (tranh trung)
    seen = set()
    if po_idx is not None and item_idx is not None:
        for r in data:
            if len(r) > max(po_idx, item_idx) and r[po_idx]:
                seen.add(_key(r[po_idx], r[item_idx]))

    new = [row for row in rows if _key(row["po_number"], row["item_number"]) not in seen]
    if not new:
        print(f"[sheet] '{SHEET_TAB}': khong co dong moi de chen (da co san)")
        return

    start_row = (len(existing) + 1) if header_exists else 1
    first_data_row = start_row if header_exists else 2
    matrix = [] if header_exists else [header]

    for i, row in enumerate(new):
        abs_row = first_data_row + i
        aligned = [str(row.get(HEADER_TO_KEY.get(nh, ""), "")) for nh in norm_header]
        if rev_idx is not None and qty_idx is not None and price_idx is not None:
            aligned[rev_idx] = f"={_col_letter(qty_idx)}{abs_row}*{_col_letter(price_idx)}{abs_row}"
        matrix.append(aligned)

    ws.update(values=matrix, range_name=f"A{start_row}", value_input_option="USER_ENTERED")
    print(f"[sheet] '{SHEET_TAB}': CHEN THEM {len(new)} dong (tu dong {first_data_row}), giu nguyen data cu")


# ─────────────────────────────────────────────────────────────
# 5. SUPABASE
# ─────────────────────────────────────────────────────────────
def push_supabase(rows: list[dict]) -> None:
    _need(SUPABASE_URL, "SUPABASE_URL")
    _need(SUPABASE_SERVICE_KEY, "SUPABASE_SERVICE_KEY")
    if not rows:
        return
    payload = [{
        "po_number": r["po_number"], "po_date_iso": r["po_date_iso"],
        "source": r["source"], "item_number": r["item_number"],
        "item_name": r["item_name"], "quantity": r["quantity"], "revenue": r["revenue"],
    } for r in rows]
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=po_number,item_number"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    for i in range(0, len(payload), 500):
        r = requests.post(url, headers=headers, data=json.dumps(payload[i:i+500]), timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase {r.status_code}: {r.text[:400]}")
    print(f"[supabase] upsert {len(payload)} dong -> {SUPABASE_TABLE}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main() -> None:
    start, end = target_window()
    print(f"[run] env={ENV} | lay don UTC ngay {start.date()} (khung [{start} , {end}))")

    token = get_token()
    cg = fetch_day(token, "getCastleGatePurchaseOrders", start, end)
    time.sleep(3.0)
    ds = fetch_day(token, "getDropshipPurchaseOrders", start, end)

    rows = to_rows(cg, "castlegate") + to_rows(ds, "dropship")
    print(f"[flatten] tong {len(rows)} line items")
    if not rows:
        print("[run] khong co don trong khung ngay. Xong.")
        return

    write_sheet(rows)
    if PUSH_SUPABASE:
        push_supabase(rows)
    else:
        print("[supabase] WF_PUSH_SUPABASE=0 -> bo qua")
    print("[run] DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
