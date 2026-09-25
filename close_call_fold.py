"""Close Call fold: replay the referee's sweeps and get the same balances, trades and scores.

Input is JSON Lines, one event per line, in the order the referee applied them:

  {"t": "seed", "px": "180.00"}
  {"t": "sweep", "n": 1, "ref": "180.00", "close": "180.40", "owners": [did, ...], "trades": [trade, ...]}
  {"t": "final", "px": "189.00"}

`ref` is the reference posted at the previous sweep, which sets the limits. `close` is Hyperliquid's
last trade before this sweep's close, which the referee posts at this sweep: it prices the clawback.

A trade is the agreed terms plus the countersigning key, in the order the rooms stamped them:

  {"id": "a7f3", "maker": did, "side": "sell", "qty": "2", "px": "181.20",
   "taker": "any" | did, "until": 1236, "countersigner": did}

Signatures, nonces, rooms and stamps are checked before this: the fold receives only messages the
referee verified, and applies the rules to them. Amounts are exact decimals; nothing is rounded
until output.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from pathlib import Path

DID = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}")
TRADE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
TWO_PLACES = re.compile(r"[0-9]{1,7}(\.[0-9]{1,2})?")
CENT = Decimal("0.01")

DEFAULTS = {
    "mint": "10000",
    "min_qty": "0.1",
    "limit_window": "0.05",
    "fee_rate": "0.01",
    "fee_rule": "clawback",
    "lock_sweep": 2556,
    "prize_places": 3,
}


def amount(text) -> Decimal | None:
    """A price or quantity: a string with at most two decimals, above zero."""
    if not isinstance(text, str) or not TWO_PLACES.fullmatch(text):
        return None
    value = Decimal(text)
    return value if value > 0 else None


@dataclass
class Account:
    key: str
    cash: Decimal
    lots: list = field(default_factory=list)   # [qty, px] FIFO; all long (qty > 0) or all short (qty < 0)
    fees: Decimal = Decimal(0)

    @property
    def position(self) -> Decimal:
        return sum((q for q, _ in self.lots), Decimal(0))

    def opening(self, side: int, qty: Decimal) -> Decimal:
        """Contracts this trade opens rather than closes; side is +1 to buy, -1 to sell."""
        held = self.position
        closing = min(qty, max(-side * held, Decimal(0)))
        return qty - closing

    def apply(self, side: int, qty: Decimal, px: Decimal, fee: Decimal) -> None:
        self.cash -= fee
        self.fees += fee
        left = qty
        while left > 0 and self.lots and self.lots[0][0] * side < 0:
            lot_qty, lot_px = self.lots[0]
            size = min(left, abs(lot_qty))
            # a long lot sold returns the sale price; a short lot bought back returns its
            # collateral plus the price difference in its favour, or minus it
            self.cash += size * px if side < 0 else size * (2 * lot_px - px)
            left -= size
            if size == abs(lot_qty):
                self.lots.pop(0)
            else:
                self.lots[0][0] = lot_qty + side * size
        if left > 0:
            self.cash -= left * px            # every contract opened, long or short, ties up its price
            self.lots.append([side * left, px])

    def value_at(self, s: Decimal) -> Decimal:
        return self.cash + sum((q * s if q > 0 else -q * (2 * p - s) for q, p in self.lots), Decimal(0))


class Fold:
    def __init__(self, config: dict | None = None):
        cfg = {**DEFAULTS, **(config or {})}
        self.mint = Decimal(cfg["mint"])
        self.min_qty = Decimal(cfg["min_qty"])
        self.window = Decimal(cfg["limit_window"])
        self.fee_rate = Decimal(cfg["fee_rate"])
        if cfg["fee_rule"] != "clawback":
            raise ValueError("config: fee_rule must be 'clawback'")
        self.lock = int(cfg["lock_sweep"])
        self.places = int(cfg["prize_places"])
        self.accounts: dict[str, Account] = {}
        self.settled: set[str] = set()
        self.sweep_n = 0
        self.global_px: Decimal | None = None
        self.final_px: Decimal | None = None
        self.fees = Decimal(0)

    def side_fees(self, side: int, qty: Decimal, px: Decimal, close: Decimal) -> tuple[Decimal, Decimal]:
        """(maker's fee, taker's fee) for a maker on `side`. Each pays the fee rate on the trade's value;
        the one that got a better price than the sweep's close pays that gap back instead, if it is more."""
        base = self.fee_rate * qty * px
        gap = (close - px) * qty                  # above zero: the buyer paid less than the close
        buyer, seller = max(base, gap), max(base, -gap)
        return (buyer, seller) if side > 0 else (seller, buyer)

    def seed(self, px: str) -> None:
        value = amount(px)
        if value is None or self.global_px is not None:
            raise ValueError("seed: expected one opening price with at most two decimals")
        self.global_px = value

    def check(self, trade: dict, n: int, ref: Decimal, close: Decimal):
        """The void reason for one trade, or None if it settles."""
        if not isinstance(trade, dict):
            return "shape"
        maker, taker, signer = trade.get("maker"), trade.get("taker"), trade.get("countersigner")
        qty, px, until = amount(trade.get("qty")), amount(trade.get("px")), trade.get("until")
        if (not isinstance(trade.get("id"), str) or not TRADE_ID.fullmatch(trade["id"])
                or trade.get("side") not in ("buy", "sell") or qty is None or px is None
                or type(until) is not int or not isinstance(maker, str) or not isinstance(signer, str)
                or not (taker == "any" or isinstance(taker, str))):
            return "shape"
        if qty < self.min_qty:
            return "shape"
        if maker not in self.accounts or signer not in self.accounts:
            return "not_owner"
        if taker != "any" and taker != signer:
            return "taker"
        if trade["id"] in self.settled:
            return "settled"
        if n > until:
            return "expired"
        if n > self.lock:
            return "locked"
        if abs(px - ref) > self.window * ref:
            return "limits"
        side = 1 if trade["side"] == "buy" else -1
        mk_fee, tk_fee = self.side_fees(side, qty, px, close)
        mk, tk = self.accounts[maker], self.accounts[signer]
        if mk is tk:
            if mk.cash < mk_fee + tk_fee:
                return "funds"
        elif (mk.cash < mk.opening(side, qty) * px + mk_fee
              or tk.cash < tk.opening(-side, qty) * px + tk_fee):
            return "funds"
        return None

    def sweep(self, n: int, ref: str, close: str, owners: list, trades: list) -> dict:
        reference, closing = amount(ref), amount(close)
        if (self.global_px is None or reference is None or closing is None or type(n) is not int
                or n <= self.sweep_n):
            raise ValueError(f"sweep {n}: needs a seed, a reference and a closing price, and an increasing sweep number")
        self.sweep_n = n
        minted = []
        for key in owners:
            if isinstance(key, str) and DID.fullmatch(key) and key not in self.accounts and n <= self.lock:
                self.accounts[key] = Account(key, self.mint)
                minted.append(key)
        outcomes, volume, notional = [], Decimal(0), Decimal(0)
        for trade in trades:
            reason = self.check(trade, n, reference, closing)
            tid = trade.get("id") if isinstance(trade, dict) else None
            if reason:
                outcomes.append({"id": tid, "outcome": "void", "reason": reason})
                continue
            qty, px = Decimal(trade["qty"]), Decimal(trade["px"])
            side = 1 if trade["side"] == "buy" else -1
            mk_fee, tk_fee = self.side_fees(side, qty, px, closing)
            mk, tk = self.accounts[trade["maker"]], self.accounts[trade["countersigner"]]
            if mk is tk:
                mk.cash -= mk_fee + tk_fee
                mk.fees += mk_fee + tk_fee
            else:
                mk.apply(side, qty, px, mk_fee)
                tk.apply(-side, qty, px, tk_fee)
            self.fees += mk_fee + tk_fee
            self.settled.add(trade["id"])
            volume += qty
            notional += qty * px
            outcomes.append({"id": tid, "outcome": "settled", "maker_fee": str(mk_fee), "taker_fee": str(tk_fee)})
        if volume:
            self.global_px = notional / volume
        return {"sweep": n, "reference": str(reference), "close": str(closing), "minted": minted, "trades": outcomes,
                "global_price": str(self.global_px.quantize(CENT))}

    def final(self, px: str) -> dict:
        s = amount(px)
        if s is None or self.final_px is not None:
            raise ValueError("final: expected one closing price with at most two decimals")
        self.final_px = s
        scores = {k: a.value_at(s) - self.mint for k, a in self.accounts.items()}
        order = sorted(scores, key=lambda k: (-scores[k], k))
        winners, place = {}, 0
        while place < min(self.places, len(order)):   # tied owners share the places they span
            tied = [k for k in order if scores[k] == scores[order[place]]]
            spanned = list(range(place + 1, min(place + len(tied), self.places) + 1))
            for k in tied:
                winners[k] = (spanned, len(tied))
            place += len(tied)
        table = [{"key": k, "score": str(scores[k].quantize(Decimal("0.000001"))),
                  "position": str(self.accounts[k].position), "fees": str(self.accounts[k].fees),
                  "places": winners.get(k, ([], 0))[0], "sharing": winners.get(k, ([], 0))[1]}
                 for k in order]
        return {"S": str(s), "owners": len(order), "fees": str(self.fees),
                "zero_sum": str(sum(scores.values(), Decimal(0)) + self.fees), "standings": table}


def replay(lines, config: dict | None = None) -> dict:
    fold, sweeps, result = Fold(config), [], None
    with localcontext() as ctx:
        ctx.prec = 60
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event.get("t")
            if kind == "seed":
                fold.seed(event.get("px"))
            elif kind == "sweep":
                sweeps.append(fold.sweep(event.get("n"), event.get("ref"), event.get("close"),
                                         event.get("owners", []), event.get("trades", [])))
            elif kind == "final":
                result = fold.final(event.get("px"))
            else:
                raise ValueError(f"line {number}: unknown event {kind!r}")
    return {"sweeps": sweeps, "final": result}


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay Close Call sweeps and print balances and standings.")
    parser.add_argument("events", type=Path, help="JSON Lines file of seed, sweep and final events")
    parser.add_argument("--config", type=Path, help="contest.json; its fold settings override the defaults")
    args = parser.parse_args()
    try:
        config = None
        if args.config:
            contest = json.loads(args.config.read_text(encoding="utf-8"))
            config = {k: contest[k] for k in DEFAULTS if k in contest}
        result = replay(args.events.read_text(encoding="utf-8").splitlines(), config)
    except (OSError, ValueError, KeyError) as error:
        print(f"fold: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
