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

import incubation_report  # noqa: E402


IST = timezone(timedelta(hours=5, minutes=30))


def write_json(root: Path, rel: str, payload: dict) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class IncubationReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data_dir = os.environ.get("MARKET_DATA_DIR")
        os.environ["MARKET_DATA_DIR"] = str(self.root)

        fixed_now = datetime(2026, 7, 1, 18, 0, tzinfo=IST)
        self.old_now_ist = incubation_report.now_ist
        self.old_is_market_open = incubation_report.is_market_open
        incubation_report.now_ist = lambda: fixed_now
        incubation_report.is_market_open = lambda _dt=None: False

    def tearDown(self) -> None:
        incubation_report.now_ist = self.old_now_ist
        incubation_report.is_market_open = self.old_is_market_open
        if self.old_data_dir is None:
            os.environ.pop("MARKET_DATA_DIR", None)
        else:
            os.environ["MARKET_DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def seed_common_files(self, history: list[dict]) -> None:
        write_json(self.root, "paper/latest.json", {
            "ist": "2026-07-01T16:30:00+05:30",
            "inception": "2026-07-01",
            "value": 500000,
            "total_pnl_pct": 0.0,
            "alpha_pct": 0.0,
            "history": history,
        })
        write_json(self.root, "paper/live.json", {
            "ist": "2026-07-01T15:20:00+05:30",
            "market_open": False,
            "intraday_trades": [],
            "stops": [],
        })
        write_json(self.root, "daily/latest.json", {
            "trading_date": "2026-07-01",
            "market_regime": {"risk_on": True, "reason": "test"},
        })
        write_json(self.root, "watchlist/latest.json", {
            "generated_ist": "2026-07-01T08:00:00+05:30",
        })
        write_json(self.root, "backtest/latest.json", {
            "strategy_lab": {
                "variants": [
                    {
                        "id": "momentum_only_weekly",
                        "total_return_pct": 10.0,
                        "alpha_pct": 8.0,
                    }
                ]
            }
        })

    def churn_criterion(self) -> dict:
        report = incubation_report.run()
        return next(c for c in report["criteria"] if c["key"] == "churn_controlled")

    def criterion(self, key: str) -> dict:
        report = incubation_report.run()
        return next(c for c in report["criteria"] if c["key"] == key)

    def test_inception_buys_do_not_count_as_churn(self) -> None:
        self.seed_common_files([
            {
                "date": "2026-07-01",
                "value": 500000,
                "n_trades": 10,
            }
        ])

        criterion = self.churn_criterion()

        self.assertTrue(criterion["passed"])
        self.assertIn("0.00 average trades/day after inception", criterion["detail"])
        self.assertIn("0 trade days", criterion["detail"])

    def test_post_inception_trades_are_still_measured(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
            {"date": "2026-07-02", "value": 501000, "n_trades": 6},
            {"date": "2026-07-03", "value": 502000, "n_trades": 4},
        ])

        criterion = self.churn_criterion()

        self.assertFalse(criterion["passed"])
        self.assertIn("5.00 average trades/day after inception", criterion["detail"])
        self.assertIn("2 trade days", criterion["detail"])

    def test_history_is_sorted_before_churn_window(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-02", "value": 501000, "n_trades": 6},
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
            {"date": "2026-07-03", "value": 502000, "n_trades": 0},
        ])

        criterion = self.churn_criterion()

        self.assertTrue(criterion["passed"])
        self.assertIn("3.00 average trades/day after inception", criterion["detail"])
        self.assertIn("1 trade days", criterion["detail"])

    def test_pre_inception_rows_do_not_count_as_churn(self) -> None:
        self.seed_common_files([
            {"date": "2026-06-30", "value": 499000, "n_trades": 7},
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
            {"date": "2026-07-02", "value": 501000, "n_trades": 0},
        ])

        criterion = self.churn_criterion()

        self.assertTrue(criterion["passed"])
        self.assertIn("0.00 average trades/day after inception", criterion["detail"])
        self.assertIn("0 trade days", criterion["detail"])

    def test_legacy_payloads_fail_execution_and_oos_gates(self) -> None:
        # Payloads produced before the next-open upgrade carry neither an
        # execution_model field nor a train/validation split — the hardened
        # gate must fail closed on both.
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
        ])

        self.assertFalse(self.criterion("execution_model_next_open")["passed"])
        self.assertFalse(self.criterion("backtest_oos_positive")["passed"])

    def test_execution_and_oos_gates_pass_with_new_payloads(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
        ])
        write_json(self.root, "paper/latest.json", {
            "ist": "2026-07-01T16:30:00+05:30",
            "inception": "2026-07-01",
            "value": 500000,
            "total_pnl_pct": 0.0,
            "alpha_pct": 0.0,
            "execution_model": "next_open",
            "history": [{"date": "2026-07-01", "value": 500000, "n_trades": 10}],
        })
        write_json(self.root, "backtest/latest.json", {
            "execution_model": "next_open",
            "validation": {"validation_alpha_pct": 2.5},
            "strategy_lab": {
                "variants": [
                    {
                        "id": "momentum_only_weekly",
                        "total_return_pct": 10.0,
                        "alpha_pct": 8.0,
                    }
                ]
            },
        })

        self.assertTrue(self.criterion("execution_model_next_open")["passed"])
        oos = self.criterion("backtest_oos_positive")
        self.assertTrue(oos["passed"])
        self.assertIn("2.50%", oos["detail"])

    def test_recomputed_totals_flag_mismatch_against_stored(self) -> None:
        history = [
            {"date": "2026-07-01", "value": 500000, "benchmark_pct": 0.0},
            {"date": "2026-07-02", "value": 501000, "benchmark_pct": 0.1},
        ]
        # Seeded stored totals claim 0.0% but the value series implies +0.2%
        # total (+0.1% alpha) — a mismatch above 0.1 pct-pts must be flagged.
        self.seed_common_files(history)
        self.assertFalse(self.criterion("totals_recomputed_consistent")["passed"])

        # With stored totals that agree with the series, the check passes and
        # the P&L criteria run off the recomputed numbers.
        write_json(self.root, "paper/latest.json", {
            "ist": "2026-07-01T16:30:00+05:30",
            "inception": "2026-07-01",
            "value": 501000,
            "start_capital": 500000,
            "total_pnl_pct": 0.2,
            "alpha_pct": 0.1,
            "history": history,
        })
        self.assertTrue(self.criterion("totals_recomputed_consistent")["passed"])
        pnl = self.criterion("positive_dummy_pnl")
        self.assertTrue(pnl["passed"])
        self.assertIn("recomputed from history", pnl["detail"])

    def test_walk_forward_gate_passes_with_positive_mean_and_two_folds(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
        ])
        write_json(self.root, "backtest/latest.json", {
            "execution_model": "next_open",
            "validation": {
                "validation_alpha_pct": -1.0,   # ignored once walk-forward exists
                "walk_forward": {
                    "n_folds": 3,
                    "mean_validation_alpha_pct": 1.4,
                    "folds_positive": 2,
                },
            },
        })

        oos = self.criterion("backtest_oos_positive")

        self.assertTrue(oos["passed"])
        self.assertIn("walk-forward mean validation alpha 1.40%", oos["detail"])
        self.assertIn("2/3 folds positive", oos["detail"])

    def test_walk_forward_gate_fails_with_only_one_positive_fold(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
        ])
        write_json(self.root, "backtest/latest.json", {
            "validation": {
                "validation_alpha_pct": 5.0,    # would pass the old single split
                "walk_forward": {
                    "n_folds": 3,
                    "mean_validation_alpha_pct": 0.6,
                    "folds_positive": 1,
                },
            },
        })

        self.assertFalse(self.criterion("backtest_oos_positive")["passed"])

    def test_walk_forward_gate_fails_with_negative_mean_alpha(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
        ])
        write_json(self.root, "backtest/latest.json", {
            "validation": {
                "walk_forward": {
                    "n_folds": 3,
                    "mean_validation_alpha_pct": -0.2,
                    "folds_positive": 2,
                },
            },
        })

        self.assertFalse(self.criterion("backtest_oos_positive")["passed"])

    def test_drift_criterion_is_advisory_and_passes_without_report(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
        ])

        criterion = self.criterion("drift_ok")

        self.assertTrue(criterion["passed"])
        self.assertFalse(criterion["blocking"])
        self.assertIn("fresh start", criterion["detail"])

    def test_drift_criterion_passes_on_ok_and_insufficient_data(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
        ])
        for verdict in ("ok", "insufficient_data"):
            write_json(self.root, "drift/latest.json", {"verdict": verdict})
            criterion = self.criterion("drift_ok")
            self.assertTrue(criterion["passed"], verdict)
            self.assertIn(verdict, criterion["detail"])

    def test_drift_criterion_fails_on_drift_but_never_blocks(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
        ])
        write_json(self.root, "drift/latest.json", {
            "verdict": "drift",
            "final_divergence_pct": -4.2,
            "daily_return_correlation": 0.61,
        })

        criterion = self.criterion("drift_ok")

        self.assertFalse(criterion["passed"])
        self.assertFalse(criterion["blocking"])
        self.assertIn("'drift'", criterion["detail"])

    def test_backtest_gate_uses_active_profile_variant(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
            {"date": "2026-07-02", "value": 501000, "n_trades": 0},
        ])
        write_json(self.root, "backtest/latest.json", {
            "strategy_lab": {
                "variants": [
                    {
                        "id": "no_regime_filter",
                        "total_return_pct": 12.99,
                        "alpha_pct": 12.89,
                    },
                    {
                        "id": "momentum_only_weekly",
                        "total_return_pct": 8.8,
                        "alpha_pct": 8.7,
                    },
                    {
                        "id": "current_daily",
                        "total_return_pct": 10.4,
                        "alpha_pct": 10.1,
                    },
                    {
                        "id": "weekly_churn_control",
                        "total_return_pct": -0.67,
                        "alpha_pct": -0.77,
                    },
                ]
            }
        })

        criterion = self.criterion("backtest_supports_profile")

        self.assertTrue(criterion["passed"])
        self.assertIn("active variant momentum_only_weekly alpha 8.70%", criterion["detail"])


if __name__ == "__main__":
    unittest.main()
