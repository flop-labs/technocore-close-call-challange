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
        result = fold.sweep(1, "100.00", [A, B], [trade("in", A, "sell", "1", "101.00", B),
                                                  trade("out", A, "sell", "1", "101.01", B)])
        self.assertEqual([self.outcome(result, i) for i in range(2)], ["settled", "limits"])

    def test_symmetric_collateral_and_exact_zero_sum(self):
        fold = self.fold()
        fold.sweep(1, "100.00", [A, B], [trade("t", A, "sell", "10", "100.00", B)])
        self.assertEqual(fold.accounts[A].cash, Decimal("8990"))   # 1,000 collateral plus a 10 fee
        self.assertEqual(fold.accounts[B].cash, Decimal("8990"))
        final = fold.final("150.00")
        scores = {r["key"]: Decimal(r["score"]) for r in final["standings"]}
        self.assertEqual(scores[A], Decimal("-510"))     # short 10 from 100 to 150, minus the fee
        self.assertEqual(scores[B], Decimal("490"))
        self.assertEqual(Decimal(final["zero_sum"]), 0)

    def test_short_can_end_below_zero(self):
        fold = self.fold()
        fold.sweep(1, "100.00", [A, B], [trade("t", A, "sell", "99", "100.00", B)])
        final = fold.final("250.00")
        self.assertLess(Decimal(final["standings"][-1]["score"]), Decimal("-10000"))

    def test_funds_count_trades_already_applied_this_sweep(self):
        fold = self.fold()
        result = fold.sweep(1, "100.00", [A, B, C], [trade("first", A, "sell", "90", "100.00", B),
                                                     trade("second", A, "sell", "10", "100.00", C)])
        self.assertEqual([self.outcome(result, i) for i in range(2)], ["settled", "funds"])

    def test_id_once_expiry_taker_and_lock(self):
        fold = Fold({"lock_sweep": 2})
        fold.seed("100.00")
        result = fold.sweep(1, "100.00", [A, B, C], [
            trade("x", A, "sell", "1", "100.00", B), trade("x", A, "sell", "1", "100.00", C),
            trade("named", A, "sell", "1", "100.00", C, taker=B), trade("old", A, "sell", "1", "100.00", B, until=0)])
        self.assertEqual([self.outcome(result, i) for i in range(4)], ["settled", "settled", "taker", "expired"])
        late = fold.sweep(3, "100.00", [], [trade("late", A, "sell", "1", "100.00", B)])
        self.assertEqual(self.outcome(late), "locked")

    def test_self_trade_pays_twice_and_moves_nothing(self):
        fold = self.fold()
        fold.sweep(1, "100.00", [A], [trade("s", A, "buy", "10", "100.00", A)])
        self.assertEqual(fold.accounts[A].cash, Decimal("9980"))
        self.assertEqual(fold.accounts[A].position, 0)

    def test_distance_fee_takes_back_a_discount(self):
        fold = self.fold(fee_rule="distance", limit_window="0.02")
        result = fold.sweep(1, "100.00", [A, B], [trade("gift", A, "sell", "10", "98.00", B)])
        self.assertEqual(result["trades"][0]["fee"], "20.00")   # distance 2.00 x 10, above 1% of 980
        final = fold.final("100.00")
        scores = {r["key"]: Decimal(r["score"]) for r in final["standings"]}
        self.assertEqual(scores[B], 0)                          # the discount is paid back as fee

    def test_ties_share_the_places_they_span(self):
        fold = self.fold()
        fold.sweep(1, "100.00", [A, B, C], [])
        rows = fold.final("100.00")["standings"]
        self.assertEqual({(tuple(r["places"]), r["sharing"]) for r in rows}, {((1, 2, 3), 3)})

    def test_malformed_inputs_are_refused(self):
        fold = self.fold()
        result = fold.sweep(1, "100.00", [A, B], [trade("bad", A, "sell", "1.005", "100.00", B),
                                                  trade("tiny", A, "sell", "0.09", "100.00", B),
                                                  "not a trade"])
        self.assertEqual([self.outcome(result, i) for i in range(3)], ["shape", "shape", "shape"])
        with self.assertRaises(ValueError):
            fold.sweep(1, "100.00", [], [])


if __name__ == "__main__":
    unittest.main()
