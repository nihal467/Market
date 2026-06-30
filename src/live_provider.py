"""Live price provider — Groww real-time LTP with a yfinance fallback.

WHY THIS EXISTS
yfinance (Yahoo Finance) is free but its NSE quotes are delayed ~15 minutes.
Groww's Trade API streams *real-time* last-traded prices for free to any Groww
account holder. This module fetches live LTPs from Groww when credentials are
configured, and transparently falls back to yfinance (current behaviour) when
they are not — so the bot keeps working with or without Groww.

ACTIVATION (one-time, done by the account owner — credentials are personal and
cannot be generated here):
  1. In Groww: Profile -> Settings -> Trading APIs -> generate a *TOTP* API key.
     You receive an API key and a TOTP secret (a base32 string).
  2. Add two GitHub repo secrets (Settings -> Secrets and variables -> Actions):
       GROWW_API_KEY     = the API key
       GROWW_API_SECRET  = the TOTP secret (base32)
  3. The workflow passes them as env vars; this module then mints a fresh daily
     access token on each run and pulls live LTPs. No token is ever committed.

If only a pre-minted token is available you can instead set GROWW_ACCESS_TOKEN
directly (it expires daily, so the TOTP-secret route above is preferred for
unattended runs).

The Groww REST surface used here (confirmed against the official growwapi SDK):
  POST https://api.groww.in/v1/token/api/access     -> {"token": "..."}
  GET  https://api.groww.in/v1/live-data/ltp         -> {"payload": {SYM: ltp}}
Headers: Authorization: Bearer <key|token>, x-api-version: 1.0
"""
from __future__ import annotations

import os
import uuid

import requests

GROWW_BASE = "https://api.groww.in/v1"
SEGMENT_CASH = "CASH"
_LTP_BATCH = 50  # keep request URLs and API load reasonable


def groww_configured() -> bool:
    """True if enough credentials exist to attempt a Groww fetch."""
    return bool(os.environ.get("GROWW_ACCESS_TOKEN") or os.environ.get("GROWW_API_KEY"))


def _headers(bearer: str) -> dict:
    return {
        "x-request-id": str(uuid.uuid4()),
        "Authorization": "Bearer " + bearer,
        "Content-Type": "application/json",
        "x-api-version": "1.0",
        "x-client-id": "market-bot",
        "x-client-platform": "market-bot",
    }


def _access_token() -> str | None:
    """Return a usable access token, minting one from the TOTP secret if needed."""
    token = os.environ.get("GROWW_ACCESS_TOKEN")
    if token:
        return token.strip()

    api_key = os.environ.get("GROWW_API_KEY")
    secret = os.environ.get("GROWW_API_SECRET")
    if not (api_key and secret):
        return None

    # Mint a fresh daily token via the TOTP flow.
    try:
        import pyotp
    except ImportError:
        print("  ! pyotp not installed — cannot mint Groww token; using fallback.")
        return None

    try:
        totp = pyotp.TOTP(secret.strip()).now()
        resp = requests.post(
            f"{GROWW_BASE}/token/api/access",
            headers=_headers(api_key.strip()),
            json={"key_type": "totp", "totp": totp},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # SDK returns response.json()["token"]; be defensive about shape.
        return data.get("token") or data.get("payload", {}).get("token")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! Groww token mint failed ({exc}); using fallback.")
        return None


def _to_groww_symbol(yf_symbol: str) -> str | None:
    """Map a yfinance NSE symbol (RELIANCE.NS) to Groww's NSE_RELIANCE form."""
    if yf_symbol.endswith(".NS"):
        return "NSE_" + yf_symbol[:-3]
    if yf_symbol.endswith(".BO"):
        return "BSE_" + yf_symbol[:-3]
    return None  # indices / unknown formats -> let yfinance handle them


def _extract_ltp(value) -> float | None:
    """LTP payload values may be a bare number or a small dict; handle both."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for k in ("ltp", "last_price", "lastPrice", "price", "value"):
            if value.get(k) is not None:
                return float(value[k])
    return None


def live_ltps(yf_symbols: list[str]) -> dict[str, float]:
    """Real-time last-traded prices keyed by the ORIGINAL yfinance symbol.

    Returns an empty dict if Groww is not configured or the call fails, so the
    caller can keep its yfinance prices unchanged.
    """
    if not groww_configured():
        return {}

    token = _access_token()
    if not token:
        return {}

    # Build Groww symbols and reverse maps back to yfinance symbols. We keep
    # several lookup keys (full "NSE_RELIANCE", bare "RELIANCE") because the LTP
    # response may key results slightly differently from the request.
    g_to_yf: dict[str, str] = {}
    bare_to_yf: dict[str, str] = {}
    for s in yf_symbols:
        g = _to_groww_symbol(s)
        if g:
            g_to_yf[g] = s
            bare_to_yf[g.split("_", 1)[-1]] = s
    if not g_to_yf:
        return {}

    def _resolve(key: str) -> str | None:
        """Map a response key back to the original yfinance symbol, robustly."""
        if key in g_to_yf:
            return g_to_yf[key]
        norm = key.replace("-", "_").upper()
        if norm in g_to_yf:
            return g_to_yf[norm]
        bare = norm.split("_", 1)[-1]
        return bare_to_yf.get(bare)

    out: dict[str, float] = {}
    g_symbols = list(g_to_yf.keys())
    for i in range(0, len(g_symbols), _LTP_BATCH):
        chunk = g_symbols[i:i + _LTP_BATCH]
        try:
            resp = requests.get(
                f"{GROWW_BASE}/live-data/ltp",
                headers=_headers(token),
                params={"segment": SEGMENT_CASH, "exchange_symbols": ",".join(chunk)},
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            payload = body.get("payload", body) if isinstance(body, dict) else {}
            for key, val in (payload or {}).items():
                ltp = _extract_ltp(val)
                yf_sym = _resolve(key)
                if ltp is not None and ltp > 0 and yf_sym:
                    out[yf_sym] = round(ltp, 2)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Groww LTP batch failed ({exc}); falling back for these.")
            continue

    if out:
        print(f"  ✓ Groww live LTP for {len(out)}/{len(yf_symbols)} symbols (real-time).")
    return out


if __name__ == "__main__":
    # Smoke test: prints whether Groww is wired and a couple of sample prices.
    print("Groww configured:", groww_configured())
    sample = ["RELIANCE.NS", "TCS.NS", "INFY.NS"]
    print(live_ltps(sample) or "(no Groww data — would use yfinance fallback)")
