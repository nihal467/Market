"""Fetch mutual fund NAVs from the AMFI daily NAV feed."""
from __future__ import annotations

import requests

AMFI_NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"


def get_mf_navs(scheme_codes: list[str]) -> dict[str, float | None]:
    """Return {scheme_code: nav}. NAV is None if the code was not found."""
    wanted = {str(c) for c in scheme_codes if str(c) not in ("", "0")}
    navs: dict[str, float | None] = {c: None for c in wanted}
    if not wanted:
        return navs

    try:
        resp = requests.get(AMFI_NAV_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! AMFI NAV fetch failed: {exc}")
        return navs

    # Each data line: Scheme Code;ISIN Div Payout;ISIN Div Reinvest;Scheme Name;NAV;Date
    for line in resp.text.splitlines():
        parts = line.split(";")
        if len(parts) < 6:
            continue
        code = parts[0].strip()
        if code in wanted:
            try:
                navs[code] = round(float(parts[4].strip()), 4)
            except ValueError:
                navs[code] = None
    return navs


if __name__ == "__main__":
    print(get_mf_navs(["120503"]))
