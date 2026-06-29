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


def write() -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)
    data = build_data()
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
</style>
</head>
<body>
<div class=\"wrap\">
  <h1 id=\"title\">Investment Dashboard</h1>
  <div class=\"sub\" id=\"updated\">Loading…</div>
  <div class=\"cards\" id=\"cards\"></div>
  <table>
    <thead><tr>
      <th>Holding</th><th>Signal</th><th>RSI</th><th>Alloc %</th><th>Return %</th>
    </tr></thead>
    <tbody id=\"rows\"></tbody>
  </table>
  <div class=\"foot\" id=\"foot\"></div>
</div>
<script>
function pct(v){ return (v===null||v===undefined) ? '—' : v.toFixed(2)+'%'; }
function cls(v){ return (v>0)?'pos':(v<0)?'neg':''; }
const d = /*__DATA__*/null;
(function(){
  if(!d){ document.getElementById('updated').textContent='No data'; return; }
  document.getElementById('title').textContent = d.title;
  document.title = d.title;
  document.getElementById('updated').textContent =
    'Updated ' + new Date(d.generated_at).toLocaleString();
  const c = d.signal_counts || {};
  const cards = [
    ['Buy', c.BUY||0, 'BUY'], ['Hold', c.HOLD||0, 'HOLD'], ['Sell', c.SELL||0, 'SELL'],
    ['Total return', pct(d.totals && d.totals.pnl_pct), '']
  ];
  if(!d.hide_amounts && d.totals){
    cards.push(['Current value', '₹'+(d.totals.current_value||0).toLocaleString('en-IN'), '']);
  }
  document.getElementById('cards').innerHTML = cards.map(x=>
    `<div class=card><div class=k>${x[0]}</div><div class=\"v ${x[2]}\">${x[1]}</div></div>`
  ).join('');
  document.getElementById('rows').innerHTML = d.positions.map(p=>{
    const reasons = (p.reasons||[]).slice(0,3).join(' · ');
    return `<tr>
      <td><div>${p.name}</div><div class=broker>${p.broker}</div>
          <div class=reasons>${reasons}</div></td>
      <td><span class=\"pill ${p.signal}\">${p.signal}</span></td>
      <td>${p.rsi===null||p.rsi===undefined?'—':p.rsi}</td>
      <td>${pct(p.allocation_pct)}</td>
      <td class=\"${cls(p.pnl_pct)}\">${pct(p.pnl_pct)}</td>
    </tr>`;
  }).join('');
  document.getElementById('foot').textContent = d.disclaimer;
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    write()
