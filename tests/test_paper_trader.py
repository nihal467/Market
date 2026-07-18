from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_trader  # noqa: E402
from paper_trader import _normalize_history  # noqa: E402
from strategy_config import COST_BPS_PER_SIDE, SLIPPAGE_BPS  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

SLIP = SLIPPAGE_BPS / 10000.0
COST_RATE = COST_BPS_PER_SIDE / 10000.0


class PaperTraderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data_dir = os.environ.get("MARKET_DATA_DIR")
        os.environ["MARKET_DATA_DIR"] = str(self.root)
        self.old_benchmark_level = paper_trader._benchmark_level
        paper_trader._benchmark_level = lambda: None
        # No network in tests: opens are stubbed per-test via self.set_opens.
        self.old_fetch_opens = paper_trader._fetch_opens
        paper_trader._fetch_opens = lambda symbols, trading_date: {}

    def tearDown(self) -> None:
        paper_trader._benchmark_level = self.old_benchmark_level
        paper_trader._fetch_opens = self.old_fetch_opens
        if self.old_data_dir is None:
            os.environ.pop("MARKET_DATA_DIR", None)
        else:
            os.environ["MARKET_DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def set_opens(self, opens: dict) -> None:
        paper_trader._fetch_opens = lambda symbols, trading_date: dict(opens)

    def freeze_now(self, dt: datetime) -> None:
        old_now = paper_trader.now_ist
        paper_trader.now_ist = lambda: dt
        self.addCleanup(lambda: setattr(paper_trader, "now_ist", old_now))

    def write_json(self, rel: str, payload: dict) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_normalize_history_sorts_and_dedupes_by_date(self) -> None:
        rows = [
            {"date": "2026-07-02", "value": 502},
            {"date": "2026-07-01", "value": 501},
            {"date": "2026-07-02", "value": 503},
            {"value": 999},
        ]

        normalized = _normalize_history(rows)

        self.assertEqual(
            normalized,
            [
                {"date": "2026-07-01", "value": 501},
                {"date": "2026-07-02", "value": 503},
            ],
        )

    def test_normalize_history_drops_rows_before_inception(self) -> None:
        rows = [
            {"date": "2026-06-29", "value": 499},
            {"date": "2026-06-30", "value": 500},
            {"date": "2026-07-01", "value": 501},
        ]

        normalized = _normalize_history(rows, inception="2026-06-30")

        self.assertEqual(
            normalized,
            [
                {"date": "2026-06-30", "value": 500},
                {"date": "2026-07-01", "value": 501},
            ],
        )

    def test_stale_daily_analysis_does_not_rewind_paper_state(self) -> None:
        self.write_json("daily/latest.json", {
            "trading_date": "2026-06-30",
            "analysis": [{"symbol": "AAA.NS", "name": "AAA", "price": 100.0}],
        })
        self.write_json("paper/state.json", {
            "inception": "2026-06-30",
            "start_capital": 500000.0,
            "cash": 100000.0,
            "positions": {"AAA.NS": {"qty": 10, "avg_price": 90.0, "name": "AAA"}},
            "last_date": "2026-07-01",
            "history": [
                {"date": "2026-06-30", "value": 499000.0, "total_pnl": -1000.0},
                {"date": "2026-07-01", "value": 501000.0, "total_pnl": 1000.0},
            ],
        })
        self.write_json("paper/latest.json", {
            "inception": "2026-06-30",
            "start_capital": 500000.0,
            "value": 499000.0,
            "cash": 100000.0,
            "total_pnl": -1000.0,
            "history": [],
        })

        result = paper_trader.run()

        self.assertEqual(result["reason"], "stale_trading_date")
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        latest = json.loads((self.root / "paper/latest.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_date"], "2026-07-01")
        self.assertEqual([row["date"] for row in state["history"]], ["2026-06-30", "2026-07-01"])
        self.assertEqual(latest["value"], 501000.0)
        self.assertEqual(latest["total_pnl"], 1000.0)

    def test_same_day_paper_trade_skips_before_market_close(self) -> None:
        old_now = paper_trader.now_ist
        paper_trader.now_ist = lambda: datetime(2026, 7, 2, 14, 56, tzinfo=IST)
        self.addCleanup(lambda: setattr(paper_trader, "now_ist", old_now))
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "analysis": [{"symbol": "AAA.NS", "name": "AAA", "price": 120.0}],
        })
        self.write_json("paper/state.json", {
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "cash": 499000.0,
            "positions": {"AAA.NS": {"qty": 10, "avg_price": 90.0, "name": "AAA"}},
            "last_date": "2026-07-01",
            "history": [{"date": "2026-07-01", "value": 500000.0, "total_pnl": 0.0}],
        })

        result = paper_trader.run()

        self.assertEqual(result["reason"], "market_not_closed")
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_date"], "2026-07-01")
        self.assertEqual([row["date"] for row in state["history"]], ["2026-07-01"])

    def test_post_close_run_replaces_preliminary_same_day_snapshot(self) -> None:
        old_now = paper_trader.now_ist
        paper_trader.now_ist = lambda: datetime(2026, 7, 2, 16, 40, tzinfo=IST)
        self.addCleanup(lambda: setattr(paper_trader, "now_ist", old_now))
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "analysis": [{
                "symbol": "AAA.NS",
                "name": "AAA",
                "price": 120.0,
                "signal": "BUY",
                "score": 4.0,
                "ret_3m": 10.0,
            }],
            "market_regime": {"risk_on": False, "reason": "test"},
        })
        self.write_json("paper/state.json", {
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "cash": 499000.0,
            "positions": {"AAA.NS": {"qty": 10, "avg_price": 90.0, "name": "AAA"}},
            "last_date": "2026-07-02",
            "last_rebalance_key": "2026-W27",
            "history": [
                {"date": "2026-07-01", "value": 500000.0, "total_pnl": 0.0},
                {"date": "2026-07-02", "value": 500100.0, "day_pnl": 100.0, "total_pnl": 100.0},
            ],
        })
        self.write_json("paper/latest.json", {
            "ist": "2026-07-02T14:56:00+05:30",
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "value": 500100.0,
            "history": [{"date": "2026-07-02", "value": 500100.0}],
        })

        result = paper_trader.run()

        self.assertFalse(result.get("skipped", False))
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        latest = json.loads((self.root / "paper/latest.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_date"], "2026-07-02")
        self.assertEqual([row["date"] for row in state["history"]], ["2026-07-01", "2026-07-02"])
        self.assertNotEqual(state["history"][-1]["value"], 500100.0)
        self.assertTrue(state["history"][-1]["final"])
        self.assertEqual(latest["value"], state["history"][-1]["value"])
        self.assertEqual(latest["day_pnl"], state["history"][-1]["day_pnl"])

    def test_pending_order_fills_at_next_open_with_slippage_and_costs(self) -> None:
        self.freeze_now(datetime(2026, 7, 2, 16, 40, tzinfo=IST))
        self.set_opens({"AAA.NS": 100.0, "CCC.NS": 95.0})
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "market_regime": {"risk_on": True, "reason": "test"},
            "analysis": [
                {"symbol": "AAA.NS", "name": "AAA", "price": 101.0,
                 "signal": "HOLD", "score": 0.0},
                {"symbol": "CCC.NS", "name": "CCC", "price": 96.0,
                 "signal": "HOLD", "score": 0.0},
            ],
        })
        self.write_json("paper/state.json", {
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "cash": 400000.0,
            "positions": {"CCC.NS": {"qty": 1000, "avg_price": 90.0,
                                     "peak_price": 95.0, "name": "CCC"}},
            "last_date": "2026-07-01",
            "last_rebalance_key": "2026-W27",
            "history": [{"date": "2026-07-01", "value": 500000.0, "total_pnl": 0.0}],
            "pending_orders": [
                {"action": "BUY", "symbol": "AAA.NS", "name": "AAA",
                 "budget": 100000.0, "reason": "weekly_rebalance",
                 "queued_date": "2026-07-01"},
                {"action": "SELL", "symbol": "CCC.NS", "name": "CCC", "qty": 1000,
                 "reason": "exit_signal", "queued_date": "2026-07-01"},
            ],
        })

        result = paper_trader.run()

        self.assertFalse(result.get("skipped", False))
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))

        # Reconcile cash to the paisa: the sell fills below the open, the buy
        # above it, and both legs pay per-side costs on notional.
        sell_fill = 95.0 * (1 - SLIP)
        sell_proceeds = 1000 * sell_fill
        sell_cost = sell_proceeds * COST_RATE
        buy_fill = 100.0 * (1 + SLIP)
        denom = buy_fill * (1 + COST_RATE)
        buy_qty = int(100000.0 / denom)
        spend = buy_qty * buy_fill
        buy_cost = spend * COST_RATE
        expected_cash = 400000.0 + sell_proceeds - sell_cost - spend - buy_cost
        self.assertAlmostEqual(state["cash"], expected_cash, places=2)

        self.assertNotIn("CCC.NS", state["positions"])
        self.assertEqual(state["positions"]["AAA.NS"]["qty"], buy_qty)
        self.assertAlmostEqual(state["positions"]["AAA.NS"]["avg_price"],
                               round(buy_fill, 2), places=2)
        self.assertEqual(state["pending_orders"], [])
        self.assertAlmostEqual(state["total_costs"],
                               round(sell_cost + buy_cost, 2), places=2)

        snap = state["history"][-1]
        self.assertEqual(snap["execution_model"], "next_open")
        fills = {t["symbol"]: t for t in snap["trades"]}
        self.assertEqual(set(fills), {"AAA.NS", "CCC.NS"})
        self.assertGreater(fills["AAA.NS"]["price"], 100.0)   # buy pays up
        self.assertLess(fills["CCC.NS"]["price"], 95.0)       # sell gives up
        for trade in fills.values():
            for key in ("action", "symbol", "qty", "price", "cost", "reason"):
                self.assertIn(key, trade)

        # A second run on the same date is idempotent and must not refill.
        again = paper_trader.run()
        self.assertEqual(again["reason"], "already_done")
        state2 = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(state2["cash"], expected_cash, places=2)

    def test_two_day_flow_queues_on_t_and_fills_at_t_plus_one_open(self) -> None:
        # Day T: fresh book, strong candidate, rebalance due -> the trader must
        # QUEUE the buy (budget = 15% position cap), not execute at T's close.
        self.freeze_now(datetime(2026, 7, 1, 16, 40, tzinfo=IST))
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-01",
            "market_regime": {"risk_on": True, "reason": "test"},
            "analysis": [
                {"symbol": "AAA.NS", "name": "AAA", "price": 100.0,
                 "signal": "BUY", "score": 5.0, "ret_3m": 25.0},
            ],
        })

        day1 = paper_trader.run()

        self.assertFalse(day1.get("skipped", False))
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["positions"], {})           # nothing filled on T
        self.assertEqual(state["cash"], 500000.0)
        self.assertEqual(len(state["pending_orders"]), 1)
        order = state["pending_orders"][0]
        self.assertEqual(order["action"], "BUY")
        self.assertEqual(order["symbol"], "AAA.NS")
        self.assertAlmostEqual(order["budget"], 75000.0, places=2)  # 15% cap
        self.assertEqual(order["queued_date"], "2026-07-01")

        # Day T+1: the queued order fills at T+1's OPEN (102), not T's close.
        self.freeze_now(datetime(2026, 7, 2, 16, 40, tzinfo=IST))
        self.set_opens({"AAA.NS": 102.0})
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "market_regime": {"risk_on": True, "reason": "test"},
            "analysis": [
                {"symbol": "AAA.NS", "name": "AAA", "price": 103.0,
                 "signal": "HOLD", "score": 0.0},
            ],
        })

        day2 = paper_trader.run()

        self.assertFalse(day2.get("skipped", False))
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        buy_fill = 102.0 * (1 + SLIP)
        denom = buy_fill * (1 + COST_RATE)
        qty = int(75000.0 / denom)
        spend = qty * buy_fill
        cost = spend * COST_RATE
        self.assertEqual(state["positions"]["AAA.NS"]["qty"], qty)
        self.assertAlmostEqual(state["cash"], 500000.0 - spend - cost, places=2)
        snap = state["history"][-1]
        self.assertEqual(snap["trades"][0]["queued_date"], "2026-07-01")
        self.assertAlmostEqual(snap["trades"][0]["price"], round(buy_fill, 2), places=2)
        self.assertEqual(state["pending_orders"], [])

    def test_partial_daily_data_executes_pending_but_queues_nothing(self) -> None:
        self.freeze_now(datetime(2026, 7, 2, 16, 40, tzinfo=IST))
        self.set_opens({"AAA.NS": 100.0})
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "partial": True,
            "coverage_pct": 62.0,
            "market_regime": {"risk_on": True, "reason": "test"},
            "analysis": [
                {"symbol": "AAA.NS", "name": "AAA", "price": 101.0,
                 "signal": "BUY", "score": 5.0, "ret_3m": 25.0},
            ],
        })
        self.write_json("paper/state.json", {
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "cash": 500000.0,
            "positions": {},
            "last_date": "2026-07-01",
            "last_rebalance_key": "2026-W20",
            "history": [{"date": "2026-07-01", "value": 500000.0, "total_pnl": 0.0}],
            "pending_orders": [
                {"action": "BUY", "symbol": "AAA.NS", "name": "AAA",
                 "budget": 50000.0, "reason": "weekly_rebalance",
                 "queued_date": "2026-07-01"},
            ],
        })

        result = paper_trader.run()

        self.assertFalse(result.get("skipped", False))
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        # The already-pending order was still filled ...
        self.assertIn("AAA.NS", state["positions"])
        # ... but NOTHING new was queued despite a strong BUY candidate and a
        # rebalance being due, and the reason is recorded in the day's row.
        self.assertEqual(state["pending_orders"], [])
        snap = state["history"][-1]
        self.assertEqual(snap["queue_skipped"], "partial_daily_data")
        # The rebalance key stays unconsumed so the next full day retries.
        self.assertEqual(state["last_rebalance_key"], "2026-W20")

    def test_circuit_breaker_blocks_new_buys_but_allows_stop_sells(self) -> None:
        self.freeze_now(datetime(2026, 7, 2, 16, 40, tzinfo=IST))
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "market_regime": {"risk_on": True, "reason": "test"},
            "analysis": [
                {"symbol": "BBB.NS", "name": "BBB", "price": 80.0,
                 "signal": "SELL", "score": -3.0, "ret_3m": -25.0},
                {"symbol": "AAA.NS", "name": "AAA", "price": 50.0,
                 "signal": "BUY", "score": 5.0, "ret_3m": 30.0},
            ],
        })
        # Legacy state without pending_orders/peak_value/total_costs keys —
        # also exercises the graceful migration path.
        self.write_json("paper/state.json", {
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "cash": 400000.0,
            "positions": {"BBB.NS": {"qty": 100, "avg_price": 100.0,
                                     "peak_price": 100.0, "name": "BBB"}},
            "last_date": "2026-07-01",
            "last_rebalance_key": "2026-W20",
            "history": [{"date": "2026-07-01", "value": 500000.0, "total_pnl": 0.0}],
        })

        result = paper_trader.run()

        self.assertFalse(result.get("skipped", False))
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        snap = state["history"][-1]
        # Book: 400000 cash + 100 x 80 = 408000 -> 18.4% below the 500000 peak.
        self.assertTrue(snap["circuit_breaker"])
        self.assertEqual(state["peak_value"], 500000.0)
        actions = [(o["action"], o["symbol"]) for o in state["pending_orders"]]
        self.assertIn(("SELL", "BBB.NS"), actions)          # protective exit queued
        self.assertFalse(any(a == "BUY" for a, _s in actions))  # no new buys

    def test_same_day_replay_never_fills_orders_queued_today(self) -> None:
        # Replaying today's snapshot (legacy preliminary row) must NOT fill
        # orders that were queued from TODAY's close — no order may fill on
        # the bar that produced it. They stay queued for the next session.
        self.freeze_now(datetime(2026, 7, 2, 16, 40, tzinfo=IST))
        self.set_opens({"AAA.NS": 100.0})
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "market_regime": {"risk_on": True, "reason": "test"},
            "analysis": [
                {"symbol": "AAA.NS", "name": "AAA", "price": 101.0,
                 "signal": "HOLD", "score": 0.0},
            ],
        })
        self.write_json("paper/state.json", {
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "cash": 500000.0,
            "positions": {},
            "last_date": "2026-07-02",
            "last_rebalance_key": "2026-W27",
            "history": [
                {"date": "2026-07-01", "value": 500000.0, "total_pnl": 0.0},
                {"date": "2026-07-02", "value": 500050.0, "total_pnl": 50.0},
            ],
            "pending_orders": [
                {"action": "BUY", "symbol": "AAA.NS", "name": "AAA",
                 "budget": 75000.0, "reason": "weekly_rebalance",
                 "queued_date": "2026-07-02"},
            ],
        })
        # Preliminary latest snapshot (pre-close ist) forces the replay path.
        self.write_json("paper/latest.json", {
            "ist": "2026-07-02T14:56:00+05:30",
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "value": 500050.0,
            "history": [{"date": "2026-07-02", "value": 500050.0}],
        })

        result = paper_trader.run()

        self.assertFalse(result.get("skipped", False))
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["cash"], 500000.0)          # nothing filled
        self.assertEqual(state["positions"], {})
        self.assertEqual(state["history"][-1]["trades"], [])
        # The same-day order is still queued for the next session.
        self.assertEqual(len(state["pending_orders"]), 1)
        self.assertEqual(state["pending_orders"][0]["symbol"], "AAA.NS")
        self.assertEqual(state["pending_orders"][0]["queued_date"], "2026-07-02")

    def test_unpriceable_pending_order_is_dropped_with_note(self) -> None:
        self.freeze_now(datetime(2026, 7, 2, 16, 40, tzinfo=IST))
        self.set_opens({})  # no bar for DDD.NS today
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "market_regime": {"risk_on": True, "reason": "test"},
            "analysis": [
                {"symbol": "AAA.NS", "name": "AAA", "price": 101.0,
                 "signal": "HOLD", "score": 0.0},
            ],
        })
        self.write_json("paper/state.json", {
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "cash": 500000.0,
            "positions": {},
            "last_date": "2026-07-01",
            "last_rebalance_key": "2026-W27",
            "history": [{"date": "2026-07-01", "value": 500000.0, "total_pnl": 0.0}],
            "pending_orders": [
                {"action": "BUY", "symbol": "DDD.NS", "name": "DDD",
                 "budget": 50000.0, "reason": "weekly_rebalance",
                 "queued_date": "2026-07-01"},
            ],
        })

        result = paper_trader.run()

        self.assertFalse(result.get("skipped", False))
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["cash"], 500000.0)
        self.assertEqual(state["positions"], {})
        snap = state["history"][-1]
        self.assertEqual(snap["trades"], [])
        self.assertEqual(snap["dropped_orders"][0]["symbol"], "DDD.NS")
        self.assertEqual(snap["dropped_orders"][0]["drop_reason"], "no_price_data_today")


if __name__ == "__main__":
    unittest.main()
