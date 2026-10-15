"""
sync_amazon_sheet.py - Sheet "Giang AMZ" -> Supabase (amz_inbound, amz_lots, amz_stock)
Chay: pip install gspread google-auth supabase --break-system-packages && python sync_amazon_sheet.py
Env: GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID_AMZ (doan giua /d/ va /edit trong URL), SUPABASE_URL, SUPABASE_KEY
"""
import os, re, time
import gspread
from google.oauth2.service_account import Credentials
from supabase import create_client

SHEET_ID = os.environ["SHEET_ID_AMZ"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
ERR = ("#REF!", "#ERROR!", "#N/A", "#VALUE!", "#NAME?", "#NULL!", "#DIV/0!", "Loading...")


def num(v):
    try:
        s = str(v).replace("$", "").replace(",", "").replace("%", "").strip()
        return float(s) if s and s != "-" else None
    except ValueError:
        return None


def iso(d):  # 'M/D/YYYY' -> 'YYYY-MM-DD'
    try:
        m, dd, y = [int(x) for x in str(d).strip().split("/")]
        return f"{y:04d}-{m:02d}-{dd:02d}"
    except ValueError:
        return None


def bad(row):
    return any(str(c).strip() in ERR for c in row)


def rows_of(sh, title, retries=3):
    for i in range(retries):
        try:
            return sh.worksheet(title).get_all_values()
        except Exception as e:
            print(f"[retry] {title} ({i+1}/{retries}): {e}")
            time.sleep(5)
    raise RuntimeError(f"Khong doc duoc tab {title}")


def c(row, i):
    return row[i].strip() if i < len(row) else ""


# INBOUND, bang chinh: A ASIN | B SKU | C Product Name | D Active Date | E Quantity | F Status | G Lot
def sync_inbound(sh):
    out = []
    for r in rows_of(sh, "INBOUND")[1:]:
        if bad(r) or not c(r, 0):
            continue
        out.append({"asin": c(r, 0), "sku": c(r, 1), "product_name": c(r, 2),
                    "active_date": c(r, 3), "active_date_iso": iso(c(r, 3)),
                    "qty": num(c(r, 4)), "status": c(r, 5).upper(), "lot": c(r, 6)})
    return out


# INBOUND, bang lo: quet header "Lot" + "Qty" (khong hardcode toa do vi vung dashboard hay bi dich)
def sync_lots(sh):
    rows = rows_of(sh, "INBOUND")
    pos = None
    for ri, r in enumerate(rows):
        for ci in range(len(r) - 1):
            if r[ci].strip() == "Lot" and r[ci + 1].strip() == "Qty":
                pos = (ri, ci)
                break
        if pos:
            break
    if not pos:
        print("[amz_lots] Khong tim thay bang Lot/Qty, bo qua.")
        return []
    r0, c0 = pos
    hdr = [h.strip().lower() for h in rows[r0]]

    def find(prefix):
        for i in range(c0, len(hdr)):
            if hdr[i].startswith(prefix):
                return i
        return None
    ix = {k: find(k) for k in ("planned", "estimated", "status", "days late", "days to")}
    get = lambda r, k: c(r, ix[k]) if ix[k] is not None else ""
    out = []
    for r in rows[r0 + 1:]:
        if not c(r, c0):
            break
        if bad(r[c0:]):
            continue
        out.append({"lot": c(r, c0), "qty": num(c(r, c0 + 1)),
                    "planned_export": get(r, "planned"), "estimated_active": get(r, "estimated"),
                    "status": get(r, "status").upper(),
                    "days_late": num(get(r, "days late")), "days_to_active": num(get(r, "days to"))})
    return out


# Stock: header row 1, row 2 la dong dieu khien (bo qua), data tu row 3
# B NCC | C Ma SP (TP) | D Ten SP | E ASIN | F FNSKU | G Brand | H Acc | I Thi truong
# J Luu kho VN | K Dang SX | L Tren bien | M Ton account | N Tong cho ban
def sync_stock(sh):
    out = []
    for r in rows_of(sh, "Stock")[2:]:
        if len(r) < 14 or bad(r) or not c(r, 4):
            continue
        out.append({"supplier_name": c(r, 1), "tp_code": c(r, 2), "product_name": c(r, 3),
                    "asin": c(r, 4), "fnsku": c(r, 5), "brand": c(r, 6), "account": c(r, 7),
                    "market": c(r, 8), "stock_vn": num(r[9]), "in_production_vn": num(r[10]),
                    "on_the_sea": num(r[11]), "account_stock": num(r[12]), "total_pending": num(r[13])})
    return out


ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


# Monthly (pivot): moi thang 2 cot (Units, Revenue), nhan thang MM/YY nam o cot dau (o merge).
# B Product Name | C ASIN | D.. = [units, revenue] cho tung thang. Van doc duoc ban cu chi co units.
def sync_monthly(sh):
    rows = rows_of(sh, "Monthly")
    month_re = re.compile(r"^(\d{1,2})/(\d{2,4})$")
    hi = next((i for i, r in enumerate(rows[:6]) if any(month_re.match(x.strip()) for x in r)), None)
    if hi is None:
        return []
    hdr = rows[hi]
    sub = rows[hi + 1] if hi + 1 < len(rows) else []
    cols, cur, k = [], None, 0
    for i in range(3, len(hdr)):
        m = month_re.match(hdr[i].strip())
        if m:
            y = int(m.group(2))
            y += 2000 if y < 100 else 0
            cur, k = f"{y:04d}-{int(m.group(1)):02d}", 0
        if cur is None:
            continue
        lab = c(sub, i).lower()
        if "unit" in lab or lab.startswith("sum of u"):
            kind = "units"
        elif lab.startswith("sum of to") or "sales" in lab or "revenue" in lab:
            kind = "revenue"
        else:
            kind = "units" if k == 0 else "revenue"
        cols.append((i, cur, kind))
        k += 1
    recs = {}
    for r in rows[hi + 1:]:
        asin = c(r, 2).upper()
        if bad(r) or not ASIN_RE.match(asin):
            continue
        for i, ym, kind in cols:
            v = num(c(r, i))
            if v is None:
                continue
            d = recs.setdefault((asin, ym), {"asin": asin, "product_name": c(r, 1), "year_month": ym, "units": None, "revenue": None})
            d[kind] = v
    return list(recs.values())


# SalesW (pivot theo tuan): row 2 = header, data tu row 3. B ASIN | C Product Name | D.. = ngay M/D/YYYY
def sync_weekly(sh):
    rows = rows_of(sh, "SalesW")
    if len(rows) < 3:
        return []
    periods = [(i, iso(h)) for i, h in enumerate(rows[1]) if i >= 3 and iso(h)]
    out = []
    for r in rows[2:]:
        asin = c(r, 1).upper()
        if bad(r) or not ASIN_RE.match(asin):
            continue
        for i, d in periods:
            u = num(c(r, i))
            if u is not None:
                out.append({"asin": asin, "product_name": c(r, 2), "week_start": d, "units": u})
    return out


def refresh(sb, table, records):
    if not records:
        print(f"[{table}] rong, giu nguyen du lieu cu.")
        return
    sb.table(table).delete().gt("id", 0).execute()
    sb.table(table).insert(records).execute()
    print(f"[{table}] {len(records)} dong.")


def main():
    creds = Credentials.from_service_account_file(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SCOPES)
    sh = gspread.authorize(creds).open_by_key(SHEET_ID)
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    refresh(sb, "amz_inbound", sync_inbound(sh))
    refresh(sb, "amz_lots", sync_lots(sh))
    refresh(sb, "amz_stock", sync_stock(sh))
    refresh(sb, "amz_monthly_sales", sync_monthly(sh))
    refresh(sb, "amz_weekly_sales", sync_weekly(sh))


if __name__ == "__main__":
    main()
