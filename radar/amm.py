"""
radar/amm.py — constant-product swap math, ported byte-for-byte from the
oylswap library so projections match what the contract will actually compute.

SOURCE OF TRUTH
    ~/Documents/github/subfrost-alkanes/crates/oylswap-library/src/lib.rs
      * get_amount_out  (lines 206-218)
      * get_amount_in   (lines 219-234)
      * DEFAULT_TOTAL_FEE_AMOUNT_PER_1000 = 10   (line 13)

THE FEE IS 1%, NOT 0.3%
    `total_fee_per_1000 = 10` means 10/1000 = 1.00%. Everyone's muscle memory
    says Uniswap's 30/10000 = 0.30%; using 0.3% here would under-state every
    swap's cost and over-state every output by ~0.7% of notional. Verified in
    source 2026-08-24.

EXACT INTEGER SEMANTICS (why this is not written with floats)
    amount_in_with_fee = (1000 - fee) * amount_in
    numerator          = amount_in_with_fee * reserve_out
    denominator        = 1000 * reserve_in + amount_in_with_fee
    amount_out         = numerator // denominator        <- FLOOR division

    Python ints are arbitrary precision, so this reproduces the contract's U256
    arithmetic exactly, including the truncation. Floats would drift on large
    reserves (DIESEL pools run ~1e15 base units) and the drift compounds across
    a multi-hop path. Keep everything in int.

WHAT THIS MODULE DELIBERATELY DOES NOT MODEL
    * Reverts. If min_out is violated the real swap reverts and the reserves do
      NOT move. The caller (radar/projection.py) decides how to treat that; see
      simulate_path's `honors_min_out` flag.
    * Fee-on-transfer or rebasing tokens. Not present in this AMM.
    * Concentrated liquidity. This is plain x*y=k.
"""

DEFAULT_FEE_PER_1000 = 10  # 1.00% - oylswap-library/src/lib.rs:13


def get_amount_out(amount_in, reserve_in, reserve_out, fee_per_1000=DEFAULT_FEE_PER_1000):
    """Exact port of oylswap-library get_amount_out (floor division included)."""
    if amount_in <= 0:
        return 0
    if reserve_in <= 0 or reserve_out <= 0:
        return 0
    amount_in_with_fee = (1000 - fee_per_1000) * amount_in
    numerator = amount_in_with_fee * reserve_out
    denominator = 1000 * reserve_in + amount_in_with_fee
    return numerator // denominator


def get_amount_in(amount_out, reserve_in, reserve_out, fee_per_1000=DEFAULT_FEE_PER_1000):
    """Exact port of oylswap-library get_amount_in (note the +1 round-up)."""
    if amount_out <= 0 or reserve_in <= 0 or reserve_out <= 0 or amount_out >= reserve_out:
        return None
    numerator = 1000 * reserve_in * amount_out
    denominator = (1000 - fee_per_1000) * (reserve_out - amount_out)
    return numerator // denominator + 1


def spot_price(reserve_in, reserve_out):
    """Marginal price of 1 unit of `in` denominated in `out`, fee excluded.

    Float is fine HERE (display only). Never feed this back into reserve math.
    """
    if reserve_in <= 0:
        return 0.0
    return reserve_out / reserve_in


def price_impact(amount_in, reserve_in, reserve_out, fee_per_1000=DEFAULT_FEE_PER_1000):
    """Fractional degradation of execution price vs the pre-trade spot price.

    Returns e.g. 0.021 for a 2.1% worse-than-spot fill. This INCLUDES the fee,
    because that is what a trader actually experiences.
    """
    out = get_amount_out(amount_in, reserve_in, reserve_out, fee_per_1000)
    if out <= 0 or amount_in <= 0:
        return 0.0
    ideal = amount_in * spot_price(reserve_in, reserve_out)
    if ideal <= 0:
        return 0.0
    return max(0.0, 1.0 - (out / ideal))


def apply_swap(reserve_in, reserve_out, amount_in, fee_per_1000=DEFAULT_FEE_PER_1000):
    """Return (new_reserve_in, new_reserve_out, amount_out) after one swap.

    The full amount_in enters the pool (the fee stays in the reserves, which is
    exactly how the LP accrues value); only the computed amount_out leaves.
    """
    out = get_amount_out(amount_in, reserve_in, reserve_out, fee_per_1000)
    return reserve_in + amount_in, reserve_out - out, out


def simulate_path(pools, path, amount_in, min_out=None, fee_per_1000=DEFAULT_FEE_PER_1000):
    """Walk a multi-hop swap path, mutating a COPY of pool reserves.

    `pools` maps a frozenset({token_a, token_b}) -> dict(reserves={tok: amt}).
    `path` is the ordered token id list from the cellpack, e.g.
    ["32:0", "2:0", "2:490"] for a two-hop route.

    Returns a dict with the per-hop breakdown, the final output, and whether
    min_out is satisfied. If any hop has no known pool we bail with
    reason="unknown-pool" rather than guessing - a fabricated reserve would
    silently poison the whole dashboard.
    """
    hops = []
    amt = int(amount_in)
    for a, b in zip(path, path[1:]):
        key = frozenset((a, b))
        pool = pools.get(key)
        if pool is None:
            return {"ok": False, "reason": "unknown-pool", "missing": [a, b], "hops": hops}
        r_in = int(pool["reserves"].get(a, 0))
        r_out = int(pool["reserves"].get(b, 0))
        if r_in <= 0 or r_out <= 0:
            return {"ok": False, "reason": "empty-reserves", "missing": [a, b], "hops": hops}
        before = spot_price(r_in, r_out)
        new_in, new_out, out = apply_swap(r_in, r_out, amt, fee_per_1000)
        if out <= 0:
            return {"ok": False, "reason": "zero-output", "hops": hops}
        hops.append(
            {
                "pool_key": sorted(key),
                "token_in": a,
                "token_out": b,
                "amount_in": amt,
                "amount_out": out,
                "reserve_in_before": r_in,
                "reserve_out_before": r_out,
                "reserve_in_after": new_in,
                "reserve_out_after": new_out,
                "spot_before": before,
                "spot_after": spot_price(new_in, new_out),
                "impact": price_impact(amt, r_in, r_out, fee_per_1000),
            }
        )
        amt = out
    honors = None if min_out is None else amt >= int(min_out)
    return {
        "ok": True,
        "hops": hops,
        "amount_out": amt,
        "min_out": None if min_out is None else int(min_out),
        "honors_min_out": honors,
    }


def commit_path(pools, sim):
    """Write a successful simulate_path result back into `pools` in place.

    Separated from simulate_path so the caller can choose NOT to commit a swap
    that violates min_out (it would revert on chain and leave reserves alone).
    """
    if not sim.get("ok"):
        return
    for hop in sim["hops"]:
        key = frozenset(hop["pool_key"])
        pool = pools.get(key)
        if pool is None:
            continue
        pool["reserves"][hop["token_in"]] = hop["reserve_in_after"]
        pool["reserves"][hop["token_out"]] = hop["reserve_out_after"]
