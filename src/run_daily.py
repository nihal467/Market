"""Daily pipeline entrypoint: portfolio -> signals -> dashboard.

Run locally or from the GitHub Action:
    python src/run_daily.py
"""
from __future__ import annotations

import portfolio
import signals
import dashboard


def main() -> None:
    print(">>> [1/3] Portfolio valuation")
    snap = portfolio.build_snapshot(portfolio.load_holdings())
    portfolio.save_snapshot(snap)
    portfolio.print_summary(snap)

    print(">>> [2/3] Daily signals")
    signals.run()

    print(">>> [3/3] Dashboard")
    dashboard.write()

    print("\nDone.")


if __name__ == "__main__":
    main()
