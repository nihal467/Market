"""Groww connectivity diagnostic — safe to run in CI logs (masks secrets).

Prints a step-by-step trace of the Groww real-time path so we can see EXACTLY
where it fails (config present? token mint status? LTP status? response shape?)
without ever leaking the API key, TOTP secret or access token.

Run:  python src/groww_diag.py
"""
from __future__ import annotations

import os

import requests

GROWW_BASE = "https://api.groww.in/v1"


def _mask(v: str | None) -> str:
    if not v:
        return "(empty)"
    v = v.strip()
    if len(v) <= 6:
        return f"set(len={len(v)})"
    return f"{v[:3]}…{v[-2:]} (len={len(v)})"


def _headers(bearer: str) -> dict:
    import uuid
    return {
        "x-request-id": str(uuid.uuid4()),
        "Authorization": "Bearer " + bearer,
        "Content-Type": "application/json",
        "x-api-version": "1.0",
        "x-client-id": "market-bot",
        "x-client-platform": "market-bot",
    }


def main() -> None:
    print("=== GROWW DIAGNOSTIC ===")
    api_key = os.environ.get("GROWW_API_KEY")
    secret = os.environ.get("GROWW_API_SECRET")
    token_env = os.environ.get("GROWW_ACCESS_TOKEN")
    print(f"GROWW_API_KEY      : {_mask(api_key)}")
    print(f"GROWW_API_SECRET   : {_mask(secret)}")
    print(f"GROWW_ACCESS_TOKEN : {_mask(token_env)}")

    token = token_env.strip() if token_env else None

    if not token:
        if not (api_key and secret):
            print("RESULT: missing API key or TOTP secret — cannot mint token.")
            return
        try:
            import pyotp
        except ImportError:
            print("RESULT: pyotp not installed.")
            return

        # Generate the 6-digit TOTP. A common failure is a secret that is not a
        # valid base32 string (Groww gives the base32 SEED, not the 6-digit code).
        try:
            code = pyotp.TOTP(secret.strip()).now()
            print(f"TOTP generated     : {code[:2]}**** (ok)")
        except Exception as exc:  # noqa: BLE001
            print(f"RESULT: TOTP generation failed — is GROWW_API_SECRET a base32 seed? ({exc})")
            return

        print("\n-- Step 1: mint access token (POST /token/api/access) --")
        try:
            resp = requests.post(
                f"{GROWW_BASE}/token/api/access",
                headers=_headers(api_key.strip()),
                json={"key_type": "totp", "totp": code},
                timeout=20,
            )
            print(f"HTTP status        : {resp.status_code}")
            body_txt = resp.text[:400]
            print(f"Body (first 400)   : {body_txt}")
            resp.raise_for_status()
            data = resp.json()
            token = data.get("token") or data.get("payload", {}).get("token")
            print(f"Parsed token       : {_mask(token)}")
            if not token:
                print(f"Top-level keys     : {list(data.keys())}")
                print("RESULT: token missing in response — response shape differs.")
                return
        except Exception as exc:  # noqa: BLE001
            print(f"RESULT: token mint failed ({exc})")
            return
    else:
        print("\n-- Using GROWW_ACCESS_TOKEN directly (skipping mint) --")

    print("\n-- Step 2: live LTP (GET /live-data/ltp) --")
    symbols = "NSE_RELIANCE,NSE_TCS,NSE_INFY"
    try:
        resp = requests.get(
            f"{GROWW_BASE}/live-data/ltp",
            headers=_headers(token),
            params={"segment": "CASH", "exchange_symbols": symbols},
            timeout=20,
        )
        print(f"HTTP status        : {resp.status_code}")
        print(f"Body (first 500)   : {resp.text[:500]}")
        resp.raise_for_status()
        body = resp.json()
        payload = body.get("payload", body) if isinstance(body, dict) else body
        print(f"Payload keys       : {list(payload.keys()) if isinstance(payload, dict) else type(payload)}")
        print("RESULT: ✓ Groww live-data reachable. If keys/prices look right, the bot will use real-time data.")
    except Exception as exc:  # noqa: BLE001
        print(f"RESULT: LTP call failed ({exc})")


if __name__ == "__main__":
    main()
