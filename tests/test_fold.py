import json
import unittest
from decimal import Decimal
from pathlib import Path

from close_call_fold import Fold, replay

ROOT = Path(__file__).resolve().parents[1]
A = "did:key:z6Mk" + "A" * 44
B = "did:key:z6Mk" + "B" * 44
C = "did:key:z6Mk" + "C" * 44


def trade(tid, maker, side, qty, px, signer, taker="any", until=99):
    return {"id": tid, "maker": maker, "side": side, "qty": qty, "px": px,
            "taker": taker, "until": until, "countersigner": signer}


def contest_config():
    contest = json.loads((ROOT / "contest.json").read_text())
    return {k: contest[k] for k in ("mint", "min_qty", "limit_window", "fee_rate", "fee_rule", "lock_sweep", "prize_places") if k in contest}


class FoldTests(unittest.TestCase):
    def fold(self, **config):
        fold = Fold(config)
        fold.seed("100.00")
        return fold

    def outcome(self, result, i=0):
        entry = result["trades"][i]
        return entry.get("reason") or entry["outcome"]

    def test_sample_season_matches_expected_output(self):
        lines = (ROOT / "examples/sample-season.jsonl").read_text().splitlines()
        expected = json.loads((ROOT / "examples/sample-season.expected.json").read_text())
        self.assertEqual(replay(lines, contest_config()), expected)
        self.assertEqual(Decimal(expected["final"]["zero_sum"]), 0)

    def test_limits_are_inclusive_and_measured_from_the_reference(self):
        fold = self.fold()
        result = fold.sweep(1, "100.00", "100.00", [A, B], [trade("in", A, "sell", "1", "105.00", B),
                                                            trade("out", A, "sell", "1", "105.01", B)])
        self.assertEqual([self.outcome(result, i) for i in range(2)], ["settled", "limits"])

    def test_symmetric_collateral_and_exact_zero_sum(self):
        fold = self.fold()
        fold.sweep(1, "100.00", "100.00", [A, B], [trade("t", A, "sell", "10", "100.00", B)])
        self.assertEqual(fold.accounts[A].cash, Decimal("8990"))   # 1,000 collateral plus a 10 fee
        self.assertEqual(fold.accounts[B].cash, Decimal("8990"))
        final = fold.final("150.00")
        scores = {r["key"]: Decimal(r["score"]) for r in final["standings"]}
        self.assertEqual(scores[A], Decimal("-510"))     # short 10 from 100 to 150, minus the fee
        self.assertEqual(scores[B], Decimal("490"))
        self.assertEqual(Decimal(final["zero_sum"]), 0)

    def test_short_can_end_below_zero(self):
        fold = self.fold()
        fold.sweep(1, "100.00", "100.00", [A, B], [trade("t", A, "sell", "99", "100.00", B)])
        final = fold.final("250.00")
        self.assertLess(Decimal(final["standings"][-1]["score"]), Decimal("-10000"))

    def test_funds_count_trades_already_applied_this_sweep(self):
        fold = self.fold()
        result = fold.sweep(1, "100.00", "100.00", [A, B, C], [trade("first", A, "sell", "90", "100.00", B),
                                                     trade("second", A, "sell", "10", "100.00", C)])
        self.assertEqual([self.outcome(result, i) for i in range(2)], ["settled", "funds"])

    def test_id_once_expiry_taker_and_lock(self):
        fold = Fold({"lock_sweep": 2})
        fold.seed("100.00")
        result = fold.sweep(1, "100.00", "100.00", [A, B, C], [
            trade("x", A, "sell", "1", "100.00", B), trade("x", A, "sell", "1", "100.00", C),
            trade("named", A, "sell", "1", "100.00", C, taker=B), trade("old", A, "sell", "1", "100.00", B, until=0)])
        self.assertEqual([self.outcome(result, i) for i in range(4)], ["settled", "settled", "taker", "expired"])
        late = fold.sweep(3, "100.00", "100.00", [], [trade("late", A, "sell", "1", "100.00", B)])
        self.assertEqual(self.outcome(late), "locked")

    def test_self_trade_pays_twice_and_moves_nothing(self):
        fold = self.fold()
        fold.sweep(1, "100.00", "100.00", [A], [trade("s", A, "buy", "10", "100.00", A)])
        self.assertEqual(fold.accounts[A].cash, Decimal("9980"))
        self.assertEqual(fold.accounts[A].position, 0)

    def fees(self, result, i=0):
        entry = result["trades"][i]
        return entry["maker_fee"], entry["taker_fee"]

    def test_a_discount_handed_to_another_key_is_paid_back(self):
        fold = self.fold()
        result = fold.sweep(1, "100.00", "100.00", [A, B], [trade("gift", A, "sell", "10", "96.00", B)])
        self.assertEqual(self.fees(result), ("9.6000", "40.00"))  # 1% of 960; the buyer's 4.00 x 10 gap
        scores = {r["key"]: Decimal(r["score"]) for r in fold.final("100.00")["standings"]}
        self.assertEqual(scores[B], 0)                          # bought 40 under the close, paid 40 back
        self.assertEqual(scores[A], Decimal("-49.6"))

    def test_a_price_a_jump_left_behind_is_paid_back(self):
        fold = self.fold()                                      # Hyperliquid jumped to 106 inside the sweep
        result = fold.sweep(1, "100.00", "106.00", [A, B], [trade("stale", A, "sell", "10", "100.00", B)])
        self.assertEqual(self.fees(result), ("10.0000", "60.00"))
        self.assertEqual(result["close"], "106.00")
        scores = {r["key"]: Decimal(r["score"]) for r in fold.final("106.00")["standings"]}
        self.assertEqual(scores[B], 0)

    def test_a_trade_at_the_close_pays_one_percent_a_side(self):
        fold = self.fold()
        result = fold.sweep(1, "100.00", "104.00", [A, B], [trade("fair", A, "sell", "10", "104.00", B)])
        self.assertEqual(self.fees(result), ("10.4000", "10.4000"))

    def test_the_harvest_nets_nothing(self):
        fold = self.fold()                                      # A buys at the bottom and sells at the top
        fold.sweep(1, "100.00", "100.00", [A, B, C], [trade("low", B, "sell", "10", "95.00", A),
                                                      trade("high", C, "buy", "10", "105.00", A)])
        self.assertEqual(fold.accounts[A].cash, Decimal("10000"))
        self.assertEqual(fold.accounts[A].position, 0)

    def test_funds_cover_the_clawback_too(self):
        fold = self.fold()                                      # 102 x 95 plus 1% fits in 10,000; plus the 5.00 gap doesn't
        result = fold.sweep(1, "100.00", "100.00", [A, B], [trade("big", A, "sell", "102", "95.00", B)])
        self.assertEqual(self.outcome(result), "funds")

    def test_only_the_clawback_rule_is_configured(self):
        with self.assertRaises(ValueError):
            Fold({"fee_rule": "flat"})

    def test_ties_share_the_places_they_span(self):
        fold = self.fold()
        fold.sweep(1, "100.00", "100.00", [A, B, C], [])
        rows = fold.final("100.00")["standings"]
        self.assertEqual({(tuple(r["places"]), r["sharing"]) for r in rows}, {((1, 2, 3), 3)})

    def test_malformed_inputs_are_refused(self):
        fold = self.fold()
        result = fold.sweep(1, "100.00", "100.00", [A, B], [trade("bad", A, "sell", "1.005", "100.00", B),
                                                  trade("tiny", A, "sell", "0.09", "100.00", B),
                                                  "not a trade"])
        self.assertEqual([self.outcome(result, i) for i in range(3)], ["shape", "shape", "shape"])
        with self.assertRaises(ValueError):
            fold.sweep(1, "100.00", "100.00", [], [])
        with self.assertRaises(ValueError):
            fold.sweep(2, "100.00", None, [], [])               # no closing price


if __name__ == "__main__":
    unittest.main()
