// Vercel serverless function: doi bid / bat-tat listing tren Wayfair Advertising API.
// Dashboard goi POST /api/ads-bid. Secret Wayfair CHI nam o day (bien moi truong Vercel), khong bao gio o trinh duyet.
//
// Bien moi truong can dat trong Vercel (Project -> Settings -> Environment Variables):
//   ADS_EDIT_PIN            ma PIN ban tu dat, dashboard phai gui dung moi duoc doi bid
//   WAYFAIR_CLIENT_ID       key production co quyen modify-bids
//   WAYFAIR_CLIENT_SECRET
//   SUPABASE_URL
//   SUPABASE_SERVICE_KEY    service_role key (ghi lich su + doc snapshot)
// Tuy chon:
//   ADS_MAX_CHANGE_PCT      muc doi bid toi da moi lan, mac dinh 50 (%)
//   ADS_MAX_BID             bid toi da cho phep, mac dinh 20 ($)
const crypto = require('crypto');

const TOKEN_URL = 'https://sso.auth.wayfair.com/oauth/token';
const ADS_BASE = 'https://api.wayfair.io/advertising/v1';
const MAX_PER_CALL = 50;   // gioi han doc: 50 listing / request, 1 campaign / request
const MIN_BID = 0.05;
let tokenCache = { token: null, exp: 0 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function safeEqual(a, b) {
  const A = Buffer.from(String(a));
  const B = Buffer.from(String(b));
  return A.length === B.length && crypto.timingSafeEqual(A, B);
}

async function getToken(force) {
  const now = Date.now();
  if (!force && tokenCache.token && now < tokenCache.exp - 60000) return tokenCache.token;
  const r = await fetch(TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'client_credentials',
      client_id: process.env.WAYFAIR_CLIENT_ID,
      client_secret: process.env.WAYFAIR_CLIENT_SECRET,
    }).toString(),
  });
  if (!r.ok) throw new Error(`Token Wayfair loi ${r.status}`);
  const j = await r.json();
  tokenCache = { token: j.access_token, exp: now + (j.expires_in || 3600) * 1000 };
  return tokenCache.token;
}

// Goi 1 campaign. Retry khi 429 / 5xx (toi da 3 lan), lam moi token 1 lan khi 401.
async function callCampaign(campaignId, listings) {
  let refreshed = false;
  for (let attempt = 1; attempt <= 3; attempt++) {
    const token = await getToken();
    const r = await fetch(`${ADS_BASE}/campaign/${campaignId}`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ campaignProductData: { listings } }),
    });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) { /* khong phai JSON */ }
    if (r.ok) return { ok: true, status: r.status, json };
    if (r.status === 401 && !refreshed) { refreshed = true; await getToken(true); attempt--; continue; }
    if ((r.status === 429 || r.status >= 500) && attempt < 3) {
      const wait = Number(r.headers.get('retry-after')) || attempt * 2;
      await sleep(Math.min(wait, 10) * 1000);
      continue;
    }
    return { ok: false, status: r.status, json, text: text.slice(0, 500) };
  }
  return { ok: false, status: 0, text: 'Het so lan thu lai' };
}

function sbHeaders(extra) {
  return Object.assign({
    apikey: process.env.SUPABASE_SERVICE_KEY,
    Authorization: `Bearer ${process.env.SUPABASE_SERVICE_KEY}`,
    'Content-Type': 'application/json',
  }, extra || {});
}

// Snapshot 7 ngay truoc khi doi (tinh tu data ads_listing_daily), gom theo (campaign, listing)
async function buildSnapshots(valid) {
  const out = new Map();
  try {
    const url = process.env.SUPABASE_URL;
    const lr = await fetch(`${url}/rest/v1/ads_listing_daily?select=report_date&order=report_date.desc&limit=1`, { headers: sbHeaders() });
    const lj = await lr.json();
    const latest = lj[0] && lj[0].report_date;
    if (!latest) return out;
    const from = new Date(latest + 'T00:00:00Z');
    from.setUTCDate(from.getUTCDate() - 6);
    const fromStr = from.toISOString().slice(0, 10);
    const byCampaign = new Map();
    valid.forEach((c) => {
      if (!byCampaign.has(c.campaign_id)) byCampaign.set(c.campaign_id, new Set());
      byCampaign.get(c.campaign_id).add(c.listing);
    });
    for (const [cid, set] of byCampaign) {
      const list = [...set].map((x) => `"${x}"`).join(',');
      const r = await fetch(
        `${url}/rest/v1/ads_listing_daily?select=report_date,listing,spend,clicks,impressions,orders_14d,sales_14d,default_bid` +
        `&campaign_id=eq.${encodeURIComponent(cid)}&listing=in.(${encodeURIComponent(list)})&report_date=gte.${fromStr}`,
        { headers: sbHeaders() });
      if (!r.ok) continue;
      const rows = await r.json();
      const acc = new Map();
      const bidDate = new Map();
      rows.forEach((row) => {
        const k = `${cid}|${row.listing}`;
        if (!acc.has(k)) acc.set(k, { from: fromStr, to: latest, days: 7, spend: 0, clicks: 0, impressions: 0, orders: 0, sales: 0, bid: null });
        const a = acc.get(k);
        a.spend += Number(row.spend) || 0;
        a.clicks += Number(row.clicks) || 0;
        a.impressions += Number(row.impressions) || 0;
        a.orders += Number(row.orders_14d) || 0;
        a.sales += Number(row.sales_14d) || 0;
        if (!bidDate.has(k) || row.report_date > bidDate.get(k)) { bidDate.set(k, row.report_date); a.bid = Number(row.default_bid); }
      });
      acc.forEach((v, k) => out.set(k, v));
    }
  } catch (e) { /* snapshot loi thi bo qua, khong chan viec doi bid */ }
  return out;
}

module.exports = async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'POST') return res.status(405).json({ ok: false, error: 'Chi ho tro POST' });

  const need = ['ADS_EDIT_PIN', 'WAYFAIR_CLIENT_ID', 'WAYFAIR_CLIENT_SECRET', 'SUPABASE_URL', 'SUPABASE_SERVICE_KEY'];
  const missing = need.filter((k) => !process.env[k]);
  if (missing.length) return res.status(500).json({ ok: false, error: `Thieu bien moi truong trong Vercel: ${missing.join(', ')}` });

  let body = req.body;
  if (typeof body === 'string') { try { body = JSON.parse(body); } catch (e) { body = null; } }
  if (!body || typeof body !== 'object') return res.status(400).json({ ok: false, error: 'Body khong hop le' });

  if (!safeEqual(body.pin || '', process.env.ADS_EDIT_PIN)) {
    await sleep(800); // lam cham do PIN
    return res.status(401).json({ ok: false, error: 'Sai PIN' });
  }

  const maxPct = Number(process.env.ADS_MAX_CHANGE_PCT) || 50;
  const maxBid = Number(process.env.ADS_MAX_BID) || 20;
  const input = Array.isArray(body.changes) ? body.changes : [];
  if (!input.length || input.length > 200) return res.status(400).json({ ok: false, error: 'Can 1 den 200 thay doi' });

  const rejected = [];
  const valid = [];
  input.forEach((c) => {
    const campaign_id = String(c.campaign_id || '').trim();
    const listing = String(c.listing || '').trim();
    const ref = { campaign_id, listing };
    if (!/^\d{1,12}$/.test(campaign_id) || !/^[A-Za-z0-9_-]{1,40}$/.test(listing)) {
      return rejected.push({ ...ref, status: 'REJECTED', error: 'campaign_id/listing khong hop le' });
    }
    const hasBid = c.new_bid !== null && c.new_bid !== undefined && c.new_bid !== '';
    const hasActive = typeof c.new_active === 'boolean';
    if (!hasBid && !hasActive) return rejected.push({ ...ref, status: 'REJECTED', error: 'Khong co gi de doi' });
    let newBid = null;
    if (hasBid) {
      newBid = Math.floor(Number(c.new_bid) * 100) / 100;   // doc: bid bi cat con 2 so thap phan
      if (!isFinite(newBid) || newBid < MIN_BID) return rejected.push({ ...ref, status: 'REJECTED', error: `Bid toi thieu $${MIN_BID}` });
      if (newBid > maxBid) return rejected.push({ ...ref, status: 'REJECTED', error: `Bid vuot muc toi da $${maxBid} (ADS_MAX_BID)` });
      const old = Number(c.old_bid);
      if (old > 0 && !body.allow_big && Math.abs(newBid - old) / old * 100 > maxPct) {
        return rejected.push({ ...ref, status: 'REJECTED', error: `Doi hon ${maxPct}% (can tick "cho phep doi lon")` });
      }
    }
    valid.push({
      campaign_id, listing, product_name: c.product_name || null, campaign_name: c.campaign_name || null,
      old_bid: c.old_bid != null && c.old_bid !== '' ? Number(c.old_bid) : null, new_bid: newBid,
      old_active: typeof c.old_active === 'boolean' ? c.old_active : null,
      new_active: hasActive ? c.new_active : null,
    });
  });

  const results = [...rejected];
  const logRows = [];
  const batchId = crypto.randomUUID();

  if (valid.length) {
    const snaps = await buildSnapshots(valid);

    // Gom theo campaign, moi lan toi da 50 listing
    const byCampaign = new Map();
    valid.forEach((c) => {
      if (!byCampaign.has(c.campaign_id)) byCampaign.set(c.campaign_id, []);
      byCampaign.get(c.campaign_id).push(c);
    });

    for (const [cid, items] of byCampaign) {
      for (let i = 0; i < items.length; i += MAX_PER_CALL) {
        const chunk = items.slice(i, i + MAX_PER_CALL);
        const build = (withOldBid) => {
          const listings = {};
          chunk.forEach((c) => {
            const e = {};
            if (c.new_bid != null) e.bid = c.new_bid.toFixed(2);
            else if (withOldBid && c.old_bid != null) e.bid = Number(c.old_bid).toFixed(2); // chi khi API doi bid di kem isActive
            if (c.new_active != null) e.isActive = c.new_active;
            listings[c.listing] = e;
          });
          return listings;
        };
        let r = await callCampaign(cid, build(false));
        const onlyToggle = chunk.some((c) => c.new_bid == null && c.new_active != null);
        if (!r.ok && r.status === 400 && onlyToggle && /bid/i.test(JSON.stringify(r.json || r.text || ''))) {
          r = await callCampaign(cid, build(true)); // thu lai kem bid cu neu API bat buoc co bid
        }
        const changes = (r.json && r.json.listingChanges) || [];
        const errText = r.ok ? null : `HTTP ${r.status}: ${((r.json && (r.json.details || r.json.message)) || r.text || '').toString().slice(0, 300)}`;
        chunk.forEach((c) => {
          const lc = changes.find((x) => x.listing === c.listing);
          const success = r.ok && lc && String(lc.status).toUpperCase() === 'SUCCESS';
          const status = success ? 'SUCCESS' : 'FAILED';
          const error = success ? null : (errText || `Wayfair tra ve status ${lc ? lc.status : 'khong ro'}`);
          results.push({ campaign_id: c.campaign_id, listing: c.listing, status, error, bid: lc ? lc.bid : null, isActive: lc && typeof lc.isActive === 'boolean' ? lc.isActive : null });
          logRows.push({
            batch_id: batchId, campaign_id: c.campaign_id, campaign_name: c.campaign_name,
            listing: c.listing, product_name: c.product_name,
            action: c.new_bid != null && c.new_active != null ? 'both' : (c.new_bid != null ? 'bid' : 'active'),
            old_bid: c.old_bid, new_bid: c.new_bid, old_active: c.old_active, new_active: c.new_active,
            status, error, note: body.note ? String(body.note).slice(0, 300) : null,
            snapshot: snaps.get(`${c.campaign_id}|${c.listing}`) || null,
          });
        });
        await sleep(120);
      }
    }
  }

  let logError = null;
  if (logRows.length) {
    try {
      const r = await fetch(`${process.env.SUPABASE_URL}/rest/v1/ads_bid_changes`, {
        method: 'POST', headers: sbHeaders({ Prefer: 'return=minimal' }), body: JSON.stringify(logRows),
      });
      if (!r.ok) logError = `Luu lich su loi ${r.status}: ${(await r.text()).slice(0, 200)}`;
    } catch (e) { logError = `Luu lich su loi: ${e.message}`; }
  }

  return res.status(200).json({ ok: true, results, log_error: logError });
};
