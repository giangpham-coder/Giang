"""
sync_google_sheet.py
---------------------
Doc nhieu tab tu Google Sheet "Giang Wf" va upsert vao Supabase.
Cac bang duoc dong bo trong ban nay: products, pricing, supply_chain_stock,
inbound, monthly_sales, daily_orders, forecast, ads_monthly, returns_monthly,
freight_monthly, ads_listing, listing_sku_map.

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

    def col_idx_any(*names):
        for name in names:
            i = col_idx(name)
            if i is not None:
                return i
        return None

    idx = {
        "month": col_idx_any("Month", "Mont"),
        "product_name": col_idx_any("Tên sản phẩm", "Product Name"),
        "product_group": col_idx_any("Product Group"),
        "cogs": col_idx_any("Cogs"),
        "cogs_total": col_idx_any("COGS"),
        "sku": col_idx_any("SKU"),
        "asin": col_idx_any("ASIN"),
        "part_number_raw": col_idx_any("TP", "Mã TP"),
        "revenue": col_idx_any("Order Sales", "Revenue"),
        "orders": col_idx_any("Qty", "Orders"),
        "impression": col_idx_any("Impressions", "Impression"),
        "clicks": col_idx_any("Clicks"),
        "spend": col_idx_any("Spend", "Spends"),
        "ads_ws_sale": col_idx_any("Ads WS Sale"),
    }

    if idx["asin"] is None:
        print("[ads_monthly] Khong tim thay cot 'ASIN' trong header, kiem tra lai ten cot trong sheet.")
        return []

    missing = [k for k, v in idx.items() if v is None]
    if missing:
        print(f"[ads_monthly] Canh bao: khong tim thay cot cho {missing} - cac gia tri nay se la 0/rong.")

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


# ============================================================
# ads_listing + listing_sku_map
#   <- tab "Ads_KW"  (Keyword Targeting Report, paste noi tiep moi thang)
#   <- tab "Ads_PT"  (Product Targeting Report, phai co cot
#                     first_10_part_numbers - day la cau noi listing -> TP)
#   <- tab "Orders"  (da co san, de lay order sales that + qty theo listing)
#   <- tab "Price"   (COGS/don vi theo Supplier Part Number, de tinh COGS/listing)
#
# Gom theo (month, listing) - KHONG gom theo campaign_name, vi 1 campaign
# thuong trai tren 2-4 listing khac nhau (VD "Ghe nha tam teak" co 4 listing).
# Ca 2 report deu co san cot `listing` nen spend da duoc Wayfair tach san,
# khong can phan bo uoc luong.
# ============================================================
ADS_KW_TAB = "KW"
ADS_PT_TAB = "PT"

# Listing chi xuat hien trong report Keyword (khong co first_10_part_numbers)
# thi khong tu map duoc TP -> khai bao tay o day.
MANUAL_LISTING_TP = {
    # "TDIT1076": ["TP00XXX"],
    # "TDIT1091": ["TP00XXX"],
}

_TP_RE = re.compile(r"TP\d+")


def _extract_tps(text):
    return _TP_RE.findall(str(text or "").upper())


def _month_from_any_date(value):
    """Chap nhan ca '2026-07-01' (dang export goc) lan '7/1/2026'
    (neu Google Sheets tu format lai khi paste). Tra ve 'YYYY-MM'."""
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) >= 7 and s[4] == "-":
        return s[:7]
    parts = s.split("/")
    if len(parts) == 3:
        try:
            m, _d, y = int(parts[0]), int(parts[1]), int(parts[2])
            if y < 100:
                y += 2000
            return f"{y:04d}-{m:02d}"
        except ValueError:
            return None
    return None


def _read_ads_tab(sh, tab_name):
    """Doc 1 tab report ads, tra ve (header_index_map, data_rows).
    Map cot theo TEN header nen thu tu cot trong sheet doi cung khong sao."""
    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        print(f"[ads_listing] Khong tim thay tab '{tab_name}', bo qua.")
        return {}, []
    rows = ws.get_all_values()
    if not rows:
        return {}, []
    header = [h.strip() for h in rows[0]]
    idx = {name: i for i, name in enumerate(header)}
    return idx, rows[1:]


def _cell(row, idx, name):
    i = idx.get(name)
    return row[i].strip() if i is not None and i < len(row) else ""


_WSC_COL = "attributed_wholesale_cost_window_view_through_USD_Day_14"
_RET_COL = "attributed_retail_sales_window_view_through_USD_Day_14"


def build_ads_listing(sh):
    """Tra ve (ads_listing_records, listing_sku_map_records, ads_sku_report_records)."""
    kw_idx, kw_rows = _read_ads_tab(sh, ADS_KW_TAB)
    pt_idx, pt_rows = _read_ads_tab(sh, ADS_PT_TAB)

    if not kw_rows and not pt_rows:
        print("[ads_listing] Ca 2 tab ads deu rong, bo qua.")
        return [], []

    # ---------- 1. listing -> {TP codes}  (tu report Product Targeting) ----
    listing_tps = {}
    listing_name = {}
    for row in pt_rows:
        if _row_has_error(row):
            continue
        listing = _cell(row, pt_idx, "listing")
        if not listing:
            continue
        listing_tps.setdefault(listing, set()).update(
            _extract_tps(_cell(row, pt_idx, "first_10_part_numbers"))
        )
        listing_name.setdefault(listing, _cell(row, pt_idx, "product_name"))
    for row in kw_rows:
        if _row_has_error(row):
            continue
        listing = _cell(row, kw_idx, "listing")
        if listing:
            listing_name.setdefault(listing, _cell(row, kw_idx, "product_name"))
    for listing, tps in MANUAL_LISTING_TP.items():
        listing_tps.setdefault(listing, set()).update(tps)

    missing = sorted(l for l in listing_name if not listing_tps.get(l))
    if missing:
        print(f"[ads_listing] {len(missing)} listing chua co part number "
              f"(order se khong map duoc): {', '.join(missing)} "
              f"-> them vao MANUAL_LISTING_TP")

    # ---------- 2. Gom ads theo (month, listing) --------------------------
    agg = {}

    def _blank():
        return {"impressions": 0.0, "clicks": 0.0, "kw_spend": 0.0, "pt_spend": 0.0,
                "ads_ws_sales": 0.0, "ads_retail_sales": 0.0,
                "order_sales": 0.0, "qty_sold": 0.0, "cogs": 0.0,
                "has_kw": False, "has_pt": False}

    def _absorb(rows, idx, spend_key, flag):
        skipped = 0
        for row in rows:
            if _row_has_error(row):
                continue
            listing = _cell(row, idx, "listing")
            month = _month_from_any_date(_cell(row, idx, "Date"))
            if not listing or not month:
                skipped += 1
                continue
            a = agg.setdefault((month, listing), _blank())
            a["impressions"] += _to_float(_cell(row, idx, "impressions")) or 0
            a["clicks"] += _to_float(_cell(row, idx, "clicks")) or 0
            a[spend_key] += _to_float(_cell(row, idx, "spend_USD")) or 0
            a["ads_ws_sales"] += _to_float(_cell(row, idx, _WSC_COL)) or 0
            a["ads_retail_sales"] += _to_float(_cell(row, idx, _RET_COL)) or 0
            a[flag] = True
        if skipped:
            print(f"[ads_listing] bo qua {skipped} dong thieu cot listing/Date.")

    _absorb(kw_rows, kw_idx, "kw_spend", "has_kw")
    _absorb(pt_rows, pt_idx, "pt_spend", "has_pt")

    # Chi giu lai nhung THANG thuc su co data ads (KW hoac PT) - vi Orders
    # tab von co lich su nhieu thang tu truoc, khong muon "an ke" thang chua
    # paste ads vao lam loang so lieu.
    ads_months = {m for (m, _l), a in agg.items() if a["has_kw"] or a["has_pt"]}
    if not ads_months:
        print("[ads_listing] Khong co thang nao co data ads that su, dung lai.")
        return [], []

    # ---------- 3. TP -> listing (conflict thi uu tien listing spend cao) --
    def _spend_of(listing):
        return sum(v["kw_spend"] + v["pt_spend"]
                   for (_m, l), v in agg.items() if l == listing)

    tp_to_listing = {}
    for listing, tps in listing_tps.items():
        for tp in tps:
            prev = tp_to_listing.get(tp)
            if prev is None or prev == listing:
                tp_to_listing[tp] = listing
                continue
            winner = listing if _spend_of(listing) > _spend_of(prev) else prev
            print(f"[ads_listing] {tp} thuoc ca {prev} va {listing} "
                  f"-> gan cho {winner} (ad spend cao hon)")
            tp_to_listing[tp] = winner

    # ---------- 4. COGS/don vi theo TP (tu tab Price) ----------------------
    cogs_per_tp = {}
    try:
        price_rows = sh.worksheet("Price").get_all_records(expected_headers=[
            "Supplier Part Number", "Product Name", "Status", "SKU",
            "COGS", "Current Base Cost", "Retail Price", "Margin",
        ])
        for row in price_rows:
            tps = _extract_tps(row.get("Supplier Part Number", ""))
            unit_cogs = _to_float(row.get("COGS"))
            if tps and unit_cogs is not None:
                cogs_per_tp[tps[0]] = unit_cogs
    except Exception as exc:
        print(f"[ads_listing] Khong doc duoc tab Price ({exc}), COGS = 0.")

    # ---------- 5. Orders that -> (month, listing) -------------------------
    unmapped = {}
    tp_month_sales = {}  # (month, tp) -> {rev, qty} - track rieng TP, khong phu thuoc co map duoc listing hay khong
    try:
        ws = sh.worksheet(ORDERS_TAB_NAME)
        for row in ws.get_all_values()[1:]:
            if len(row) < 12 or _row_has_error(row):
                continue
            status = row[8].strip().lower()              # I: Order Status
            if "cancel" in status or "reject" in status:
                continue
            tps = _extract_tps(row[9])                   # J: Item Number
            if not tps:
                continue
            month = _month_from_any_date(_parse_us_date_to_iso(row[5].strip()))  # F: PO Date
            if not month or month not in ads_months:
                continue
            tp = tps[0]
            qty = _to_float(row[11]) or 0                # L: Quantity
            rev = _to_float(row[0]) or 0                 # A: Revenue (= qty x wholesale)

            s = tp_month_sales.setdefault((month, tp), {"rev": 0.0, "qty": 0.0})
            s["rev"] += rev
            s["qty"] += qty

            listing = tp_to_listing.get(tp)
            if not listing:
                u = unmapped.setdefault((month, tp), {"rev": 0.0, "qty": 0.0})
                u["rev"] += rev
                u["qty"] += qty
                continue
            a = agg.setdefault((month, listing), _blank())
            a["order_sales"] += rev
            a["qty_sold"] += qty
            a["cogs"] += qty * cogs_per_tp.get(tp, 0.0)
    except Exception as exc:
        print(f"[ads_listing] Khong doc duoc tab {ORDERS_TAB_NAME} ({exc}).")

    # Order khong map duoc listing nao -> gom vao 1 dong canh bao, de khong
    # that thoat revenue khi doi chieu tong.
    for (month, tp), u in unmapped.items():
        a = agg.setdefault((month, "UNMAPPED"), _blank())
        a["order_sales"] += u["rev"]
        a["qty_sold"] += u["qty"]
        a["cogs"] += u["qty"] * cogs_per_tp.get(tp, 0.0)
    if unmapped:
        total_unmapped = sum(u["rev"] for u in unmapped.values())
        top = sorted(unmapped.items(), key=lambda x: -x[1]["rev"])[:10]
        print(f"[ads_listing] UNMAPPED ${total_unmapped:,.2f}: "
              + " ".join(f"{tp}@{m}(${u['rev']:.0f})" for (m, tp), u in top))

    # ---------- 6b. TP -> ASIN (tu tab Product, cot A=SKU, cot D=ASIN) -----
    tp_asin = {}
    try:
        prod_rows = sh.worksheet("Product").get_all_values()
        for row in prod_rows[1:]:
            if not row or _row_has_error(row):
                continue
            sku = row[0].strip() if len(row) > 0 else ""
            asin = row[3].strip() if len(row) > 3 else ""
            tps = _extract_tps(sku)
            if tps and asin:
                tp_asin[tps[0]] = asin
    except Exception as exc:
        print(f"[ads_sku_report] Khong doc duoc tab Product ({exc}), ASIN se de trong.")

    # ---------- 6c. Bao cao phang theo TP/ASIN - UNION day du, khong co
    # bucket "UNMAPPED" nao ca. Con nao co ban VA/HOAC co ads deu len dong
    # rieng cua no. Spend hien theo listing (vi Wayfair khong tach duoc
    # spend theo tung TP rieng le trong 1 listing).
    all_tps_by_month = {}
    for month in ads_months:
        all_tps_by_month[month] = set()
    for tp in tp_to_listing:
        for month in ads_months:
            all_tps_by_month[month].add(tp)
    for (month, tp) in tp_month_sales:
        if month in ads_months:
            all_tps_by_month[month].add(tp)

    sku_report_records = []
    for month, tps_this_month in all_tps_by_month.items():
        for tp in sorted(tps_this_month):
            listing = tp_to_listing.get(tp, "")
            listing_agg = agg.get((month, listing)) if listing else None
            ads_spend = round((listing_agg["kw_spend"] + listing_agg["pt_spend"]), 2) if listing_agg else 0.0
            sales = tp_month_sales.get((month, tp), {"rev": 0.0, "qty": 0.0})
            in_ads = bool(listing)
            in_orders = sales["qty"] > 0
            if not in_ads and not in_orders:
                continue
            sku_report_records.append({
                "month": month,
                "tp_code": tp,
                "asin": tp_asin.get(tp, ""),
                "listing": listing,
                "product_name": listing_name.get(listing, "") if listing else "",
                "qty_sold": int(sales["qty"]),
                "order_sales": round(sales["rev"], 2),
                "ads_spend": ads_spend,
                "in_ads": in_ads,
                "in_orders": in_orders,
            })

    print(f"[ads_sku_report] {len(sku_report_records)} dong "
          f"({sum(1 for r in sku_report_records if r['in_ads'] and not r['in_orders'])} chi co ads khong ban, "
          f"{sum(1 for r in sku_report_records if r['in_orders'] and not r['in_ads'])} chi co ban khong ads).")

    # ---------- 6d. Xuat records (ads_listing, gom theo listing) -----------
    records = []
    for (month, listing), a in agg.items():
        spend = a["kw_spend"] + a["pt_spend"]
        tps = sorted(listing_tps.get(listing, []))
        if a["has_kw"] and a["has_pt"]:
            ads_type = "KW+PT"
        elif a["has_kw"]:
            ads_type = "KW"
        elif a["has_pt"]:
            ads_type = "PT"
        else:
            ads_type = "Organic"
        records.append({
            "month": month,
            "listing": listing,
            "product_name": listing_name.get(listing, ""),
            "tp_codes": ", ".join(tps),
            "sku_count": len(tps),
            "impressions": int(a["impressions"]),
            "clicks": int(a["clicks"]),
            "kw_spend": round(a["kw_spend"], 2),
            "pt_spend": round(a["pt_spend"], 2),
            "spend": round(spend, 2),
            "ads_ws_sales": round(a["ads_ws_sales"], 2),
            "ads_retail_sales": round(a["ads_retail_sales"], 2),
            "order_sales": round(a["order_sales"], 2),
            "qty_sold": int(a["qty_sold"]),
            "cogs": round(a["cogs"], 2),
            "roas": round(a["ads_ws_sales"] / spend, 3) if spend > 0 else 0,
            "tacos": round(spend / a["order_sales"] * 100, 3) if a["order_sales"] > 0 else 0,
            "cpc": round(spend / a["clicks"], 4) if a["clicks"] > 0 else 0,
            "ads_type": ads_type,
        })

    map_records = [
        {"listing": listing, "tp_code": tp, "product_name": listing_name.get(listing, "")}
        for listing, tps in listing_tps.items() for tp in sorted(tps)
    ]

    months = sorted({r["month"] for r in records})
    print(f"[ads_listing] {len(records)} dong / {len(months)} thang ({', '.join(months)}), "
          f"tong spend ${sum(r['spend'] for r in records):,.2f}")
    return records, map_records, sku_report_records


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

    # Tab "KW"/"PT" da bi xoa (Giang tu lam consolidator rieng) - bao ve
    # bang try/except de neu 2 tab nay khong ton tai, sync khong bi crash
    # giua chung, cac bang khac phia truoc van chay binh thuong.
    try:
        ads_listing_rows, sku_map_rows, sku_report_rows = build_ads_listing(sh)
        upsert(supabase, "listing_sku_map", sku_map_rows, "listing", dedupe_keys="listing,tp_code")
        upsert(supabase, "ads_listing", ads_listing_rows, "month", dedupe_keys="month,listing")
        upsert(supabase, "ads_sku_report", sku_report_rows, "month", dedupe_keys="month,tp_code")
    except Exception as exc:
        print(f"[ads_listing] Bo qua (tab KW/PT khong ton tai hoac loi khac): {exc}")


if __name__ == "__main__":
    main()
