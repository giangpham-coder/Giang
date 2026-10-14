"""
sync_amazon_sheet.py - Sheet "Giang AMZ" -> Supabase (amz_inbound, amz_lots, amz_stock)
Chay: pip install gspread google-auth supabase --break-system-packages && python sync_amazon_sheet.py
Env: GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID_AMZ (doan giua /d/ va /edit trong URL), SUPABASE_URL, SUPABASE_KEY
"""
import os, time
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


if __name__ == "__main__":
    main()
