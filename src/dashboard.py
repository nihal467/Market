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


def build_data() -> dict:
    settings = _settings()
    hide = bool(settings.get("hide_amounts", True))
    portfolio = _load(os.path.join(DATA_DIR, "portfolio.json"))
    signals = _load(os.path.join(DATA_DIR, "signals.json"))

    sig_by_symbol = {}
    for s in signals.get("signals", []):
        sym = s["symbol"]
        sig_by_symbol[sym] = s
        if sym.startswith("MF:"):
            sig_by_symbol[sym[3:]] = s  # also key by bare scheme code

    positions = []
    for p in portfolio.get("positions", []):
        sym = p.get("identifier")
        sig = sig_by_symbol.get(sym, {})
        row = {
            "name": p["name"],
            "broker": p["broker"],
            "type": p["type"],
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

    counts = {"BUY": 0, "HOLD": 0, "SELL": 0}
    for p in positions:
        if p["signal"] in counts:
            counts[p["signal"]] += 1

    return {
        "title": settings.get("title", "My Investment Dashboard"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hide_amounts": hide,
        "totals": totals,
        "signal_counts": counts,
        "positions": positions,
        "disclaimer": signals.get("disclaimer", "Not investment advice."),
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
    # password. The password comes from the DASHBOARD_PASSWORD env var (a GitHub
    # Secret in CI) and is NEVER written to the repo — only ciphertext is.
    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if password and data.get("hide_amounts"):
        from secret_box import encrypt_payload
        portfolio = _load(os.path.join(DATA_DIR, "portfolio.json"))
        secret = _gather_secret(portfolio)
        # Map by_id -> by position index so the page can match rows after decrypt.
        by_index = {}
        for i, p in enumerate(portfolio.get("positions", [])):
            by_index[i] = secret["by_id"].get(p.get("identifier"))
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
</style>
</head>
<body>
<div class=\"wrap\">
  <h1 id=\"title\">Investment Dashboard</h1>
  <div class=\"sub\" id=\"updated\">Loading…</div>
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
      <th id=\"valhdr\" style=\"display:none\">Value</th>
    </tr></thead>
    <tbody id=\"rows\"></tbody>
  </table>
  <div class=\"foot\" id=\"foot\"></div>
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

  function renderCards(extra){
    const c = d.signal_counts || {};
    const cards = [
      ['Buy', c.BUY||0, 'BUY'], ['Hold', c.HOLD||0, 'HOLD'], ['Sell', c.SELL||0, 'SELL'],
      ['Total return', pct(d.totals && d.totals.pnl_pct), '']
    ];
    if(extra){
      cards.push(['Current value', inr(extra.totals.current_value), '']);
      cards.push(['Total P&L', inr(extra.totals.pnl), extra.totals.pnl>=0?'BUY':'SELL']);
    } else if(!d.hide_amounts && d.totals){
      cards.push(['Current value', inr(d.totals.current_value), '']);
    }
    document.getElementById('cards').innerHTML = cards.map(x=>
      `<div class=card><div class=k>${x[0]}</div><div class=\"v ${x[2]}\">${x[1]}</div></div>`
    ).join('');
  }

  const showVal = !d.hide_amounts || false;
  document.getElementById('rows').innerHTML = d.positions.map((p,i)=>{
    const reasons = (p.reasons||[]).slice(0,3).join(' · ');
    const valCell = (!d.hide_amounts)
      ? `<td class=val>${inr(p.current_value)}</td>`
      : (d.has_secret ? `<td class=\"val\" data-i=\"${i}\" style=\"display:none\">🔒</td>` : '');
    return `<tr>
      <td><div>${p.name}</div><div class=broker>${p.broker}</div>
          <div class=reasons>${reasons}</div></td>
      <td><span class=\"pill ${p.signal}\">${p.signal}</span></td>
      <td>${p.rsi===null||p.rsi===undefined?'—':p.rsi}</td>
      <td>${pct(p.allocation_pct)}</td>
      <td class=\"${cls(p.pnl_pct)}\">${pct(p.pnl_pct)}</td>
      ${valCell}
    </tr>`;
  }).join('');
  if(!d.hide_amounts){ document.getElementById('valhdr').style.display=''; }

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
        document.getElementById('valhdr').style.display='';
        document.querySelectorAll('td.val[data-i]').forEach(td=>{
          const i = td.getAttribute('data-i');
          const a = sec.by_index[i] || {};
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
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    write()
