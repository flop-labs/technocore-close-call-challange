"""Close Call rules check: an agent-based simulation of the draft rules, at several scales, with gaming scenarios.

Rules simulated (the proposed rules are the defaults):
- one NVDA future in POLF, 1 POLF per $; 10,000 POLF per owner key; a did:key is a player (no human checks)
- no book: a maker signs an offer (id, side, qty, price, last sweep); a taker countersigns; either posts it
- every 5-minute sweep the referee applies countersigned trades in stamp order; each settles in full or is
  void: id already settled, expired, price outside the window, or either side short of funds
- limit up/down: a trade must be within 2% of the reference, Hyperliquid's last price posted at the previous
  sweep (variant "hl", band_w=0.02); nothing is ever reset to that price
- fee_mode: "flat" 1% a side; "max" 1% or the trade's price gap to the reference, whichever is more (the
  default, also dev_fee=True); "plus" 1% plus the gap; "beyond:x" 1% within x of the reference, the gap beyond.
  Otherwise: a fee of 1% of value on each side, or the trade's distance from the reference if that is larger,
  so a discount handed across the window is paid back as fee; every contract opened, long or short, ties up
  its price; no margin calls
- global price after a sweep: volume-weighted price of its trades, unchanged if nothing settled; it only marks
  the live board
- score: POLF after settlement at S (Hyperliquid, one hour after the lock) minus 10,000
- 1,000,000 FLOP split among the three highest scores; no liveness rule. The suite counts prize
  places won, not FLOP, since the split between places is not part of the rules

Alternatives the suite compares:
- variant "vwap": the band is measured from our own last global price instead (band_w, flat fee)
- limit: a window around Hyperliquid on top of the "vwap" band
- max_move (speed limit), guard (snap to Hyperliquid), protect / prorata (best price first, shared fills);
  the walk-then-funnel group can know the guard ("aware")

Honest agents: trackers (read Hyperliquid, quote open offers 1.2% either side, accept mispriced offers),
noise traders (four random trades a day), bettors (all-in on day 1, then hold). Gaming groups are added by
scenario; their combined score is what they gain or lose. "harvest" is a favoured key that buys at the bottom
of the window from one free key and sells at the top to another, every sweep.
Prices are synthetic: a random walk with two 3-6% jumps. Not a forecast.

    uv run --with numpy close_call_sim.py            # full suite, writes sim_results.json
"""
import json, math, random, sys, time
import numpy as np

SWEEPS, AFTER = 2556, 12                  # 12:05 Fri 25 Sep .. 09:00 Sun 4 Oct; S at 10:00
MINT, FEE, BAND, SEED = 10_000.0, 0.01, 0.01, 180.0
PLACES = 3
EPS = 1e-9


def hl_path(rng):
    n = SWEEPS + AFTER
    hours = np.arange(1, n + 1) * 5 / 60
    weekend = ((hours > 9) & (hours <= 58)) | ((hours > 177) & (hours <= 226))
    r = rng.normal(0, np.where(weekend, 0.012, 0.03) / math.sqrt(288))
    jumps = []
    for _ in range(2):
        j = int(rng.integers(24, SWEEPS - 24))
        size = float(rng.choice([-1, 1]) * rng.uniform(0.03, 0.06))
        r[j] += size
        jumps.append(j)
    return SEED * np.exp(np.cumsum(r)), jumps


class Acct:
    __slots__ = ("id", "kind", "group", "R", "q", "e", "fees", "dir")

    def __init__(self, i, kind, group="honest"):
        self.id, self.kind, self.group = i, kind, group
        self.R, self.q, self.e, self.fees = MINT, 0.0, 0.0, 0.0
        self.dir = 0

    def split(self, side, qty):
        close = min(qty, max(-self.q, 0.0)) if side > 0 else min(qty, max(self.q, 0.0))
        return close, qty - close

    def need(self, side, qty, px, fr=None):
        return self.split(side, qty)[1] * px + (FEE if fr is None else fr) * qty * px

    def apply(self, side, qty, px, fr=None):
        close, open_ = self.split(side, qty)
        fee = (FEE if fr is None else fr) * qty * px
        self.R -= fee
        self.fees += fee
        if close > 0:
            self.R += close * (2 * self.e - px) if side > 0 else close * px
            self.q += side * close
            if abs(self.q) < EPS:
                self.q, self.e = 0.0, 0.0
        if open_ > 0:
            self.R -= open_ * px
            self.e = px if abs(self.q) < EPS else (abs(self.q) * self.e + open_ * px) / (abs(self.q) + open_)
            self.q += side * open_

    def final(self, S):
        return self.R + (self.q * S if self.q > 0 else -self.q * (2 * self.e - S))


def r2(x):
    return math.floor(x * 100 + 1e-6) / 100


DEFAULT = "hl"   # proposed rule: limit up/down around Hyperliquid's last price; the market sets its own price inside it


def run(seed, n=100, variant=DEFAULT, scenario=None, pos_cap=None, fee=0.01, band_w=0.02, max_move=None,
        guard=None, prorata=False, room_price=False, protect=False, limit=None, dev_fee=True, fee_mode=None):
    global FEE, BAND
    FEE, BAND = fee, band_w
    edge = fee + 0.002          # trackers need the fee plus a margin
    rng = np.random.default_rng(seed)
    pr = random.Random(seed * 7919 + n)
    h, jumps = hl_path(rng)
    S = float(h[-1])
    accts = []
    n_track, n_noise = int(n * 0.4), int(n * 0.4)
    n_bet = n - n_track - n_noise
    for kind, m in (("tracker", n_track), ("noise", n_noise), ("bettor", n_bet)):
        for _ in range(m):
            a = Acct(len(accts), kind)
            if kind == "bettor":
                a.dir = pr.choice([-1, 1])
            accts.append(a)
    sc = scenario or {}
    group = []
    for i in range(sc.get("keys", 0)):
        a = Acct(len(accts), sc["name"], sc["name"])
        a.dir = 1 if i % 2 == 0 else -1
        accts.append(a)
        group.append(a)
    offers, settled_ids = {}, set()
    P, next_id = SEED, 0
    PA = None                     # the attackers' own room price, if rooms price separately
    stats = dict(settled=0, void_taken=0, void_expired=0, void_band=0, void_funds=0, void_cap=0, volume=0.0)
    honest_void_funds = 0
    funnel_refs, funnel_h = [], []
    bracket = dict(alive=list(range(sc.get("keys", 0))), pairs=[], open_px=None)
    track_err, dev = [], []

    band = [0.0, 0.0]          # this sweep's band in whole cents, inclusive
    mode = fee_mode or ("max" if dev_fee else "flat")

    def fee_rate(px, ref_px):
        """Fee as a share of the trade's value. The distance is the absolute price gap to the reference,
        so a trade far below it pays back the whole discount, not a share of its own small value."""
        dist = abs(px - ref_px) / px
        if mode == "max":
            return max(FEE, dist)
        if mode == "plus":
            return FEE + dist
        if mode.startswith("beyond:"):
            return FEE if abs(px / ref_px - 1) <= float(mode.split(":")[1]) else dist
        return None

    captured = {}              # value each gaming key captured against Hyperliquid's price at the time
    settled_per_sweep = []

    def cents(px):
        return min(max(round(px, 2), band[0]), band[1])

    def post(maker, side, qty, px, until, k):
        nonlocal next_id
        qty = r2(qty)
        if qty >= 0.1:
            offers[next_id] = dict(id=next_id, maker=maker, side=side, qty=qty, px=cents(px), until=until, k=k, open=True)
            next_id += 1

    def direct(trades, k, a, b, side_a, qty, px, raw=False):
        """a and b agree privately; a is the maker taking side_a."""
        nonlocal next_id
        o = dict(id=next_id, maker=a, side=side_a, qty=r2(qty), px=round(px, 2) if raw else cents(px), until=k, k=k)
        next_id += 1
        trades.append((pr.random() + k, o, b))

    for k in range(SWEEPS):
        hp = float(h[k - 1]) if k > 0 else SEED
        ref = hp if variant == "hl" else P
        lo, hi = ref * (1 - BAND), ref * (1 + BAND)
        if limit:                   # limit up/down: every trade also within `limit` of Hyperliquid's last price
            lo, hi = max(lo, hp * (1 - limit)), min(hi, hp * (1 + limit))
        if room_price:
            PA = P if PA is None else PA
            alo, ahi = PA * (1 - BAND), PA * (1 + BAND)
        band[0], band[1] = math.ceil(lo * 100 - 1e-6) / 100, math.floor(hi * 100 + 1e-6) / 100
        for oid in [oid for oid, o in offers.items() if o["until"] < k or oid in settled_ids]:
            del offers[oid]
        asks = sorted((o for o in offers.values() if o["k"] < k and o["side"] < 0 and lo <= o["px"] <= hi), key=lambda o: o["px"])
        bids = sorted((o for o in offers.values() if o["k"] < k and o["side"] > 0 and lo <= o["px"] <= hi), key=lambda o: -o["px"])
        trades = []

        stamp_base = {}

        def take(taker, o):
            st = stamp_base.get(taker.id)
            st = (pr.random() * 0.9 + k) if st is None else st + 1e-6
            stamp_base[taker.id] = st
            trades.append((st, o, taker))

        def funded(x):
            return x["maker"].R + EPS >= x["maker"].need(x["side"], x["qty"], x["px"])

        fb = next((x for x in bids if funded(x)), None)
        fa = next((x for x in asks if funded(x)), None)

        def inside(px):
            """a private price that doesn't trade through the funded open offers visible now"""
            if protect:
                if fa is not None:
                    px = min(px, fa["px"])
                if fb is not None:
                    px = max(px, fb["px"])
            return px

        order = accts[:]
        pr.shuffle(order)
        for a in order:
            kind = a.kind
            if kind == "tracker":
                if pr.random() > 1 / 6:
                    continue
                f = hp * (1 + pr.gauss(0, 0.002))
                lim = min(25, pos_cap or 25)
                room_b, room_s = lim - a.q, lim + a.q
                for o in asks:
                    if o["px"] >= f * (1 - edge):
                        break
                    if o["maker"] is not a and o["qty"] <= room_b and o["maker"].R >= o["maker"].need(-1, o["qty"], o["px"]):
                        take(a, o); room_b -= o["qty"]; room_s += o["qty"]
                for o in bids:
                    if o["px"] <= f * (1 + edge):
                        break
                    if o["maker"] is not a and o["qty"] <= room_s and o["maker"].R >= o["maker"].need(1, o["qty"], o["px"]):
                        take(a, o); room_s -= o["qty"]; room_b += o["qty"]
                bid, ask = min(f * (1 - edge), hi), max(f * (1 + edge), lo)
                if bid >= lo and a.q < lim:
                    post(a, 1, 2, bid, k + 6, k)
                if ask <= hi and a.q > -lim:
                    post(a, -1, 2, ask, k + 6, k)
            elif kind == "noise":
                if pr.random() > 1 / 72:
                    continue
                side = pr.choice([-1, 1])
                book = asks if side > 0 else bids
                best = next((o for o in book if o["maker"] is not a), None)
                if best:
                    take(a, best)
                else:
                    post(a, side, pr.uniform(0.5, 3), ref * (1 + 0.005 * side), k + 12, k)
            elif kind == "bettor":
                if k < 24:
                    side = a.dir
                    want = 0.9 * MINT / ref - abs(a.q)
                    if pos_cap:
                        want = min(want, pos_cap - abs(a.q))
                    if want >= 0.1:
                        got = 0.0
                        for o in (asks if side > 0 else bids):
                            if got + o["qty"] > want:
                                break
                            if o["maker"] is not a:
                                take(a, o); got += o["qty"]
                        if got < want:
                            post(a, side, min(want - got, 10), ref * (1 + 0.01 * side), k + 3, k)

        # ---------- gaming groups ----------
        name = sc.get("name")
        if name == "farm":
            if k == 0:            # pairs go all-in against each other at the reference price
                for i in range(0, len(group) - 1, 2):
                    q = r2(min(0.98 * MINT / (ref * (1 + FEE)), pos_cap or 1e9))
                    direct(trades, k, group[i], group[i + 1], 1, q, ref)
        elif name == "wash" and sc["start"] <= k < sc["start"] + sc["sweeps"]:
            whi = math.floor(ahi * 100 + 1e-6) / 100 if room_price else band[1]
            if protect and not room_price:
                for x in asks:          # take every honest ask below the price it wants to print
                    if x["maker"].group == "honest" and x["px"] < whi:
                        take(group[len(trades) % len(group)], x)
            for i in range(0, len(group) - 1, 2):
                direct(trades, k, group[i], group[i + 1], -1 if (k - sc["start"]) % 2 == 0 else 1, sc["qty"], whi, raw=True)
        elif name == "walkdump":
            s0, w = sc["start"], sc["sweeps"]
            if s0 <= k < s0 + w:
                direct(trades, k, group[0], group[1], -1 if (k - s0) % 2 == 0 else 1, sc["qty"], hi)
            elif s0 + w <= k < s0 + w + 6:
                for g in group:     # dump: open offers to sell at the bottom of the walked band
                    post(g, -1, 5, lo, k + 1, k)
        elif name == "funnel" and k < 24:
            main, feeders = group[0], group[1:]
            for fdr in feeders:     # feeders sell to the main key 1% under the price, the most the band allows
                if abs(main.q) < 0.9 * MINT / ref:
                    direct(trades, k, fdr, main, -1, 2, lo)
                    funnel_refs.append(ref); funnel_h.append(hp)
        elif name == "walkfunnel":
            main, feeders = group[0], group[1:]
            s0, w = sc["start"], sc["sweeps"]
            flo = math.ceil(alo * 100 - 1e-6) / 100 if room_price else band[0]
            hold = None
            if sc.get("aware") and guard:   # a group that knows the guard parks the price just inside it
                hold = min(max(math.ceil(hp * (1 - guard + 0.002) * 100) / 100, flo), band[1])
            if s0 <= k < s0 + w:        # feeders wash at the bottom of the band to walk the price down
                if protect and not room_price:
                    for x in bids:      # first sell into every honest bid above the floor
                        if x["maker"].group == "honest" and x["px"] > flo:
                            take(feeders[len(trades) % len(feeders)], x)
                for i in range(0, len(feeders) - 1, 2):
                    direct(trades, k, feeders[i], feeders[i + 1], -1 if (k - s0) % 2 == 0 else 1, sc["qty"], flo if hold is None else hold, raw=True)
            elif s0 + w <= k < s0 + w + sc.get("funnel_sweeps", 4):   # then sell to the main key at the walked-down floor
                sold = 0.0
                for fdr in feeders:
                    if abs(main.q) < 0.9 * MINT / ref:
                        direct(trades, k, fdr, main, -1, 5, flo, raw=True)
                        funnel_refs.append(ref); funnel_h.append(hp)
                        sold += 5
                if hold is not None and sold:   # an equal wash at the top of the band keeps the average inside the guard
                    direct(trades, k, feeders[0], feeders[1], 1 if k % 2 else -1, sold, band[1], raw=True)
        if name == "bracket":
            n_rounds = sc["rounds"]
            span = SWEEPS // n_rounds
            r_now, phase = divmod(k, span)
            if r_now < n_rounds and phase == 0 and bracket["pairs"]:
                px = cents(ref)             # close last round's pairs at the reference; the side the price favoured survives
                won = []
                for li, si, q in bracket["pairs"]:
                    direct(trades, k, group[li], group[si], -1, q, px)
                    won.append(li if px >= bracket["open_px"] else si)
                bracket["alive"], bracket["pairs"] = won, []
            elif r_now < n_rounds and phase == 1:
                px = cents(ref)             # survivors split in half, long against short, all-in
                al = bracket["alive"]
                for x, y in zip(al[0::2], al[1::2]):
                    q = r2(0.95 * min(group[x].R, group[y].R) / (px * (1 + FEE + 0.03)))
                    if q >= 0.1:
                        direct(trades, k, group[x], group[y], 1, q, px)
                        bracket["pairs"].append((x, y, q))
                bracket["open_px"] = px
        if name == "absurd" and k == sc.get("start", 12):
            main, feeders = group[0], group[1:]
            px = band[0]                    # the lowest price the limits allow
            fee_px = (fee_rate(px, hp) or FEE) * px
            for fdr in feeders:             # each free key sells the favoured key as much as both can fund
                q = r2(0.95 * min(fdr.R, main.R / len(feeders)) / (px + fee_px))
                direct(trades, k, fdr, main, -1, q, px, raw=True)
        if name == "sniper":
            now = float(h[k])               # the sniper sees Hyperliquid's price before the sweep closes
            sn = group[0]
            for book, side in ((asks, 1), (bids, -1)):
                for o in book:
                    if o["maker"].group != "honest":
                        continue
                    cost = (fee_rate(o["px"], hp) or FEE) + 0.001
                    gain = side * (now - o["px"]) / o["px"]
                    if gain > cost and abs(sn.q + side * o["qty"]) <= 50:
                        take(sn, o)
        if name == "harvest" and k >= sc.get("start", 12):
            main, feeders = group[0], group[1:]
            i = (k // 2) % (len(feeders) // 2)      # one pair of free keys for two sweeps, so their positions net out
            a, b = feeders[2 * i], feeders[2 * i + 1]
            seller, buyer = (a, b) if k % 2 == 0 else (b, a)
            direct(trades, k, seller, main, -1, sc["qty"], band[0], raw=True)   # the main key buys at the bottom of the window
            direct(trades, k, buyer, main, 1, sc["qty"], band[1], raw=True)     # and sells at the top, ending flat
        if name == "grief" and pr.random() < 1 / 3:
            for g in group:         # bait offers far beyond the key's funds
                f = hp
                for _ in range(5):
                    post(g, -1, 10, max(f * (1 - 0.013), lo), k + 2, k)
                    post(g, 1, 10, min(f * (1 + 0.013), hi), k + 2, k)

        # ---------- referee ----------
        vol, notional, own = 0.0, 0.0, {}
        avol, anot = 0.0, 0.0
        settled_before = set(settled_ids) if prorata else settled_ids
        if prorata:
            groups = {}
            for t in trades:
                if t[1].get("open"):
                    groups.setdefault(t[1]["id"], []).append(t)
            split = []
            for t in trades:
                g = groups.get(t[1]["id"])
                if not g or len(g) == 1:
                    split.append(t)
                    continue
                if t is not g[0] and t[1]["id"] in groups and t[1].get("_done"):
                    continue
                o = t[1]
                o["_done"] = True
                g = sorted(g, key=lambda x: x[0])
                q = o["qty"]
                share = math.floor(q / len(g) * 100) / 100
                allocs = [share] * len(g) if share >= 0.1 else [0.1] * int(q / 0.1 + 1e-9) + [0.0] * len(g)
                allocs = allocs[:len(g)]
                allocs[0] = round(allocs[0] + q - sum(allocs), 2)
                for (st, oo, tk), qa in zip(g, allocs):
                    if qa >= 0.1:
                        split.append((st, dict(oo, qty=qa, part=True), tk))
                    else:
                        stats["void_taken"] += 1
            trades = split
        def through(o, mk, taker):
            """Accepting an open offer: the taker may not pay more (or get less) than another open offer.
            A private deal: neither side may trade through an open offer."""
            buyer, seller = (mk, taker) if o["side"] > 0 else (taker, mk)
            taker_buys = o["side"] < 0
            chk_buy = (not o.get("open")) or taker_buys
            chk_sell = (not o.get("open")) or not taker_buys
            if chk_buy:
                best_ask = next((x for x in asks if x["id"] != o["id"] and x["id"] not in settled_ids and x["maker"] is not buyer and funded(x)), None)
                if best_ask is not None and o["px"] > best_ask["px"] + EPS:
                    return True
            if chk_sell:
                best_bid = next((x for x in bids if x["id"] != o["id"] and x["id"] not in settled_ids and x["maker"] is not seller and funded(x)), None)
                if best_bid is not None and o["px"] < best_bid["px"] - EPS:
                    return True
            return False

        deferred, final_pass = [], False
        queue = sorted(trades, key=lambda t: t[0])
        while True:
          for stamp, o, taker in queue:
            mk = o["maker"]
            if (o["id"] in settled_before) if o.get("part") else (o["id"] in settled_ids):
                stats["void_taken"] += 1; continue
            if k > o["until"]:
                stats["void_expired"] += 1; continue
            in_att = room_price and (mk.group != "honest" and taker.group != "honest")
            blo, bhi = (math.ceil(alo * 100 - 1e-6) / 100, math.floor(ahi * 100 + 1e-6) / 100) if in_att else (lo, hi)
            if not (blo - EPS <= o["px"] <= bhi + EPS):
                stats["void_band"] += 1; continue
            if protect and through(o, mk, taker):
                if not final_pass:
                    deferred.append((stamp, o, taker))
                    continue
                stats["void_through"] = stats.get("void_through", 0) + 1
                if mk.group == "honest" and taker.group == "honest":
                    stats["honest_through"] = stats.get("honest_through", 0) + 1
                continue
            if pos_cap and (abs(mk.q + o["side"] * o["qty"]) > pos_cap + EPS or abs(taker.q - o["side"] * o["qty"]) > pos_cap + EPS):
                stats["void_cap"] += 1; continue
            fr = fee_rate(o["px"], hp)
            if mk.R + EPS < mk.need(o["side"], o["qty"], o["px"], fr) or taker.R + EPS < taker.need(-o["side"], o["qty"], o["px"], fr):
                stats["void_funds"] += 1
                if taker.group == "honest":
                    honest_void_funds += 1
                continue
            mk.apply(o["side"], o["qty"], o["px"], fr)
            taker.apply(-o["side"], o["qty"], o["px"], fr)
            for acc, sd in ((mk, o["side"]), (taker, -o["side"])):
                if acc.group != "honest":
                    captured[acc.id] = captured.get(acc.id, 0.0) + sd * o["qty"] * (float(h[k]) - o["px"]) - (FEE if fr is None else fr) * o["qty"] * o["px"]
            settled_ids.add(o["id"])
            stats["settled"] += 1
            stats["volume"] += o["qty"]
            if in_att:
                avol += o["qty"]; anot += o["qty"] * o["px"]
            else:
                vol += o["qty"]; notional += o["qty"] * o["px"]
            for acc in (mk, taker):
                s = own.setdefault(acc.id, [0.0, 0.0]); s[0] += o["qty"] * o["px"]; s[1] += o["qty"]
          if final_pass or not deferred:
              break
          queue, deferred, final_pass = deferred, [], True
        settled_per_sweep.append(stats["settled"])
        if room_price and avol > 0:
            PA = anot / avol
        if variant in ("vwap", "hl"):
            if vol > 0:
                newP = notional / vol
                if max_move:            # the reference may move at most max_move a sweep
                    newP = min(max(newP, P * (1 - max_move)), P * (1 + max_move))
                P = newP
            if guard and abs(P / float(h[k]) - 1) > guard:   # snap back to Hyperliquid's last trade
                P = float(h[k])
        elif len(own) >= 5:
            P = sum(v[0] / v[1] for v in own.values()) / len(own)
        hk = float(h[k])
        track_err.append(abs(P / hk - 1)); dev.append(P / hk - 1)
        if abs(sum(a.q for a in accts)) > 1e-6:
            raise AssertionError("positions don't net to zero")
        if any(a.R < -1e-6 for a in accts):
            raise AssertionError("an account went below zero during the season")
        if pos_cap and any(abs(a.q) > pos_cap + 1e-6 for a in accts):
            raise AssertionError("position cap broken")

    finals = {a.id: a.final(S) - MINT for a in accts}
    fees = sum(a.fees for a in accts)
    zero_sum_gap = sum(finals.values()) + fees
    ranked = sorted(accts, key=lambda a: -finals[a.id])
    prize = {}
    for a in ranked[:PLACES]:
        prize[a.group] = prize.get(a.group, 0) + 1
    kinds = {}
    for a in accts:
        kinds.setdefault(a.kind, []).append(finals[a.id])
    top_kinds = [a.kind for a in ranked[:3]]
    per = np.diff([0] + settled_per_sweep)
    post_jump = [int(per[j:j + 12].sum()) for j in jumps]
    lag = []
    for j in jumps:
        m = next((i for i in range(j, min(SWEEPS, j + 300)) if abs(dev[i]) < 0.012), None)
        lag.append(None if m is None else m - j)
    out = dict(seed=seed, n=n, variant=variant, scenario=sc.get("name"), pos_cap=pos_cap, move=S / SEED - 1,
               stats=stats, fees=fees, zero_sum_gap=zero_sum_gap,
               track_mean=float(np.mean(track_err)), track_p95=float(np.percentile(track_err, 95)),
               jump_lag=lag, kinds={k: dict(mean=float(np.mean(v)), n=len(v)) for k, v in kinds.items()},
               scores={k: [round(x, 1) for x in v] for k, v in kinds.items()},
               top_kinds=top_kinds, top_scores=[round(finals[a.id], 1) for a in ranked[:3]],
               prize=prize, honest_void_funds=honest_void_funds, post_jump_trades=post_jump)
    if group:
        out["group_score"] = sum(finals[a.id] for a in group)
        out["group_best"] = max(finals[a.id] for a in group)
        out["group_fees"] = sum(a.fees for a in group)
        out["main_score"] = finals[group[0].id]
        out["main_edge"] = captured.get(group[0].id, 0.0)
        out["group_edge"] = sum(captured.values())
        if sc["name"] in ("funnel", "walkfunnel"):
            main = group[0]
            h_avg = float(np.mean(funnel_h)) if funnel_h else SEED
            out["funnel_boost"] = main.q * h_avg * (1 + FEE) - (main.q * main.e + main.fees)
            out["funnel_main_q"] = main.q
        if sc["name"] in ("wash", "walkdump", "walkfunnel"):
            s0, w = sc["start"], sc["sweeps"]
            out["dev_window"] = float(max((abs(x) for x in dev[s0:s0 + w + 36]), default=0))
            out["recovery"] = next((i - (s0 + w) for i in range(s0 + w, min(SWEEPS, s0 + w + 400)) if abs(dev[i]) < 0.012), None)
    else:
        out["dev_window"] = float(max(abs(x) for x in dev[168:168 + 48]))
    return out


SUITE = {
    "scale": [(n, s) for n, seeds in ((30, 20), (100, 20), (300, 10), (1000, 5)) for s in range(seeds)],
    "scenarios": {
        "wash": dict(name="wash", keys=2, qty=25.0, start=168, sweeps=12),
        "walkdump": dict(name="walkdump", keys=2, qty=25.0, start=168, sweeps=12),
        "funnel": dict(name="funnel", keys=11),
        "grief": dict(name="grief", keys=5),
        "walkfunnel": dict(name="walkfunnel", keys=5, qty=25.0, start=168, sweeps=10),
        "harvest": dict(name="harvest", keys=81, qty=10.0, start=12),
        "farm10": dict(name="farm", keys=10),
        "farm50": dict(name="farm", keys=50),
        "absurd": dict(name="absurd", keys=11, start=12),
        "sniper": dict(name="sniper", keys=1),
        "bracket16": dict(name="bracket", keys=16, rounds=4),
    },
}
ROOM = dict(variant="vwap", band_w=0.01, dev_fee=False)     # a 1% band on our own last price, flat fee
WINDOWS = {
    "band 1% on our last price, fee 1%": ROOM,
    "window 1% of Hyperliquid, fee 1%": dict(band_w=0.01, dev_fee=False),
    "window 2%, fee 1%": dict(dev_fee=False),
    "window 2%, fee 2%": dict(dev_fee=False, fee=0.02),
    "window 2%, fee 1% or distance": dict(),
    "window 5%, fee 1% or distance": dict(band_w=0.05),
}
FIXES = {    # ways to stop the walk if the band stays on our own last price
    "speed 3%/h": dict(ROOM, max_move=0.03 / 12),
    "guard 2%": dict(ROOM, guard=0.02),
    "best price": dict(ROOM, protect=True),
}


def jobs():
    """(result key, seed, run kwargs) for the whole suite."""
    sc = SUITE["scenarios"]
    for n, s in SUITE["scale"]:
        yield ("scale",), s, dict(n=n)
    for name, x in sc.items():
        for s in range(20):
            yield ("scenarios", name), s, dict(n=100, scenario=x)
    for label, kw in WINDOWS.items():
        for nm in ("base", "wash", "walkfunnel", "harvest", "farm10", "absurd", "sniper", "bracket16"):
            for s in range(20):
                yield ("windows", label, nm), s, dict(n=100, scenario=sc.get(nm), **kw)
    walk = dict(sc["walkfunnel"], aware=True)
    for label, kw in FIXES.items():
        for nm, x in (("base", None), ("walkfunnel", walk), ("farm10", sc["farm10"])):
            for s in range(20):
                yield ("fixes", label, nm), s, dict(n=100, scenario=x, **kw)
    for n, kk in ((1000, 10), (1000, 100)):
        for s in range(5):
            yield ("farm_scale", f"{n}/{kk}"), s, dict(n=n, scenario=dict(name="farm", keys=kk))
    for name in ("base", "farm10"):
        for s in range(20):
            yield ("capped", name), s, dict(n=100, scenario=sc.get(name), pos_cap=10)


def _one(job):
    key, seed, kw = job
    return key, run(seed, **kw)


if __name__ == "__main__":
    from multiprocessing import Pool
    out_path = sys.argv[1] if len(sys.argv) > 1 else "sim_results.json"
    t0 = time.time()
    todo = list(jobs())
    with Pool() as pool:
        done = pool.map(_one, todo, chunksize=4)
    res = {}
    for key, r in done:
        node = res
        for k in key[:-1]:
            node = node.setdefault(k, {})
        node.setdefault(key[-1], []).append(r)
    json.dump(res, open(out_path, "w"), default=float)
    print(f"{len(todo)} seasons in {time.time() - t0:.0f}s")
