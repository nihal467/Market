"""Phase 3: Generate a static dashboard for GitHub Pages.

Reads data/portfolio.json and data/signals.json, plus the dashboard settings in
holdings.yaml, and writes:
  - docs/data.json   (sanitized data the page fetches)
  - docs/index.html  (the dashboard, committed once)

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
    print(f"Dashboard written -> {os.path.join(DOCS_DIR, 'index.html')}")
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
          padding:14px; }
  .card .k { color:var(--mut); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .card .v { font-size:24px; font-weight:600; margin-top:6px; }
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
      <td>${pctCell(r.ret_1m)}</td>
      <td>${pctCell(r.ret_3m)}</td>
      <td>${r.composite}</td></tr>`).join('');
    return `<div class=sub>Top ${watch.top_n} of ${watch.evaluated} evaluated · ranked by technicals + news</div>
      <table><thead><tr><th>#</th><th>Stock</th><th>RSI</th><th>1M</th><th>3M</th><th>Score</th></tr></thead>
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
  const [p, bt] = await Promise.all([load('paper'), load('backtest')]);

  // --- Backtest summary (historical proof) ---
  function renderBacktest(){
    if(!bt){ return ''; }
    const a = bt.alpha_pct, acl = (a>0)?'up':((a<0)?'down':'');
    const tcl = (bt.total_return_pct>0)?'up':((bt.total_return_pct<0)?'down':'');
    const pr = bt.params||{};
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
      <div class=foot>${bt.note||''} Params: top ${pr.top_n}, ${pr.stop_loss_pct}% stop, ${pr.max_position_pct}% max/stock, ${pr.max_sector_pct}% max/sector, ${pr.cost_per_side_pct}% cost/leg.</div>`;
  }

  if(!p){
    upd.textContent = bt ? ('· backtest ' + bt.start_date + '–' + bt.end_date) : '';
    body.innerHTML = renderBacktest()
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
    ? `<div class=foot>🛡️ Risk controls: <b>${rs.stop_loss_pct}%</b> stop-loss · <b>${rs.max_position_pct}%</b> max per stock · <b>${rs.max_sector_pct}%</b> max per sector · top <b>${rs.top_n}</b> equal-weight</div>`
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

  const tr = (p.today_trades||[]);
  const trTable = tr.length
    ? `<h2 class=sec>Today's trades</h2>
       <table><thead><tr><th>Action</th><th>Stock</th><th>Qty</th><th>Price</th></tr></thead>
       <tbody>${tr.map(t=>`<tr>
         <td class=\"${t.action==='BUY'?'up':'down'}\">${t.action}${t.reason==='stop_loss'?' 🛑':''}</td>
         <td>${t.name||t.symbol}</td><td>${t.qty}</td>
         <td>₹${(t.price||0).toLocaleString('en-IN',{maximumFractionDigits:2})}</td></tr>`).join('')}</tbody></table>`
    : '';

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

  body.innerHTML = cards + riskBar + riskEvents + trTable + posTable
    + histTable + renderBacktest() + note;
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    write()
