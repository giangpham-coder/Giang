"""
sync_google_sheet.py
---------------------
Doc nhieu tab tu Google Sheet "Giang Wf" va upsert vao Supabase.
Cac bang duoc dong bo trong ban nay: products, pricing, supply_chain_stock,
inbound, monthly_sales.

CHUA lam (can Giang xac nhan them):
    - Forecast, Dat hang: cot la khoang ngay dong (rolling), can xu ly rieng
    - INBOUND: chi dong bo "danh sach chinh" (Part Number/Active Date/Stage)
      theo yeu cau cua Giang, chua dong bo bang chi tiet lot/invoice

Cach chay:
    pip install gspread google-auth supabase --break-system-packages
    python sync_google_sheet.py

Can chuan bi truoc (chua co = chua chay duoc):
    1. GOOGLE_SERVICE_ACCOUNT_JSON -> duong dan file key .json cua service account
    2. Share sheet "Giang Wf" cho email cua service account (quyen Viewer)
    3. SUPABASE_URL, SUPABASE_KEY -> Supabase project settings > API
"""

import os
import re
import gspread
from google.oauth2.service_account import Credentials
from supabase import create_client

# ------------------------------ CONFIG ------------------------------
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SHEET_ID = "1L7fZWzg71BqqrnajCAwTP-10-946ryZDKHVqoiDvGb0"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

_MONTH_MAP = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


def _to_float(value):
    if value in (None, "", "-"):
        return None
    try:
        cleaned = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


_SHEET_ERROR_TOKENS = ("#REF!", "#ERROR!", "#N/A", "#VALUE!", "#NAME?", "#NULL!", "#DIV/0!", "Loading...")


def _normalize_month(month_str):
    """Chuan hoa 'M/YYYY' hoac 'M/YY' ve dang 'YYYY-MM' de cac tab khac nhau
    (Ads dung M/YYYY, Return dung M/YY) khop duoc voi nhau."""
    if not month_str:
        return None
    try:
        parts = month_str.strip().split("/")
        if len(parts) != 2:
            return None
        m, y = int(parts[0]), int(parts[1])
        if y < 100:
            y += 2000
        return f"{y:04d}-{m:02d}"
    except (ValueError, IndexError):
        return None


def _parse_us_date_to_iso(date_str):
    """Chuyen 'M/D/YYYY' (dang text tu sheet) sang 'YYYY-MM-DD' de Postgres
    sort/filter dung kieu ngay. Tra ve None neu khong parse duoc."""
    if not date_str:
        return None
    try:
        parts = date_str.strip().split("/")
        if len(parts) != 3:
            return None
        m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{y:04d}-{m:02d}-{d:02d}"
    except (ValueError, IndexError):
        return None


def _row_has_error(row):
    """Phat hien loi tam thoi cua IMPORTRANGE (VD khi file nguon dang bi
    gian doan ket noi). Neu co, nen bo qua ca dong thay vi luu data rac.
    row co the la list (get_all_values) hoac dict (get_all_records)."""
    values = row.values() if isinstance(row, dict) else row
    return any(str(cell).strip() in _SHEET_ERROR_TOKENS for cell in values)


def _get_client():
    creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
    return gspread.authorize(creds)


def _sheet():
    return _get_client().open_by_key(SHEET_ID)


# ============================================================
# products <- tab "Product"
# ============================================================
def sync_products(sh):
    ws = sh.worksheet("Product")
    rows = ws.get_all_values()
    records = []
    for row in rows[1:]:
        if not row:
            continue
        if _row_has_error(row):
            continue
        sku = row[0].strip() if len(row) > 0 else ""
        if not sku:
            continue

        def cell(i):
            return row[i].strip() if i < len(row) else ""

        records.append({
            "sku": sku,
            "product_name": cell(1),
            "product_class": cell(2),
            "asin": cell(3),
            "cogs": _to_float(cell(4)),
            "product_group": cell(9),
        })
    return records


# ============================================================
# pricing <- tab "Price"
# ============================================================
def sync_pricing(sh):
    ws = sh.worksheet("Price")
    rows = ws.get_all_records(expected_headers=[
        "Supplier Part Number", "Product Name", "Status", "SKU",
        "COGS", "Current Base Cost", "Retail Price", "Margin",
    ])
    records = []
    for row in rows:
        if _row_has_error(row):
            continue
        part_no = str(row.get("Supplier Part Number", "")).strip()
        if not part_no:
            continue
        margin_raw = row.get("Margin")
        records.append({
            "supplier_part_number": part_no,
            "sku_internal": str(row.get("SKU", "")).strip(),
            "status": str(row.get("Status", "")).strip(),
            "cogs": _to_float(row.get("COGS")),
            "base_cost": _to_float(row.get("Current Base Cost")),
            "retail_price": _to_float(row.get("Retail Price")),
            "margin_pct": _to_float(margin_raw),
        })
    return records


# ============================================================
# supply_chain_stock <- tab "BC Ton & ASIN"
# Header that nam o row 3 (khong phai row 1), nen doc raw values.
# Cot co dinh theo cau truc da xac nhan: A..O
# ============================================================
def sync_supply_chain_stock(sh):
    ws = sh.worksheet("BC Tồn & ASIN")
    rows = ws.get_all_values()
    data_rows = rows[3:]  # data bat dau tu row 4 (index 3)
    records = []
    for row in data_rows:
        if len(row) < 15:
            continue
        if _row_has_error(row):
            continue
        supplier_part_number = row[5].strip()  # cot F: ASIN-part number (vd WF-TP00010)
        if not supplier_part_number:
            continue
        records.append({
            "supplier_part_number": supplier_part_number,
            "supplier_name": row[2].strip(),      # C: NCC
            "ma_sp": row[3].strip(),               # D: Ma SP
            "product_name": row[4].strip(),        # E: Ten SP
            "asin": supplier_part_number,
            "fnsku_gtin": row[6].strip(),          # G
            "brand": row[7].strip(),               # H
            "market": row[9].strip(),              # J
            "stock_vn": _to_float(row[10]),        # K: Luu kho tai VN
            "in_production_vn": _to_float(row[11]),# L: Dang san xuat tai VN
            "on_the_sea": _to_float(row[12]),      # M: Tong hang tren bien di
            "account_stock": _to_float(row[13]),   # N: Hang ton trong account
            "total_pending": _to_float(row[14]),   # O: Tong hang dang cho ban
        })
    return records


# ============================================================
# inbound <- tab "INBOUND", CHI danh sach chinh (theo Giang chon)
# Header row 1 co mot so cot bi trong ten, nen doc raw values.
# Cot co dinh: A Part Number, B Ma TP, C Ten hang, D Active Date,
#              E Qty, F Stage, G Channel, H Market
# ============================================================
def sync_inbound(sh):
    ws = sh.worksheet("INBOUND")
    rows = ws.get_all_values()
    data_rows = rows[1:]  # data bat dau tu row 2
    records = []
    for row in data_rows:
        if len(row) < 8:
            continue
        if _row_has_error(row):
            continue
        part_number = row[0].strip()
        if not part_number:
            continue
        records.append({
            "supplier_part_number": part_number,
            "ma_tp": row[1].strip(),
            "product_name": row[2].strip(),
            "active_date": row[3].strip(),
            "qty": _to_float(row[4]),
            "stage": row[5].strip(),      # PRODUCTION / SHIPMENT
            "channel": row[6].strip(),
            "market": row[7].strip(),
        })
    return records


# ============================================================
# monthly_sales <- tab "Orders" (pivot theo thang, wide format)
# Row 2 (index 1) chua nhan thang vd '2026-Apr' tai cac cot le
# Row 3 (index 2) chua sub-header 'SUM of Quantity' / 'SUM of Revenue'
# Du lieu bat dau row 4 (index 3)
# ============================================================
def sync_monthly_sales(sh):
    ws = sh.worksheet("Monthly")
    rows = ws.get_all_values()
    period_row = rows[1]
    data_rows = rows[3:]

    periods = []  # list of (year_month:str, qty_col_idx, rev_col_idx)
    col = 4  # cot E (index 4) la vi tri bat dau cac cap thang
    while col < len(period_row):
        label = period_row[col].strip()
        if label:
            match = re.match(r"(\d{4})-([A-Za-z]{3})", label)
            year_month = f"{match.group(1)}-{_MONTH_MAP.get(match.group(2), '00')}" if match else label
            periods.append((year_month, col, col + 1))
        col += 2

    SANITY_MAX_QTY = 500_000       # 1 SKU/thang khong the ban vuot con so nay
    SANITY_MAX_REV = 50_000_000    # tuong tu cho doanh thu (USD)

    records = []
    skipped = []
    for row in data_rows:
        if len(row) < 4:
            continue
        if _row_has_error(row):
            continue
        sku = row[3].strip()  # cot D: Item Number
        product_name = row[2].strip() if len(row) > 2 else ""
        if not sku or "total" in sku.lower() or "total" in product_name.lower():
            continue

        row_records = []
        anomalous = False
        for year_month, qty_idx, rev_idx in periods:
            qty = _to_float(row[qty_idx]) if qty_idx < len(row) else None
            rev = _to_float(row[rev_idx]) if rev_idx < len(row) else None
            if qty is None and rev is None:
                continue
            if (qty is not None and abs(qty) > SANITY_MAX_QTY) or (rev is not None and abs(rev) > SANITY_MAX_REV):
                anomalous = True
                break
            row_records.append({
                "sku": sku,
                "product_name": product_name,
                "year_month": year_month,
                "quantity": int(qty) if qty is not None else None,
                "revenue": rev,
            })

        if anomalous:
            skipped.append(sku)
            continue
        records.extend(row_records)

    if skipped:
        print(f"[monthly_sales] Bo qua {len(skipped)} dong co so lieu bat thuong (nghi la dong Total/rac): {skipped[:10]}")

    return records


# ============================================================
# inbound_summary <- bang "Stage / Units" trong dashboard cua tab INBOUND
# Day la nguon tong hop chuan (Giang xac nhan), khac voi danh sach chinh
# vi danh sach chinh co the chua phan anh het cac lo qua som (New Lot).
# Tim vi tri bang bang cach quet label "Stage" + "Units" thay vi hardcode
# toa do, vi day la vung dashboard de bi dich chuyen khi sua sheet.
# ============================================================
def sync_inbound_summary(sh):
    ws = sh.worksheet("INBOUND")
    rows = ws.get_all_values()

    header_pos = None
    for r_idx, row in enumerate(rows):
        for c_idx, cell in enumerate(row):
            if cell.strip() == "Stage" and c_idx + 1 < len(row) and row[c_idx + 1].strip() == "Units":
                header_pos = (r_idx, c_idx)
                break
        if header_pos:
            break

    if header_pos is None:
        print("[inbound_summary] Khong tim thay bang Stage/Units, bo qua bang nay.")
        return []

    r0, c0 = header_pos
    records = []
    for row in rows[r0 + 1:]:
        if c0 >= len(row) or not row[c0].strip():
            break
        if _row_has_error(row[c0:c0 + 2]):
            continue
        stage = row[c0].strip()
        units = _to_float(row[c0 + 1]) if c0 + 1 < len(row) else None
        records.append({"stage": stage, "units": units})

    return records


# ============================================================
# daily_orders <- tab Order (export hang ngay, Dropship vs CastleGate)
# ============================================================
ORDERS_TAB_NAME = "Orders"

def sync_daily_orders(sh):
    ws = sh.worksheet(ORDERS_TAB_NAME)
    rows = ws.get_all_values()
    data_rows = rows[1:]  # bo qua header row

    records = []
    for row in data_rows:
        if len(row) < 12:
            continue
        if _row_has_error(row):
            continue
        item_number = row[9].strip()  # J: Item Number
        if not item_number:
            continue

        store_name = row[3].strip()  # D: Store Name
        source = "castlegate" if store_name.lower() == "singlechannel" else "dropship"

        records.append({
            "revenue": _to_float(row[0]),            # A
            "warehouse_name": row[1].strip(),          # B
            "store_name": store_name,                  # D
            "source": source,
            "po_number": row[4].strip(),               # E
            "po_date": row[5].strip(),                 # F
            "po_date_iso": _parse_us_date_to_iso(row[5].strip()),
            "order_status": row[8].strip(),             # I
            "item_number": item_number,                 # J
            "item_name": row[10].strip(),               # K
            "quantity": _to_float(row[11]),             # L
        })

    return records


# ============================================================
# forecast <- tab Forecast (rolling forecast, 10 chu ky x 15 ngay)
# Header row co ten chu ky lap lai o cot E-N (forecast demand) va
# O-X (projected stock). Chuyen tu wide sang long format.
# ============================================================
def sync_forecast(sh):
    ws = sh.worksheet("Forecast")
    rows = ws.get_all_values()
    header = rows[0]
    data_rows = rows[1:]

    records = []
    for row in data_rows:
        if len(row) < 24:
            continue
        if _row_has_error(row):
            continue
        part_number = row[0].strip()
        if not part_number:
            continue

        product_name = row[1].strip()
        current_stock = _to_float(row[2])
        sales_raw = row[3].strip() if len(row) > 3 else ""
        sales_rate = None if sales_raw.upper() == "NONE" else _to_float(sales_raw)
        ipi = _to_float(row[25]) if len(row) > 25 else None

        for i in range(10):
            demand_idx = 4 + i    # cot E..N
            stock_idx = 14 + i    # cot O..X
            if demand_idx >= len(row) or stock_idx >= len(row):
                continue
            cycle_label = header[demand_idx].strip() if demand_idx < len(header) else f"Chu ky {i + 1}"
            records.append({
                "part_number": part_number,
                "product_name": product_name,
                "current_stock": current_stock,
                "sales_rate": sales_rate,
                "ipi": ipi,
                "cycle_index": i + 1,
                "cycle_label": cycle_label,
                "forecast_demand": _to_float(row[demand_idx]),
                "projected_stock": _to_float(row[stock_idx]),
            })

    return records


# ============================================================
# ads_monthly <- tab "Ads" (cap nhat theo thang, du lieu la "hien tai")
# Join voi products qua ASIN (khong qua SKU vi "Ma TP" trong tab Ads
# khong co tien to WF- nen khong khop truc tiep duoc)
# ============================================================
# ============================================================
# returns_monthly <- tab "Return" (Incidents/Buyer's Remorse/Replacement)
# So luong return = Incidents Number + Buyer's Remorse Number + Replacement Count
# ============================================================
def sync_returns(sh):
    ws = sh.worksheet("Return")
    rows = ws.get_all_values()
    header = rows[0]

    def col_idx(name):
        try:
            return header.index(name)
        except ValueError:
            return None

    idx = {
        "month": col_idx("Month"),
        "product_name": col_idx("Product Name"),
        "product_id": col_idx("Product ID"),
        "wayfair_sku": col_idx("Wayfair SKU"),
        "incidents_number": col_idx("Incidents Number"),
        "remorse_number": col_idx("Buyers's Remorse Returns Number"),
        "replacement_count": col_idx("Replacement Parts Count"),
        "total_deduction": col_idx("Total Deductions"),
        "class_name": col_idx("Class"),
        "last_delivery_date": col_idx("Last Delivery Date Reported"),
    }

    if idx["product_id"] is None:
        print("[returns_monthly] Khong tim thay cot 'Product ID', kiem tra lai ten cot trong sheet.")
        return []

    def get(row, key):
        i = idx[key]
        return row[i].strip() if i is not None and i < len(row) else ""

    records = []
    for row in rows[1:]:
        if _row_has_error(row):
            continue
        product_id = get(row, "product_id")
        if not product_id:
            continue
        records.append({
            "month": _normalize_month(get(row, "month")),
            "product_name": get(row, "product_name"),
            "product_id": product_id,
            "wayfair_sku": get(row, "wayfair_sku"),
            "incidents_number": _to_float(get(row, "incidents_number")),
            "remorse_number": _to_float(get(row, "remorse_number")),
            "replacement_count": _to_float(get(row, "replacement_count")),
            "total_deduction": _to_float(get(row, "total_deduction")),
            "class_name": get(row, "class_name"),
            "last_delivery_date": get(row, "last_delivery_date"),
        })
    return records


# ============================================================
# freight_monthly <- tab "Freight" (Transportation + Fulfillment CastleGate)
# ============================================================
def sync_freight(sh):
    ws = sh.worksheet("Freight")
    rows = ws.get_all_values()
    records = []
    for row in rows[1:]:
        if _row_has_error(row):
            continue
        if len(row) < 3:
            continue
        month = _normalize_month(row[0].strip())
        charge_type = row[1].strip()
        if not month or not charge_type:
            continue
        records.append({
            "month": month,
            "charge_type": charge_type,
            "charge_amount": _to_float(row[2]),
        })
    return records


def sync_ads_monthly(sh):
    ws = sh.worksheet("Ads")
    rows = ws.get_all_values()
    header = rows[0]

    def col_idx(name):
        try:
            return header.index(name)
        except ValueError:
            return None

    idx = {
        "month": col_idx("Month"),
        "product_name": col_idx("Product Name"),
        "product_group": col_idx("Product Group"),
        "cogs": col_idx("Cogs"),
        "cogs_total": col_idx("COGS"),
        "asin": col_idx("ASIN"),
        "part_number_raw": col_idx("Mã TP"),
        "revenue": col_idx("Revenue"),
        "orders": col_idx("Orders"),
        "impression": col_idx("Impression"),
        "clicks": col_idx("Clicks"),
        "spend": col_idx("Spends"),
        "ads_ws_sale": col_idx("Ads WS Sale"),
    }

    if idx["asin"] is None:
        print("[ads_monthly] Khong tim thay cot 'ASIN' trong header, kiem tra lai ten cot trong sheet.")
        return []

    def get(row, key):
        i = idx[key]
        return row[i].strip() if i is not None and i < len(row) else ""

    records = []
    for row in rows[1:]:
        if _row_has_error(row):
            continue
        asin = get(row, "asin")
        if not asin or asin.upper() == "GRAND TOTAL":
            continue
        records.append({
            "month": _normalize_month(get(row, "month")),
            "asin": asin,
            "product_name": get(row, "product_name"),
            "product_group": get(row, "product_group"),
            "cogs": _to_float(get(row, "cogs")),
            "cogs_total": _to_float(get(row, "cogs_total")),
            "part_number_raw": get(row, "part_number_raw"),
            "revenue": _to_float(get(row, "revenue")),
            "orders": _to_float(get(row, "orders")),
            "impression": _to_float(get(row, "impression")),
            "clicks": _to_float(get(row, "clicks")),
            "spend": _to_float(get(row, "spend")),
            "ads_ws_sale": _to_float(get(row, "ads_ws_sale")),
        })
    return records


def _dedupe(records, on_conflict):
    """Loai bo dong trung key (giu dong cuoi cung = ban moi nhat), vi Postgres
    khong cho upsert 2 dong trung key trong cung 1 lenh."""
    keys = [k.strip() for k in on_conflict.split(",")]
    deduped = {}
    for r in records:
        key = tuple(r.get(k) for k in keys)
        deduped[key] = r
    return list(deduped.values())


def upsert(supabase, table, records, delete_filter_col, dedupe_keys=None):
    if not records:
        print(f"[{table}] khong co dong nao de sync.")
        return
    if dedupe_keys:
        records = _dedupe(records, dedupe_keys)
    # Xoa toan bo du lieu cu roi ghi lai tu dau (full refresh), thay vi
    # upsert cong don - vi bang nay la "guong" cua sheet, khong can giu
    # du lieu cu. Tranh viec rac ton dong neu lan sync truoc bi loi
    # (VD tro nham tab, parse sai cot...).
    supabase.table(table).delete().gte(delete_filter_col, "").execute()
    supabase.table(table).insert(records).execute()
    print(f"[{table}] da sync {len(records)} dong (full refresh).")


def main():
    sh = _sheet()
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    upsert(supabase, "products", sync_products(sh), "sku", dedupe_keys="sku")
    upsert(supabase, "pricing", sync_pricing(sh), "supplier_part_number", dedupe_keys="supplier_part_number")
    upsert(supabase, "supply_chain_stock", sync_supply_chain_stock(sh), "supplier_part_number", dedupe_keys="supplier_part_number")
    upsert(supabase, "inbound", sync_inbound(sh), "supplier_part_number", dedupe_keys=None)
    upsert(supabase, "monthly_sales", sync_monthly_sales(sh), "sku", dedupe_keys="sku,year_month")
    upsert(supabase, "inbound_summary", sync_inbound_summary(sh), "stage", dedupe_keys="stage")
    upsert(supabase, "daily_orders", sync_daily_orders(sh), "item_number", dedupe_keys=None)
    upsert(supabase, "forecast", sync_forecast(sh), "part_number", dedupe_keys=None)
    upsert(supabase, "ads_monthly", sync_ads_monthly(sh), "asin", dedupe_keys=None)
    upsert(supabase, "returns_monthly", sync_returns(sh), "product_id", dedupe_keys=None)
    upsert(supabase, "freight_monthly", sync_freight(sh), "month", dedupe_keys=None)


if __name__ == "__main__":
    main()
