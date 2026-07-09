"""Phase 3: Generate a static dashboard for GitHub Pages.

Reads data/portfolio.json and data/signals.json, plus the dashboard settings in
holdings.yaml, and writes:
  - data/dashboard.json  (sanitized data archive)
  - docs/index.html      (dashboard copy)
  - index.html           (GitHub Pages source for this repo)

Privacy: if dashboard.hide_amounts is true, rupee values are omitted and only
percentages + signals are published (safe for a public GitHub Pages site).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _settings() -> dict:
    with open(os.path.join(ROOT, "holdings.yaml"), "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg.get("dashboard") or {}


def resolve_password(settings: dict) -> str:
    """Resolve the dashboard password.

    Order of precedence:
      1. DASHBOARD_PASSWORD env var (set directly, or by the 1Password
         load-secrets GitHub Action in CI).
      2. 1Password CLI: `op read <password_op_ref>` for local use, where
         password_op_ref is set in holdings.yaml (e.g.
         op://Personal/Market Dashboard/password).
    Returns "" if no password is available (amounts simply stay hidden).
    """
    env = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if env:
        return env

    ref = (settings.get("password_op_ref") or "").strip()
    if ref and shutil.which("op"):
        try:
            out = subprocess.run(
                ["op", "read", ref],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0:
                return out.stdout.strip()
            print(f"  ! 1Password read failed: {out.stderr.strip()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 1Password CLI error: {exc}")
    return ""


def _data_base_url() -> str:
    """Raw base URL of the `data` branch, derived from the git remote.

    GitHub Pages serves this dashboard from `main`, but the time-series market
    data lives on the separate `data` branch. The page fetches that JSON at
    runtime from raw.githubusercontent.com (which sends permissive CORS
    headers). Overridable via the DATA_BASE_URL env var.
    """
    override = os.environ.get("DATA_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    slug = "nihal467/Market"
    try:
        out = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=10, cwd=ROOT,
        )
        url = out.stdout.strip()
        if url:
            # Normalize git@github.com:owner/repo.git or https URLs to owner/repo.
            url = url.replace("git@github.com:", "").replace(
                "https://github.com/", "")
            if url.endswith(".git"):
                url = url[:-4]
            if "/" in url:
                slug = url
    except Exception:  # noqa: BLE001
        pass
    return f"https://raw.githubusercontent.com/{slug}/data"


def build_data() -> dict:
    settings = _settings()
    hide = bool(settings.get("hide_amounts", True))
    portfolio = _load(os.path.join(DATA_DIR, "portfolio.json"))
    signals = _load(os.path.join(DATA_DIR, "signals.json"))

    sig_by_key = {}
    for s in signals.get("signals", []):
        sig_by_key[(s.get("broker", ""), s.get("name", ""))] = s

    positions = []
    for p in portfolio.get("positions", []):
        sig = sig_by_key.get((p.get("broker", ""), p.get("name", "")), {})
        row = {
            "name": p["name"],
            "broker": p["broker"],
            "type": p["type"],
            "tradable": sig.get("tradable", False),
            "allocation_pct": p.get("allocation_pct"),
            "pnl_pct": p.get("pnl_pct"),
            "signal": sig.get("signal", "—"),
            "reasons": sig.get("reasons", []),
            "rsi": (sig.get("indicators") or {}).get("rsi14") if sig.get("indicators") else None,
        }
        if not hide:
            row["invested"] = p.get("invested")
            row["current_value"] = p.get("current_value")
            row["pnl"] = p.get("pnl")
        positions.append(row)

    totals_src = portfolio.get("totals", {})
    totals = {"pnl_pct": totals_src.get("pnl_pct")}
    if not hide:
        totals.update({
            "invested": totals_src.get("invested"),
            "current_value": totals_src.get("current_value"),
            "pnl": totals_src.get("pnl"),
        })

    # Only the actively-traded (₹5L lumpsum) holdings get buy/sell tallies.
    counts = {"BUY": 0, "HOLD": 0, "SELL": 0}
    for p in positions:
        if p.get("tradable") and p["signal"] in counts:
            counts[p["signal"]] += 1

    return {
        "title": settings.get("title", "My Investment Dashboard"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hide_amounts": hide,
        "totals": totals,
        "signal_counts": counts,
        "positions": positions,
        "disclaimer": signals.get("disclaimer", "Not investment advice."),
        "data_base_url": _data_base_url(),
    }


def _gather_secret(portfolio: dict) -> dict:
    """Collect the rupee amounts to be encrypted (never published in plaintext)."""
    by_id = {}
    for p in portfolio.get("positions", []):
        by_id[p.get("identifier")] = {
            "invested": p.get("invested"),
            "current_value": p.get("current_value"),
            "pnl": p.get("pnl"),
        }
    t = portfolio.get("totals", {})
    return {
        "totals": {
            "invested": t.get("invested"),
            "current_value": t.get("current_value"),
            "pnl": t.get("pnl"),
        },
        "by_id": by_id,
    }


def write() -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)
    data = build_data()

    # Optionally embed AES-encrypted amounts, unlockable in-browser with a
    # password. The password is resolved from DASHBOARD_PASSWORD (env / CI
    # secret) or from 1Password via password_op_ref. It is NEVER written to the
    # repo — only ciphertext is.
    password = resolve_password(_settings())
    if password and data.get("hide_amounts"):
        from secret_box import encrypt_payload
        portfolio = _load(os.path.join(DATA_DIR, "portfolio.json"))
        secret = _gather_secret(portfolio)
        # Map by_id -> by position index so the page can match rows after decrypt.
        by_index = {}
        for i, p in enumerate(portfolio.get("positions", [])):
            by_index[i] = {
                "invested": p.get("invested"),
                "current_value": p.get("current_value"),
                "pnl": p.get("pnl"),
            }
        secret_payload = {"totals": secret["totals"], "by_index": by_index}
        data["secret"] = encrypt_payload(secret_payload, password)
        data["has_secret"] = True
        print("  amounts encrypted (password-protected unlock enabled)")
    else:
        data["has_secret"] = False

    payload = json.dumps(data, indent=2)
    # Keep a standalone data.json (useful if served via Pages / for history).
    with open(os.path.join(DATA_DIR, "dashboard.json"), "w", encoding="utf-8") as fh:
        fh.write(payload)
    # Self-contained page: data embedded so it works by double-clicking locally
    # (no web server needed) and also when served via GitHub Pages.
    html = INDEX_HTML.replace("/*__DATA__*/null", payload)
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Dashboard written -> {os.path.join(DOCS_DIR, 'index.html')}")
    print(f"Dashboard written -> {os.path.join(ROOT, 'index.html')}")
    print(f"  positions: {len(data['positions'])}  signals: {data['signal_counts']}")


INDEX_HTML = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>Investment Dashboard</title>
<style>
  :root { --bg:#0e1116; --card:#171c24; --line:#222a35; --txt:#e6edf3; --mut:#8b97a7;
          --buy:#1f9d55; --sell:#e5484d; --hold:#9aa4b2; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--txt); }
  .wrap { max-width:1000px; margin:0 auto; padding:24px 16px 60px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--mut); font-size:13px; margin-bottom:20px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:12px; margin-bottom:24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px; min-width:0; }
  .card .k { color:var(--mut); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .card .v { font-size:24px; font-weight:600; margin-top:6px; overflow-wrap:anywhere; }
  .card .v.compact { font-size:18px; line-height:1.25; }
  table { width:100%; border-collapse:collapse; background:var(--card);
          border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line);
          font-size:14px; vertical-align:top; }
  th { color:var(--mut); font-weight:500; font-size:12px; text-transform:uppercase; }
  tr:last-child td { border-bottom:none; }
  .pill { display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px;
          font-weight:600; }
  .BUY { background:rgba(31,157,85,.18); color:var(--buy); }
  .SELL { background:rgba(229,72,77,.18); color:var(--sell); }
  .HOLD { background:rgba(154,164,178,.15); color:var(--hold); }
  .SIP { background:rgba(47,129,247,.15); color:#6cb0ff; }
  .pos { color:var(--buy); } .neg { color:var(--sell); }
  .reasons { color:var(--mut); font-size:12px; margin-top:4px; }
  .foot { color:var(--mut); font-size:12px; margin-top:20px; line-height:1.5; }
  .broker { color:var(--mut); font-size:12px; }
  .lock { display:flex; gap:8px; align-items:center; margin-bottom:20px; flex-wrap:wrap; }
  .lock input { background:var(--card); border:1px solid var(--line); color:var(--txt);
                padding:8px 10px; border-radius:8px; font-size:14px; }
  .lock button { background:#2f81f7; color:#fff; border:0; padding:8px 14px;
                 border-radius:8px; font-size:14px; cursor:pointer; }
  .lock button:hover { background:#2670d6; }
  .lock .msg { color:var(--mut); font-size:12px; }
  .lock .msg.err { color:var(--sell); }
  .val { white-space:nowrap; }
  .maintabs { display:flex; gap:8px; margin:20px 0; border-bottom:1px solid var(--line); }
  .maintab { background:none; border:0; border-bottom:2px solid transparent; color:var(--mut);
             padding:10px 6px; margin-bottom:-1px; font-size:15px; font-weight:600;
             cursor:pointer; }
  .maintab.active { color:var(--txt); border-bottom-color:#2f81f7; }
  .sec { font-size:18px; margin:34px 0 4px; }
  .secsub { color:var(--mut); font-size:12px; font-weight:400; }
  .livebanner { background:linear-gradient(180deg,rgba(31,157,85,.10),transparent);
                border:1px solid var(--line); border-left:3px solid var(--buy);
                border-radius:10px; padding:14px 16px 4px; margin-bottom:22px; }
  .livebanner.down { background:linear-gradient(180deg,rgba(229,72,77,.10),transparent);
                     border-left-color:var(--sell); }
  .livehead { display:flex; align-items:center; gap:8px; font-size:12px; font-weight:700;
              letter-spacing:.05em; color:var(--mut); text-transform:uppercase; margin-bottom:12px;
              flex-wrap:wrap; overflow-wrap:anywhere; }
  .livedot { width:9px; height:9px; border-radius:50%; background:var(--buy);
             box-shadow:0 0 0 0 rgba(31,157,85,.6); animation:pulse 1.6s infinite; }
  .livebanner.down .livedot { background:var(--sell); box-shadow:0 0 0 0 rgba(229,72,77,.6); }
  @keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(31,157,85,.5);} 70%{box-shadow:0 0 0 7px rgba(31,157,85,0);} 100%{box-shadow:0 0 0 0 rgba(31,157,85,0);} }
  .livebanner .cards { margin-bottom:8px; }
  .srcbadge { font-size:10px; font-weight:700; padding:2px 7px; border-radius:10px; margin-left:8px; letter-spacing:.03em; }
  .srcbadge.rt { background:rgba(31,157,85,.18); color:var(--buy); }
  .srcbadge.dl { background:rgba(154,164,178,.15); color:var(--mut); }
  .tabs { display:flex; gap:8px; margin:12px 0 14px; flex-wrap:wrap; }
  .tab { background:var(--card); border:1px solid var(--line); color:var(--mut);
         padding:7px 14px; border-radius:8px; font-size:13px; cursor:pointer; }
  .tab.active { background:#2f81f7; color:#fff; border-color:#2f81f7; }
  .empty { color:var(--mut); font-size:13px; padding:14px 0; }
  .up { color:var(--buy); } .down { color:var(--sell); }
  .flag { display:inline-block; padding:1px 8px; border-radius:20px; font-size:11px;
          margin-left:6px; background:rgba(154,164,178,.15); color:var(--mut); }
  .flag.big_move_up { background:rgba(31,157,85,.18); color:var(--buy); }
  .flag.big_move_down { background:rgba(229,72,77,.18); color:var(--sell); }
  .flag.rsi_oversold { background:rgba(31,157,85,.14); color:var(--buy); }
  .flag.rsi_overbought { background:rgba(229,72,77,.14); color:var(--sell); }
  .rank { color:var(--mut); font-size:12px; }
  .chg { color:var(--mut); font-size:12px; margin-top:2px; }
  .mut { color:var(--mut); }
  .txlist { display:grid; gap:8px; margin-bottom:22px; }
  .tx { display:grid; grid-template-columns:1fr auto; gap:8px 12px; background:var(--card);
        border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
  .tx .meta { color:var(--mut); font-size:11px; text-transform:uppercase; letter-spacing:.03em; }
  .tx .name { font-size:14px; font-weight:700; overflow-wrap:anywhere; }
  .tx .detail { color:var(--mut); font-size:12px; line-height:1.35; }
  .tx .side { text-align:right; }
  .tx .value { font-size:14px; font-weight:700; margin-top:4px; }
  .filters { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:8px 0 12px; }
  .filters label { color:var(--mut); font-size:12px; font-weight:700; text-transform:uppercase; }
  .filters select { background:var(--card); border:1px solid var(--line); color:var(--txt);
                    border-radius:8px; padding:7px 10px; font-size:13px; }
  .panel, .livebanner { min-width:0; max-width:100%; }
  #panel-paper table { display:block; max-width:100%; overflow-x:auto; }
  #panel-paper th, #panel-paper td { white-space:nowrap; }
  #panel-paper td:nth-child(2), #panel-paper td:nth-child(3), #panel-paper td:nth-child(4) {
    white-space:normal; min-width:120px;
  }
  @media (max-width: 520px) {
    .wrap { padding:18px 14px 44px; overflow-x:hidden; }
    .cards { grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
    .card { padding:12px; border-radius:8px; }
    .card .v { font-size:20px; }
    .card .v.compact { font-size:15px; }
    .livebanner { padding:12px 12px 2px; }
    th,td { padding:9px 10px; font-size:12px; }
    .maintabs { gap:4px; }
    .maintab { font-size:13px; padding:9px 4px; flex:1; }
  }
</style>
</head>
<body>
<div class=\"wrap\">
  <h1 id=\"title\">Investment Dashboard</h1>
  <div class=\"sub\" id=\"updated\">Loading…</div>

  <div class=\"maintabs\">
    <button class=\"maintab active\" data-panel=\"investment\">💼 My Investment</button>
    <button class=\"maintab\" data-panel=\"market\">📈 Market Watch</button>
    <button class=\"maintab\" data-panel=\"paper\">🤖 Dummy ₹5L</button>
  </div>

  <section id=\"panel-investment\" class=\"panel\">
    <div class=\"lock\" id=\"lock\" style=\"display:none\">
      <span>🔒 Amounts hidden.</span>
      <input id=\"pw\" type=\"password\" placeholder=\"Enter password\" autocomplete=\"off\" />
      <button id=\"unlock\">Show amounts</button>
      <span class=\"msg\" id=\"lockmsg\"></span>
    </div>
    <div class=\"cards\" id=\"cards\"></div>
    <table>
      <thead><tr>
        <th>Holding</th><th>Signal</th><th>RSI</th><th>Alloc %</th><th>Return %</th>
        <th class=\"amtcol\" style=\"display:none\">Invested</th>
        <th class=\"amtcol\" style=\"display:none\">Value</th>
      </tr></thead>
      <tbody id=\"rows\"></tbody>
    </table>
    <div class=\"foot\" id=\"foot\"></div>
  </section>

  <section id=\"panel-market\" class=\"panel\" style=\"display:none\">
    <div class=\"sub\" id=\"mkt-updated\"></div>
    <div class=\"tabs\">
      <button class=\"tab active\" data-tab=\"movers\">Movers (today)</button>
      <button class=\"tab\" data-tab=\"changes\">Changes (24h)</button>
      <button class=\"tab\" data-tab=\"watchlist\">Top 50 watchlist</button>
    </div>
    <div id=\"mkt-body\"><div class=\"empty\">Loading market data…</div></div>
  </section>

  <section id=\"panel-paper\" class=\"panel\" style=\"display:none\">
    <div class=\"sub\" id=\"paper-updated\"></div>
    <div id=\"paper-body\"><div class=\"empty\">Loading paper-trading bot…</div></div>
  </section>
</div>
<script>
function pct(v){ return (v===null||v===undefined) ? '—' : v.toFixed(2)+'%'; }
function cls(v){ return (v>0)?'pos':(v<0)?'neg':''; }
function inr(v){ return (v===null||v===undefined) ? '—'
  : '₹'+Number(v).toLocaleString('en-IN',{maximumFractionDigits:0}); }
const d = /*__DATA__*/null;

function b64(s){ return Uint8Array.from(atob(s), c=>c.charCodeAt(0)); }
async function decryptSecret(password, sec){
  const enc = new TextEncoder();
  const baseKey = await crypto.subtle.importKey(
    'raw', enc.encode(password), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    { name:'PBKDF2', salt:b64(sec.salt), iterations:sec.iter, hash:'SHA-256' },
    baseKey, { name:'AES-GCM', length:256 }, false, ['decrypt']);
  const pt = await crypto.subtle.decrypt(
    { name:'AES-GCM', iv:b64(sec.iv) }, key, b64(sec.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}

(function(){
  if(!d){ document.getElementById('updated').textContent='No data'; return; }
  document.getElementById('title').textContent = d.title;
  document.title = d.title;
  document.getElementById('updated').textContent =
    'Updated ' + new Date(d.generated_at).toLocaleString();

  // Top-level panel switching (My Investment / Market Watch / Dummy ₹5L).
  document.querySelectorAll('.maintab').forEach(t=>
    t.addEventListener('click', ()=>{
      const panel = t.getAttribute('data-panel');
      document.querySelectorAll('.maintab').forEach(x=>
        x.classList.toggle('active', x===t));
      document.getElementById('panel-investment').style.display =
        panel==='investment' ? '' : 'none';
      document.getElementById('panel-market').style.display =
        panel==='market' ? '' : 'none';
      document.getElementById('panel-paper').style.display =
        panel==='paper' ? '' : 'none';
    }));

  function renderCards(extra){
    const c = d.signal_counts || {};
    const cards = [
      ['Buy', c.BUY||0, 'BUY'], ['Hold', c.HOLD||0, 'HOLD'], ['Sell', c.SELL||0, 'SELL'],
      ['Total return', pct(d.totals && d.totals.pnl_pct), '']
    ];
    if(extra){
      cards.push(['Invested', inr(extra.totals.invested), '']);
      cards.push(['Current value', inr(extra.totals.current_value), '']);
      cards.push(['Total P&L', inr(extra.totals.pnl), extra.totals.pnl>=0?'BUY':'SELL']);
    } else if(!d.hide_amounts && d.totals){
      cards.push(['Invested', inr(d.totals.invested), '']);
      cards.push(['Current value', inr(d.totals.current_value), '']);
    }
    document.getElementById('cards').innerHTML = cards.map(x=>
      `<div class=card><div class=k>${x[0]}</div><div class=\"v ${x[2]}\">${x[1]}</div></div>`
    ).join('');
  }

  function showAmtCols(){ document.querySelectorAll('.amtcol').forEach(e=>e.style.display=''); }

  document.getElementById('rows').innerHTML = d.positions.map((p,i)=>{
    const reasons = (p.reasons||[]).slice(0,3).join(' · ');
    let invCell='', valCell='';
    if(!d.hide_amounts){
      invCell = `<td class=\"amt\">${inr(p.invested)}</td>`;
      valCell = `<td class=\"amt\">${inr(p.current_value)}</td>`;
    } else if(d.has_secret){
      invCell = `<td class=\"amt inv\" data-i=\"${i}\" style=\"display:none\">🔒</td>`;
      valCell = `<td class=\"amt val\" data-i=\"${i}\" style=\"display:none\">🔒</td>`;
    }
    return `<tr>
      <td><div>${p.name}</div><div class=broker>${p.broker}</div>
          <div class=reasons>${reasons}</div></td>
      <td><span class=\"pill ${p.signal}\">${p.signal}</span></td>
      <td>${p.rsi===null||p.rsi===undefined?'—':p.rsi}</td>
      <td>${pct(p.allocation_pct)}</td>
      <td class=\"${cls(p.pnl_pct)}\">${pct(p.pnl_pct)}</td>
      ${invCell}${valCell}
    </tr>`;
  }).join('');
  if(!d.hide_amounts){ showAmtCols(); }

  renderCards(null);
  document.getElementById('foot').textContent = d.disclaimer;

  if(d.has_secret && d.secret){
    const lock = document.getElementById('lock');
    lock.style.display = '';
    const msg = document.getElementById('lockmsg');
    async function attempt(){
      const pw = document.getElementById('pw').value;
      if(!pw){ return; }
      msg.className='msg'; msg.textContent='Decrypting…';
      try{
        const sec = await decryptSecret(pw, d.secret);
        renderCards(sec);
        showAmtCols();
        document.querySelectorAll('td.amt.inv[data-i]').forEach(td=>{
          const a = sec.by_index[td.getAttribute('data-i')] || {};
          td.style.display=''; td.textContent = inr(a.invested);
        });
        document.querySelectorAll('td.amt.val[data-i]').forEach(td=>{
          const a = sec.by_index[td.getAttribute('data-i')] || {};
          td.style.display=''; td.textContent = inr(a.current_value);
        });
        lock.style.display='none';
      }catch(e){
        msg.className='msg err'; msg.textContent='Wrong password.';
      }
    }
    document.getElementById('unlock').addEventListener('click', attempt);
    document.getElementById('pw').addEventListener('keydown', e=>{
      if(e.key==='Enter') attempt();
    });
  }

  // --- Market Watch: live data from the `data` branch (public, no amounts) ---
  initMarket();
  // --- Dummy ₹5L: virtual paper-trading bot results from the `data` branch ---
  initPaper();
})();

async function initMarket(){
  const base = (d && d.data_base_url) ? d.data_base_url : '';
  if(!base){ return; }
  const bust = '?t=' + Date.now();
  async function load(name){
    try{
      const r = await fetch(base + '/' + name + '/latest.json' + bust, {cache:'no-store'});
      if(!r.ok) return null;
      return await r.json();
    }catch(e){ return null; }
  }
  const [intraday, daily, watch] = await Promise.all(
    [load('intraday'), load('daily'), load('watchlist')]);

  const body = document.getElementById('mkt-body');
  const upd = document.getElementById('mkt-updated');
  const stamp = (watch && watch.generated_ist) || (daily && daily.ist) || '';
  if(stamp){ upd.textContent = '· watchlist built ' + new Date(stamp).toLocaleString(); }

  function pctCell(v){
    if(v===null||v===undefined) return '<span class=mut>—</span>';
    const c = v>0?'up':(v<0?'down':'');
    return '<span class=\"'+c+'\">'+(v>0?'+':'')+v.toFixed(2)+'%</span>';
  }
  function flags(arr){
    return (arr||[]).map(f=>'<span class=\"flag '+f+'\">'+f.replace(/_/g,' ')+'</span>').join('');
  }

  function renderMovers(){
    if(!intraday || !intraday.movers || !intraday.movers.length){
      return '<div class=empty>No movers right now. Intraday updates every ~15 min during market hours (NSE 09:15–15:30 IST).</div>';
    }
    const rows = intraday.movers.map(m=>`<tr>
      <td><div>${m.name||m.symbol}</div><div class=broker>${m.symbol}</div></td>
      <td>${pctCell(m.chg_pct)}</td>
      <td>${m.rsi==null?'—':m.rsi}</td>
      <td>${flags(m.flags)}</td></tr>`).join('');
    return `<div class=sub>${intraday.count||0} watched · ${intraday.movers.length} moving · ${new Date(intraday.ist).toLocaleTimeString()}</div>
      <table><thead><tr><th>Stock</th><th>Change</th><th>RSI</th><th>Flags</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  function renderChanges(){
    if(!daily){ return '<div class=empty>No daily analysis yet.</div>'; }
    const ch = daily.changes_since_prev || [];
    const c = daily.signal_counts || {};
    const head = `<div class=sub>EOD signals: <span class=up>${c.BUY||0} buy</span> · ${c.HOLD||0} hold · <span class=down>${c.SELL||0} sell</span> across ${daily.analyzed||0} stocks</div>`;
    if(!ch.length){ return head + '<div class=empty>No changes vs the previous run.</div>'; }
    const rows = ch.map(x=>{
      let what = x.type.replace(/_/g,' ');
      if(x.type==='signal_flip') what = `${x.from} → <b>${x.to}</b>`;
      else if(x.rsi!=null) what += ` (RSI ${x.rsi})`;
      return `<tr><td><div>${x.name||x.symbol}</div><div class=broker>${x.symbol}</div></td>
        <td>${what}</td></tr>`;
    }).join('');
    return head + `<table><thead><tr><th>Stock</th><th>Change since last run</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  function renderWatch(){
    if(!watch || !watch.watchlist || !watch.watchlist.length){
      return '<div class=empty>Watchlist not built yet. The weekly job runs Sunday 17:30 IST.</div>';
    }
    const rows = watch.watchlist.map(r=>`<tr>
      <td><span class=rank>#${r.rank}</span></td>
      <td><div>${r.name}</div><div class=broker>${r.symbol} · ${r.sector||''}</div></td>
      <td>${r.rsi14==null?'—':r.rsi14}</td>
      <td>${pctCell(r.ret_1d)}</td>
      <td>${pctCell(r.ret_1w)}</td>
      <td>${pctCell(r.ret_1m)}</td>
      <td>${pctCell(r.ret_3m)}</td>
      <td>${r.composite}</td></tr>`).join('');
    return `<div class=sub>Top ${watch.top_n} of ${watch.evaluated} evaluated · ranked by technicals + news</div>
      <table><thead><tr><th>#</th><th>Stock</th><th>RSI</th><th>1D</th><th>1W</th><th>1M</th><th>3M</th><th>Score</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  const views = {movers:renderMovers, changes:renderChanges, watchlist:renderWatch};
  function show(tab){
    document.querySelectorAll('.tab').forEach(t=>
      t.classList.toggle('active', t.getAttribute('data-tab')===tab));
    body.innerHTML = (views[tab]||renderMovers)();
  }
  document.querySelectorAll('.tab').forEach(t=>
    t.addEventListener('click', ()=>show(t.getAttribute('data-tab'))));
  show('movers');
}

async function initPaper(){
  const base = (d && d.data_base_url) ? d.data_base_url : '';
  const body = document.getElementById('paper-body');
  const upd = document.getElementById('paper-updated');
  if(!base){ body.innerHTML = '<div class=empty>No data source configured.</div>'; return; }
  const sign = v => (v>0?'+':'');
  async function load(name){
    try{
      const r = await fetch(base + '/' + name + '/latest.json?t=' + Date.now(), {cache:'no-store'});
      if(r.ok) return await r.json();
    }catch(e){}
    return null;
  }
  async function loadFile(path){
    try{
      const r = await fetch(base + '/' + path + '?t=' + Date.now(), {cache:'no-store'});
      if(r.ok) return await r.json();
    }catch(e){}
    return null;
  }
  const [p, bt, live, ready] = await Promise.all([
    load('paper'), load('backtest'), loadFile('paper/live.json'), loadFile('paper/readiness.json')
  ]);

  // --- LIVE intraday banner (only meaningful while market is open) ---
  function renderLive(){
    if(!live){ return ''; }
    const liveAgeMin = live.ist ? ((Date.now() - new Date(live.ist).getTime()) / 60000) : null;
    const freshLive = live.market_open && liveAgeMin !== null && liveAgeMin <= 90;
    const dcl = live.day_pnl>0?'up':(live.day_pnl<0?'down':'');
    const when = live.ist ? new Date(live.ist).toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'}) : '';
    const nb = (live.intraday_trades||[]).filter(t=>t.action==='BUY').length;
    const ns = (live.stops||[]).length;
    const sells = (live.intraday_trades||[]).filter(t=>t.action==='SELL').length;
    const shock = live.intraday_shock_mode;
    const sm = live.intraday_shock_metrics || {};
    const src = live.source_label || 'Yahoo Finance';
    const delay = live.delay || '~15-min delayed';
    const rt = live.realtime
      ? `<span class=\"srcbadge rt\">● real-time · ${src}</span>`
      : `<span class=\"srcbadge dl\">● ${delay} · ${src}</span>`;
    return `<div class=\"livebanner ${dcl}\">
      <div class=livehead><span class=livedot></span> ${freshLive?'LIVE':'LATEST LIVE SNAPSHOT'} · virtual ₹5L · updates every ~15 min during market hours ${rt}</div>
      <div class=cards>
        <div class=card><div class=k>Live value</div><div class=v>${inr(live.value)}</div>
          <div class=chg>as of ${when} IST${freshLive?'':' · not current'}</div></div>
        <div class=\"card\"><div class=k>Today's P&L (live)</div>
          <div class=\"v ${dcl}\">${sign(live.day_pnl)}${inr(live.day_pnl)}</div>
          <div class=\"chg ${dcl}\">${sign(live.day_pnl_pct)}${(live.day_pnl_pct||0).toFixed(2)}%</div></div>
        <div class=card><div class=k>Holdings</div><div class=v>${live.n_positions||0}</div>
          <div class=chg>cash ${inr(live.cash)}</div></div>
        <div class=card><div class=k>Intraday actions</div><div class=v>${nb} buys · ${sells} sells</div>
          <div class=chg>${shock?'shock guard active':(freshLive ? (live.realtime?'real-time':'delayed feed') : 'stale snapshot')}</div></div>
      </div>
      ${shock?`<div class=foot>Shock guard active: no new intraday buys. Book ${live.day_pnl_pct||0}% today; index proxy ${sm.index_drop_pct||0}%; watchlist down ${Math.round((sm.watchlist_down_fraction||0)*100)}%.</div>`:''}
      ${ns?`<div class=foot>Protective sells: ${(live.stops||[]).map(s=>`<b>${s.name||s.symbol}</b> ${s.type||'sell'} at ${s.loss_pct}%`).join(' · ')}</div>`:''}
      <div class=foot>${live.note||''}</div>
    </div>`;
  }

  // --- Backtest summary (historical proof) ---
  function renderBacktest(){
    if(!bt){ return ''; }
    const a = bt.alpha_pct, acl = (a>0)?'up':((a<0)?'down':'');
    const tcl = (bt.total_return_pct>0)?'up':((bt.total_return_pct<0)?'down':'');
    const pr = bt.params||{};
    const lab = ((bt.strategy_lab||{}).variants||[]).slice(0,5);
    const labRows = lab.length ? `<table><thead><tr><th>Variant</th><th>Return</th><th>Alpha</th><th>Turnover</th><th>Max DD</th></tr></thead><tbody>${
      lab.map(v=>{
        const rc=(v.total_return_pct>0)?'up':((v.total_return_pct<0)?'down':'');
        const ac=(v.alpha_pct>0)?'up':((v.alpha_pct<0)?'down':'');
        return `<tr><td>${v.label||v.id}</td><td class="${rc}">${sign(v.total_return_pct)}${(v.total_return_pct||0).toFixed(2)}%</td><td class="${ac}">${v.alpha_pct==null?'—':sign(v.alpha_pct)+v.alpha_pct.toFixed(2)+'%'}</td><td>${(v.turnover_pct_of_start||0).toFixed(0)}%</td><td class=down>${(v.max_drawdown_pct||0).toFixed(2)}%</td></tr>`;
      }).join('')
    }</tbody></table>` : '';
    return `<h2 class=sec>Backtest <span class=secsub>${bt.start_date} → ${bt.end_date} · ${bt.trading_days} trading days · ${bt.lookback} lookback</span></h2>
      <div class=cards>
        <div class=card><div class=k>Strategy return</div>
          <div class=\"v ${tcl}\">${sign(bt.total_return_pct)}${(bt.total_return_pct||0).toFixed(2)}%</div>
          <div class=chg>CAGR ${sign(bt.cagr_pct)}${(bt.cagr_pct||0).toFixed(2)}%</div></div>
        <div class=\"card\"><div class=k>vs ${bt.benchmark_name||'NIFTY 50'} (alpha)</div>
          <div class=\"v ${acl}\">${a==null?'—':sign(a)+a.toFixed(2)+'%'}</div>
          <div class=chg>index ${bt.benchmark_return_pct==null?'—':sign(bt.benchmark_return_pct)+bt.benchmark_return_pct.toFixed(2)+'%'}</div></div>
        <div class=card><div class=k>Max drawdown</div>
          <div class=\"v down\">${(bt.max_drawdown_pct||0).toFixed(2)}%</div>
          <div class=chg>worst peak-to-trough</div></div>
        <div class=card><div class=k>Sharpe</div><div class=v>${bt.sharpe}</div>
          <div class=chg>win days ${bt.win_rate_pct}%</div></div>
      </div>
      ${labRows}
      <div class=foot>${bt.note||''} Params: top ${pr.top_n}, ${pr.stop_loss_pct}% stop, ${pr.trailing_stop_pct||0}% trail, ${pr.max_drawdown_pct||0}% maxDD guard, ${pr.cost_per_side_pct}% cost/leg.</div>`;
  }

  function renderReadiness(){
    if(!ready){ return ''; }
    const s = ready.summary || {};
    const crit = ready.criteria || [];
    const decision = (ready.decision || 'incubating').replace(/_/g,' ');
    const profile = s.active_profile || '—';
    const profileLabel = profile === 'momentum_weekly_churn_control'
      ? 'Momentum weekly'
      : (profile === 'no_regime_filter' ? 'Aggressive no-regime'
        : (profile === 'aggressive_regime_guarded' ? 'Aggressive guarded'
        : profile.replace(/_/g,' ')));
    const passed = crit.filter(c=>c.blocking && c.passed).length;
    const blocking = crit.filter(c=>c.blocking).length;
    const cls = ready.decision === 'eligible_for_review' ? 'up'
      : (ready.decision === 'not_ready' ? 'down' : '');
    const rows = crit.map(c=>`<tr>
      <td>${c.passed?'PASS':'WAIT'}</td>
      <td>${c.label}</td>
      <td>${c.detail||''}</td>
    </tr>`).join('');
    return `<h2 class=sec>Dummy incubation <span class=secsub>one-month gate before real money</span></h2>
      <div class=cards>
        <div class=card><div class=k>Decision</div><div class="v ${cls}">${decision}</div>
          <div class=chg>${ready.note||''}</div></div>
        <div class=card><div class=k>Paper days</div><div class=v>${s.paper_days||0}/${s.min_trading_days||20}</div>
          <div class=chg>minimum trading days</div></div>
        <div class=card><div class=k>Blocking checks</div><div class=v>${passed}/${blocking}</div>
          <div class=chg>must pass before review</div></div>
        <div class=card><div class=k>Active profile</div><div class="v compact">${profileLabel}</div>
          <div class=chg>${profile}${s.best_lab_variant?(' · best lab: '+(s.best_lab_variant.label||s.best_lab_variant.id)):' · waiting for lab'}</div></div>
      </div>
      ${rows?`<table><thead><tr><th>Status</th><th>Criterion</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table>`:''}`;
  }

  if(!p){
    upd.textContent = bt ? ('· backtest ' + bt.start_date + '–' + bt.end_date) : '';
    body.innerHTML = renderLive() + renderReadiness() + renderBacktest()
      + '<div class=empty>Live paper trading has not placed its first trade yet. '
      + 'It runs after market close (16:30 IST) each trading day, simulating a virtual '
      + '₹5,00,000 across the day\\'s top BUY-ranked stocks.</div>';
    return;
  }

  if(p.ist){ upd.textContent = '· last run ' + new Date(p.ist).toLocaleString()
    + ' · since ' + (p.inception||'—'); }

  const dpos = p.day_pnl>0, dneg = p.day_pnl<0;
  const dcl = dpos?'up':(dneg?'down':'');
  const tcl = p.total_pnl>0?'up':(p.total_pnl<0?'down':'');
  const acl = (p.alpha_pct||0)>0?'up':((p.alpha_pct||0)<0?'down':'');
  const bname = p.benchmark_name||'NIFTY 50';

  function tradeTime(t, fallback){
    const raw = t.ist || t.time || t.generated_at || fallback || '';
    if(!raw) return '—';
    try{ return new Date(raw).toLocaleString('en-IN',{dateStyle:'medium',timeStyle:'short'}); }
    catch(e){ return raw; }
  }
  function tradeValue(t){
    const v = (t.qty||0) * (t.price||0);
    return v ? inr(v) : '—';
  }
  function tradeReason(t){
    return (t.reason || t.phase || 'paper_trade').replace(/_/g,' ');
  }
  function dateKey(raw){
    if(!raw) return '';
    try{
      const d = new Date(raw);
      if(!Number.isNaN(d.getTime())){
        return d.toLocaleDateString('en-CA', {timeZone:'Asia/Kolkata'});
      }
    }catch(e){}
    return String(raw).slice(0,10);
  }

  const liveAgeMin = live && live.ist ? ((Date.now() - new Date(live.ist).getTime()) / 60000) : null;
  const livePnl = live && live.market_open && liveAgeMin !== null && liveAgeMin <= 90 ? live : null;
  const pnlSource = livePnl || p;
  const pnlUpdated = livePnl ? live.ist : p.ist;
  const pnlDcl = (pnlSource.day_pnl||0)>0?'up':((pnlSource.day_pnl||0)<0?'down':'');
  const pnlTcl = (pnlSource.total_pnl||0)>0?'up':((pnlSource.total_pnl||0)<0?'down':'');
  const pnlPrevValue = pnlSource.value!=null && pnlSource.day_pnl!=null ? pnlSource.value - pnlSource.day_pnl : null;
  const pnlStartValue = pnlSource.start_capital || p.start_capital || 500000;
  const pnlOnly = `<h2 class=sec>P&L only <span class=secsub>${livePnl?'live market snapshot':'latest paper close'}</span></h2>
    <div class=cards>
      <div class=card><div class=k>Current value</div><div class=v>${inr(pnlSource.value)}</div>
        <div class=chg>${pnlUpdated ? 'updated ' + tradeTime({ist:pnlUpdated}) : ''}</div></div>
      <div class=card><div class=k>Today P&L vs prev close</div>
        <div class="v ${pnlDcl}">${sign(pnlSource.day_pnl)}${inr(pnlSource.day_pnl)}</div>
        <div class="chg ${pnlDcl}">${sign(pnlSource.day_pnl_pct)}${(pnlSource.day_pnl_pct||0).toFixed(2)}%${pnlPrevValue!=null?' from '+inr(pnlPrevValue):''}</div></div>
      <div class=card><div class=k>Total P&L vs start</div>
        <div class="v ${pnlTcl}">${sign(pnlSource.total_pnl)}${inr(pnlSource.total_pnl)}</div>
        <div class="chg ${pnlTcl}">${sign(pnlSource.total_pnl_pct)}${(pnlSource.total_pnl_pct||0).toFixed(2)}% from ${inr(pnlStartValue)}</div></div>
      <div class=card><div class=k>Cash</div><div class=v>${inr(pnlSource.cash)}</div>
        <div class=chg>${pnlSource.n_positions||0} holdings</div></div>
    </div>`;

  const cards = `<div class=cards>
    <div class=card><div class=k>Portfolio value</div><div class=v>${inr(p.value)}</div>
      <div class=chg>started ${inr(p.start_capital)}</div></div>
    <div class=\"card\"><div class=k>Today's P&L</div>
      <div class=\"v ${dcl}\">${sign(p.day_pnl)}${inr(p.day_pnl)}</div>
      <div class=\"chg ${dcl}\">${sign(p.day_pnl_pct)}${(p.day_pnl_pct||0).toFixed(2)}%</div></div>
    <div class=\"card\"><div class=k>Total P&L</div>
      <div class=\"v ${tcl}\">${sign(p.total_pnl)}${inr(p.total_pnl)}</div>
      <div class=\"chg ${tcl}\">${sign(p.total_pnl_pct)}${(p.total_pnl_pct||0).toFixed(2)}%</div></div>
    <div class=\"card\"><div class=k>vs ${bname} (alpha)</div>
      <div class=\"v ${acl}\">${sign(p.alpha_pct)}${(p.alpha_pct||0).toFixed(2)}%</div>
      <div class=chg>index ${sign(p.benchmark_pct)}${(p.benchmark_pct||0).toFixed(2)}% · ${inr(p.benchmark_value)}</div></div>
    <div class=card><div class=k>Holdings</div><div class=v>${p.n_positions||0}</div>
      <div class=chg>cash ${inr(p.cash)}</div></div>
  </div>`;

  const rs = p.risk_settings;
  const riskBar = rs
    ? `<div class=foot>🛡️ Risk controls: <b>${rs.stop_loss_pct}%</b> stop · <b>${rs.trailing_stop_pct||0}%</b> trail · <b>${rs.max_daily_loss_pct||0}%</b> daily loss guard · <b>${rs.max_drawdown_pct||0}%</b> drawdown guard · top <b>${rs.top_n}</b></div>`
    : '';

  const re = (p.risk_events||[]).filter(e=>e.type==='stop_loss');
  const riskEvents = re.length
    ? `<h2 class=sec>Risk actions today</h2><div class=foot>${re.map(e=>`🛑 Stopped out <b>${e.name||e.symbol}</b> at ${e.loss_pct}%`).join(' · ')}</div>`
    : '';

  const pos = (p.positions||[]).map(x=>`<tr>
    <td><div>${x.name||x.symbol}</div><div class=broker>${x.symbol}</div></td>
    <td>${x.qty}</td>
    <td>₹${(x.avg_price||0).toLocaleString('en-IN',{maximumFractionDigits:2})}</td>
    <td>₹${(x.price||0).toLocaleString('en-IN',{maximumFractionDigits:2})}</td>
    <td>${inr(x.value)}</td>
    <td class=\"${x.pnl>0?'up':(x.pnl<0?'down':'')}\">${sign(x.pnl)}${inr(x.pnl)} (${sign(x.pnl_pct)}${(x.pnl_pct||0).toFixed(2)}%)</td>
  </tr>`).join('');
  const posTable = pos
    ? `<h2 class=sec>Current positions</h2>
       <table><thead><tr><th>Stock</th><th>Qty</th><th>Avg</th><th>Price</th><th>Value</th><th>P&L</th></tr></thead>
       <tbody>${pos}</tbody></table>`
    : '<div class=empty>No open positions.</div>';

  const liveTrades = (live && live.intraday_trades ? live.intraday_trades : [])
    .map(t=>({...t, source:'Live market'}));
  const eodTrades = (p.today_trades||[]).map(t=>({...t, source:'EOD paper'}));
  const allTrades = liveTrades.concat(eodTrades);
  const latestTradeDate = (p.history||[]).length
    ? (p.history[p.history.length - 1].date || dateKey(p.ist))
    : dateKey(p.ist);
  function txCard(t){
    const key = dateKey(t.ist || t.date || p.ist);
    const sourceKey = t.source === 'Live market' ? 'live' : 'eod';
    return `<div class=tx data-date="${key}" data-source="${sourceKey}">
      <div>
        <div class=meta>${tradeTime(t, p.ist)} · ${t.source}</div>
        <div class=name>${t.name||t.symbol}</div>
        <div class=detail>${t.symbol||''} · qty ${t.qty||0} · ₹${(t.price||0).toLocaleString('en-IN',{maximumFractionDigits:2})}</div>
        <div class=detail>${tradeReason(t)}</div>
      </div>
      <div class=side>
        <div class="${t.action==='BUY'?'up':'down'}">${t.action}${(t.reason||'')==='stop_loss'?' STOP':''}</div>
        <div class=value>${tradeValue(t)}</div>
      </div>
    </div>`;
  }
  const trTable = `<h2 class=sec>Dummy transactions <span class=secsub>live market + daily paper allocation</span></h2>
     ${allTrades.length ? `<div class=filters>
       <label for=tx-filter>Show</label>
       <select id=tx-filter>
         <option value=today selected>Today only</option>
         <option value=live>Live market</option>
         <option value=eod>EOD paper</option>
         <option value=all>All</option>
       </select>
       <span class=mut id=tx-count></span>
     </div>
     <div class=txlist data-latest-date="${latestTradeDate}">${allTrades.map(txCard).join('')}</div>`
       : '<div class=empty>No dummy transactions recorded for the latest run. During market hours, live protective actions appear here; end-of-day allocation trades appear after the daily paper run.</div>'}`;

  const hist = (p.history||[]).slice().reverse();
  const histTable = hist.length
    ? `<h2 class=sec>Daily history <span class=secsub>(most recent first)</span></h2>
       <table><thead><tr><th>Date</th><th>Value</th><th>Day P&L</th><th>Total P&L</th></tr></thead>
       <tbody>${hist.map(h=>`<tr>
         <td>${h.date}</td><td>${inr(h.value)}</td>
         <td class=\"${h.day_pnl>0?'up':(h.day_pnl<0?'down':'')}\">${sign(h.day_pnl)}${inr(h.day_pnl)} (${sign(h.day_pnl_pct)}${(h.day_pnl_pct||0).toFixed(2)}%)</td>
         <td class=\"${h.total_pnl>0?'up':(h.total_pnl<0?'down':'')}\">${sign(h.total_pnl_pct)}${(h.total_pnl_pct||0).toFixed(2)}%</td></tr>`).join('')}</tbody></table>`
    : '';

  const note = p.strategy ? `<div class=foot>${p.strategy}</div>` : '';

  body.innerHTML = renderLive() + pnlOnly + renderReadiness() + trTable + cards + riskBar + riskEvents + posTable
    + histTable + renderBacktest() + note;
  setupTxFilter();
}

function setupTxFilter(){
  const sel = document.getElementById('tx-filter');
  const list = document.querySelector('.txlist');
  const count = document.getElementById('tx-count');
  if(!sel || !list){ return; }
  const latest = list.getAttribute('data-latest-date') || '';
  function apply(){
    let shown = 0;
    document.querySelectorAll('.tx').forEach(el=>{
      const mode = sel.value;
      const ok = mode === 'all'
        || (mode === 'today' && el.getAttribute('data-date') === latest)
        || (mode === el.getAttribute('data-source'));
      el.style.display = ok ? '' : 'none';
      if(ok) shown += 1;
    });
    if(count){ count.textContent = `${shown} shown`; }
  }
  sel.addEventListener('change', apply);
  apply();
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    write()
