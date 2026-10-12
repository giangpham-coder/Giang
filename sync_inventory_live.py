#!/usr/bin/env python3
"""
Wayfair CastleGate On-hand Inventory -> upsert vao bang `live_inventory`
tren Supabase. Chay 1 LAN/NGAY, cung luc voi sync_google_sheet.py (khong
can polling 15 phut - da confirm voi Giang la Wayfair cung chi cap nhat
noi bo theo nhip feed TRUE_UP/DIFFERENTIAL, poll nhanh hon khong lam data
moi hon).

Phan AUTH + QUERY trong file nay copy nguyen tu script
`wayfair_inventory_realtime.py` (ban da test thanh cong tren sandbox,
ra dung data that) - KHONG doan mo, giu y het de chac an dung.

CAN DIEN TRUOC KHI CHAY:
- WAYFAIR_CLIENT_ID, WAYFAIR_CLIENT_SECRET: da co san, dang dung cho
  script realtime kia.
- WAYFAIR_SUPPLIER_ID: supplier ID parent (Int).
- SUPABASE_URL, SUPABASE_KEY: giong sync_google_sheet.py dang dung.
- Danh sach supplier part number: lay HET tu bang `products` da sync san
  tren Supabase. Da TEST THAT tren production (TP00429): Wayfair dung dang
  "<ma>-WF" (vd TP00429-WF); "WF-TP00429" va "TP00429" tra ve rong, khong
  bao loi. Cot products.sku lai lan lon 2 kieu (WF-TP... va TP...-WF) nen
  script chuan hoa ve ma goc roi gui <ma>-WF (+ vai dang du phong).

CHAY:
    python sync_inventory_live.py                # production
    python sync_inventory_live.py --sandbox       # test truoc, khong ghi de data that

Chay hang ngay: them 1 step trong .github/workflows/sync.yml, ngay sau
buoc chay sync_google_sheet.py:

    - name: Sync live inventory (CastleGate)
      run: python sync_inventory_live.py
      env:
        WAYFAIR_CLIENT_ID: ${{ secrets.WAYFAIR_CLIENT_ID }}
        WAYFAIR_CLIENT_SECRET: ${{ secrets.WAYFAIR_CLIENT_SECRET }}
        WAYFAIR_SUPPLIER_ID: ${{ secrets.WAYFAIR_SUPPLIER_ID }}
        SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
        SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

# ---- Auth + endpoint: copy nguyen tu ban da test thanh cong -----------
TOKEN_URL_PROD = "https://sso.auth.wayfair.com/oauth/token"
API_URL_PROD = "https://api.wayfair.io/v1/supplier-order-api/graphql"
API_URL_SANDBOX = "https://api.wayfair.io/sandbox/v1/supplier-order-api/graphql"

TOKEN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wayfair_token_cache.json")

PART_CHUNK_SIZE = 100  # gioi han filter.supplierPartNumbers (1-100)
PAGE_SIZE = 100        # gioi han page.first (1-100)

INVENTORY_QUERY = """
query inventorySummaryList($supplierId: Int!, $filter: InventoryFilterInput, $page: InventoryPageInput) {
  inventorySummaryList(supplierId: $supplierId, filter: $filter, page: $page) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      cursor
      node {
        supplierPartNumber
        manufacturerPartId
        sku
        productName
        options
        inventoryPosition {
          castleGate {
            onHandQty
            onHand {
              inStockQty
              inStock {
                fulfillableQty
              }
            }
            warehouses {
              warehouseId
              onHandQty
            }
          }
          physicalRetail {
            onHandQty
            onHand {
              inStockQty
              inStock {
                fulfillableQty
              }
            }
            warehouses {
              warehouseId
              onHandQty
            }
          }
        }
      }
    }
  }
}
"""


class WayfairAuthError(Exception):
    pass


class WayfairAPIError(Exception):
    pass


def get_access_token(client_id: str, client_secret: str) -> str:
    """Fetch (or reuse cached) OAuth access token via client_credentials grant.
    Copy nguyen tu ban da test thanh cong - khong doi logic."""
    now = datetime.now(timezone.utc)

    if os.path.exists(TOKEN_CACHE_FILE):
        try:
            with open(TOKEN_CACHE_FILE, "r") as f:
                cached = json.load(f)
            expires_at = datetime.fromisoformat(cached["expires_at"])
            if cached.get("client_id") == client_id and now < expires_at - timedelta(minutes=30):
                return cached["access_token"]
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    resp = requests.post(
        TOKEN_URL_PROD,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise WayfairAuthError(f"Token request failed [{resp.status_code}]: {resp.text}")

    data = resp.json()
    access_token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    expires_at = now + timedelta(seconds=expires_in)

    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(
            {"client_id": client_id, "access_token": access_token, "expires_at": expires_at.isoformat()},
            f,
        )

    return access_token


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_inventory_for_parts(api_url, token, supplier_id, part_numbers):
    """Fetch inventorySummaryList cho 1 batch (<=100) part numbers, tu
    dong lap qua het cac trang cua batch do."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    filter_obj = {"supplierPartNumbers": part_numbers}

    all_nodes = []
    after_cursor = None
    while True:
        page_obj = {"first": PAGE_SIZE}
        if after_cursor:
            page_obj["after"] = after_cursor

        variables = {"supplierId": supplier_id, "filter": filter_obj, "page": page_obj}
        resp = requests.post(
            api_url,
            headers=headers,
            json={"query": INVENTORY_QUERY, "variables": variables},
            timeout=30,
        )

        if resp.status_code == 401:
            raise WayfairAuthError("Unauthorized - token invalid/expired.")
        if resp.status_code != 200:
            raise WayfairAPIError(f"HTTP {resp.status_code}: {resp.text}")

        payload = resp.json()
        if payload.get("errors"):
            msgs = "; ".join(e.get("message", str(e)) for e in payload["errors"])
            raise WayfairAPIError(f"GraphQL error(s): {msgs}")

        data = payload.get("data") or {}
        summary = data.get("inventorySummaryList")
        if summary is None:
            raise WayfairAPIError(f"Unexpected response shape: {json.dumps(payload)[:500]}")

        edges = summary.get("edges", [])
        all_nodes.extend(e["node"] for e in edges)

        page_info = summary.get("pageInfo", {})
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            after_cursor = page_info["endCursor"]
        else:
            break

    return all_nodes


def fetch_all_inventory(api_url, token, supplier_id, part_numbers):
    results = []
    for chunk in _chunk(part_numbers, PART_CHUNK_SIZE):
        results.extend(fetch_inventory_for_parts(api_url, token, supplier_id, chunk))
    return results


def flatten_node(node):
    """Flatten 1 node thanh dict cho upsert Supabase (gom ca CastleGate +
    Physical Retail + breakdown theo warehouse)."""
    inv = node.get("inventoryPosition") or {}
    cg = inv.get("castleGate") or {}
    cg_onhand = cg.get("onHand") or {}
    cg_instock = cg_onhand.get("inStock") or {}
    pr = inv.get("physicalRetail") or {}
    pr_onhand = pr.get("onHand") or {}
    pr_instock = pr_onhand.get("inStock") or {}

    cg_warehouses = ";".join(
        f"{w['warehouseId']}:{w['onHandQty']}" for w in (cg.get("warehouses") or [])
    )

    return {
        "supplier_part_number": node.get("supplierPartNumber"),
        "sku": node.get("sku"),
        "product_name": node.get("productName"),
        "cg_on_hand_qty": cg.get("onHandQty") or 0,
        "cg_in_stock_qty": cg_onhand.get("inStockQty") or 0,
        "cg_fulfillable_qty": cg_instock.get("fulfillableQty") or 0,
        "cg_warehouses": cg_warehouses,
        "pr_on_hand_qty": pr.get("onHandQty") or 0,
        "pr_in_stock_qty": pr_onhand.get("inStockQty") or 0,
        "pr_fulfillable_qty": pr_instock.get("fulfillableQty") or 0,
    }


def core_code(sku):
    """Bo tien to WF- va hau to -WF, con lai ma goc (vd TP00429, 004939)."""
    s = (sku or "").strip().upper()
    if s.startswith("WF-"):
        s = s[3:]
    if s.endswith("-WF"):
        s = s[:-3]
    return s


def get_part_candidates_from_supabase(supabase):
    """Tra ve (candidates, cores): danh sach supplierPartNumber de gui len
    Wayfair, va tap ma goc cua tat ca san pham trong bang products.

    Voi moi san pham gui <ma>-WF (dang da xac nhan), kem WF-<ma> va chinh
    products.sku lam du phong. Ma nao Wayfair khong biet thi chi bi bo qua
    (khong bao loi - da thay khi test), nen gui du cac dang khong gay hai."""
    resp = supabase.table("products").select("sku").execute()
    candidates, seen, cores = [], set(), set()
    for r in resp.data:
        sku = (r.get("sku") or "").strip()
        core = core_code(sku)
        if not core:
            continue
        cores.add(core)
        for c in (f"{core}-WF", f"WF-{core}", sku):
            if c and c not in seen:
                seen.add(c)
                candidates.append(c)
    return candidates, cores


def run(api_url, supabase, client_id, client_secret, supplier_id, part_numbers):
    token = get_access_token(client_id, client_secret)
    try:
        nodes = fetch_all_inventory(api_url, token, supplier_id, part_numbers)
    except WayfairAuthError:
        if os.path.exists(TOKEN_CACHE_FILE):
            os.remove(TOKEN_CACHE_FILE)
        token = get_access_token(client_id, client_secret)
        nodes = fetch_all_inventory(api_url, token, supplier_id, part_numbers)

    rows = [flatten_node(n) for n in nodes]
    for r in rows:
        r["updated_at"] = "now()"

    if rows:
        supabase.table("live_inventory").upsert(rows, on_conflict="supplier_part_number").execute()
        print(f"[wayfair] Upsert {len(rows)} dong vao live_inventory.")
    else:
        print("[wayfair] Khong co du lieu tra ve.")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Sync Wayfair CastleGate inventory vao Supabase")
    parser.add_argument("--sandbox", action="store_true", help="Dung sandbox endpoint (test, khong ghi de data that)")
    args = parser.parse_args()

    client_id = os.environ.get("WAYFAIR_CLIENT_ID")
    client_secret = os.environ.get("WAYFAIR_CLIENT_SECRET")
    supplier_id = os.environ.get("WAYFAIR_SUPPLIER_ID")
    if not client_id or not client_secret or not supplier_id:
        print("Thieu WAYFAIR_CLIENT_ID / WAYFAIR_CLIENT_SECRET / WAYFAIR_SUPPLIER_ID trong environment.", file=sys.stderr)
        sys.exit(1)

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    api_url = API_URL_SANDBOX if args.sandbox else API_URL_PROD
    part_numbers, cores = get_part_candidates_from_supabase(supabase)

    if not part_numbers:
        print("Khong tim thay SKU nao trong bang products.", file=sys.stderr)
        sys.exit(1)

    rows = run(api_url, supabase, client_id, client_secret, int(supplier_id), part_numbers)

    # Doi chieu: san pham nao trong products ma Wayfair khong tra ve dong nao
    # (o bat ky dang ma nao) - de biet co sot khong.
    returned = {core_code(r["supplier_part_number"]) for r in rows}
    missing = sorted(cores - returned)
    print(f"[wayfair] {len(cores) - len(missing)}/{len(cores)} san pham co du lieu tu Wayfair.")
    if missing:
        print(f"[wayfair] Khong thay tren Wayfair ({len(missing)}): {', '.join(missing[:20])}{' ...' if len(missing) > 20 else ''}")


if __name__ == "__main__":
    main()
