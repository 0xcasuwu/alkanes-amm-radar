"""
radar/projection.py — turn a mempool snapshot into projected price action.

THE QUESTION THIS ANSWERS
    "Granting that all pending transactions come to pass, what changes?"

    Three independent engines, because alkanes price action has three distinct
    mechanical sources:
      1. DIESEL issuance  - pro-rata mint split (radar/diesel.py). Always on.
                            The dominant signal: mint counts swing 1310..4892
                            block to block, a 3.7x move in per-mint yield.
      2. AMM reserves     - pending swaps walked through x*y=k (radar/amm.py).
                            Rare but high-impact.
      3. frBTC supply     - wrap/unwrap net flow changes the pegged supply.

ORDERING MATTERS AND WE DO NOT PRETEND OTHERWISE
    Swaps are path-dependent: two swaps hitting one pool produce different
    final reserves depending on which lands first. We apply them in PROJECTED
    BLOCK ORDER (the ancestor-fee-rate order from radar/blocktemplate.py),
    which is the best available proxy for miner ordering. It is a proxy, not a
    guarantee - miners can and do order differently, and private relays
    (Rebar Shield) are invisible to us entirely.

REVERTS ARE MODELLED, NOT IGNORED
    A swap whose projected output falls below its own min_out would REVERT on
    chain, leaving reserves untouched. We detect that (amm.simulate_path
    -> honors_min_out) and by default do NOT commit it, then report it as a
    predicted revert. This matters: a naive engine that commits every pending
    swap will over-state price movement precisely during the volatile moments
    when several swaps stack on one pool.

EXTRAPOLATION UNDER PARTIAL COVERAGE
    If we classified only 60% of the projected block, the raw mint count is an
    undercount. We report BOTH the observed count and a coverage-scaled
    estimate, always labelled, never blended into one confident-looking number.

THE PROJECTION IS TIME-DEPENDENT WITHIN A BLOCK INTERVAL
    Observed live 2026-08-24: pending mint counts collapse the instant a block
    is mined (the queue just confirmed) and then rebuild over the following
    minutes. Sampled 30 s after a block the radar saw ~1,055 pending mints;
    sampled late in the interval it saw 2,247-3,431. So "DIESEL per mint" is
    HIGHEST right after a block and falls as the queue refills.
    Read the number as "if the block sealed at this instant", which is exactly
    what it says - not as a stable forecast of the whole interval.

MEASURED BIAS (first scored blocks, 2026-08-24)
    Mint-count predictions came in ~10-13% HIGH on the first two scored blocks
    (2020 vs 1787, 606 vs 550). Plausible causes: our greedy template packs
    slightly more small mint txs than the miner did, and some pending mints
    conflict or get replaced before inclusion. We deliberately do NOT apply a
    correction factor - calibrating on two samples would be curve-fitting.
    The scoreboard exists so this bias stays visible and can be judged once
    enough blocks accumulate.

EVERY OUTPUT IS AN UPPER BOUND ON INTENT
    These are decoded REQUESTS. The VM may revert any of them (slippage,
    deadline, insufficient balance, edict clamping). Treat the numbers as
    "what the mempool is asking for", not "what will happen".
"""

import time

from . import amm, diesel
from .pools import clone_reserves, human


def _fees_of(mempool, txids):
    total = 0.0
    for t in txids:
        e = mempool.get(t)
        if not e:
            continue
        f = (e.get("fees") or {}).get("modified")
        if f is None:
            f = (e.get("fees") or {}).get("base", 0)
        total += float(f) * 1e8
    return int(total)


def project_diesel(snapshot, scanner, previous_block=None):
    """Next-block DIESEL issuance from the projected template."""
    if not snapshot["templates"]:
        return None
    tpl = snapshot["templates"][0]
    txids = tpl["txids"]

    observed = 0
    scanned = 0
    for t in txids:
        recs = scanner.cache.get(t)
        if recs is None:
            continue
        scanned += 1
        if any(r.get("intent") == "diesel_mint" for r in recs):
            observed += 1

    coverage = scanned / len(txids) if txids else 1.0
    estimated = int(round(observed / coverage)) if coverage > 0 else observed

    fees = _fees_of(snapshot["mempool"], txids)
    height = snapshot["next_height"]

    proj_observed = diesel.predict(height, max(observed, 1), fees)
    proj_estimated = diesel.predict(height, max(estimated, 1), fees)

    out = {
        "height": height,
        "mints_observed": observed,
        "mints_estimated": estimated,
        "coverage": coverage,
        "block_fees_sats": fees,
        "projection": proj_estimated,
        "projection_observed_only": proj_observed,
        "template_weight": tpl["weight"],
        "template_txs": tpl["count"],
    }

    if previous_block:
        prev_per_mint = previous_block.get("value_per_mint")
        out["previous"] = previous_block
        out["change_pct"] = diesel.dilution_vs(prev_per_mint, proj_estimated["value_per_mint"])
        out["mint_change"] = estimated - (previous_block.get("mints") or 0)
    return out


def project_amm(snapshot, scanner, pool_state, token_meta=None, commit_reverts=False):
    """Walk every pending swap through the pools in projected block order."""
    if not pool_state or not pool_state.get("pools"):
        return {"available": False, "reason": "no-pool-state", "swaps": [], "pools": []}

    working = clone_reserves(pool_state["pools"])
    baseline = pool_state["pools"]

    swaps = []
    touched = set()

    ordered = []
    for tpl in snapshot["templates"]:
        ordered.extend(tpl["txids"])

    for txid in ordered:
        for rec in scanner.cache.get(txid) or []:
            if rec.get("intent") != "amm_swap":
                continue
            sim = amm.simulate_path(
                working, rec["path"], rec["amount_in"], rec.get("min_out")
            )
            entry = {
                "txid": txid,
                "path": rec["path"],
                "amount_in": rec["amount_in"],
                "min_out": rec.get("min_out"),
                "deadline": rec.get("deadline"),
                "ok": sim.get("ok"),
                "reason": sim.get("reason"),
                "amount_out": sim.get("amount_out"),
                "honors_min_out": sim.get("honors_min_out"),
                "hops": sim.get("hops", []),
            }
            # A swap that misses its own min_out reverts: reserves do not move.
            will_execute = sim.get("ok") and (
                sim.get("honors_min_out") is not False or commit_reverts
            )
            entry["will_execute"] = bool(will_execute)
            if will_execute:
                amm.commit_path(working, sim)
                for hop in sim["hops"]:
                    touched.add(frozenset(hop["pool_key"]))
            swaps.append(entry)

    pools_out = []
    for key in touched:
        before = baseline[key]["reserves"]
        after = working[key]["reserves"]
        toks = sorted(before.keys())
        if len(toks) != 2:
            continue
        a, b = toks
        p_before = amm.spot_price(before[a], before[b])
        p_after = amm.spot_price(after[a], after[b])
        change = ((p_after - p_before) / p_before * 100.0) if p_before else 0.0
        sym = (lambda t: token_meta.symbol(t) if token_meta else t)
        pools_out.append(
            {
                "pool_id": baseline[key].get("pool_id"),
                "token_a": a,
                "token_b": b,
                "symbol_a": sym(a),
                "symbol_b": sym(b),
                "reserve_a_before": before[a],
                "reserve_b_before": before[b],
                "reserve_a_after": after[a],
                "reserve_b_after": after[b],
                "price_before": p_before,
                "price_after": p_after,
                "change_pct": change,
                "abs_change_pct": abs(change),
            }
        )
    pools_out.sort(key=lambda p: p["abs_change_pct"], reverse=True)

    return {
        "available": True,
        "swaps": swaps,
        "swap_count": len(swaps),
        "executing": sum(1 for s in swaps if s["will_execute"]),
        "reverting": sum(1 for s in swaps if s.get("honors_min_out") is False),
        "unknown_pool": sum(1 for s in swaps if s.get("reason") == "unknown-pool"),
        "pools": pools_out,
        "pools_touched": len(pools_out),
        "reserves_age_sec": time.time() - pool_state.get("fetched_at", time.time()),
    }


def project_frbtc(snapshot, scanner):
    """Net frBTC supply pressure from pending wraps/unwraps."""
    wraps = unwraps = 0
    unwrap_amount = 0
    for tpl in snapshot["templates"]:
        for txid in tpl["txids"]:
            for rec in scanner.cache.get(txid) or []:
                if rec.get("intent") == "frbtc_wrap":
                    wraps += 1
                elif rec.get("intent") == "frbtc_unwrap":
                    unwraps += 1
                    unwrap_amount += rec.get("amount") or 0
    return {
        "wraps": wraps,
        "unwraps": unwraps,
        "unwrap_amount": unwrap_amount,
        "unwrap_amount_display": human(unwrap_amount, 8),
        "net_direction": "mint" if wraps > unwraps else ("burn" if unwraps > wraps else "flat"),
    }


def intent_census(snapshot, scanner):
    """Histogram of every decoded intent across the horizon - the raw census."""
    census = {}
    contracts = {}
    for tpl in snapshot["templates"]:
        for txid in tpl["txids"]:
            for rec in scanner.cache.get(txid) or []:
                census[rec["intent"]] = census.get(rec["intent"], 0) + 1
                tgt = rec.get("target")
                if tgt:
                    contracts[tgt] = contracts.get(tgt, 0) + 1
    return {
        "by_intent": dict(sorted(census.items(), key=lambda kv: -kv[1])),
        "by_contract": dict(sorted(contracts.items(), key=lambda kv: -kv[1])[:15]),
    }


def build(snapshot, scanner, pool_state, token_meta=None, previous_block=None, flow=None):
    """Assemble the full projection payload served to the dashboard."""
    return {
        "generated_at": time.time(),
        "tip": snapshot["tip"],
        "next_height": snapshot["next_height"],
        "mempool_size": snapshot["mempool_size"],
        "coverage": snapshot["coverage"],
        "scanned": snapshot["scanned"],
        "unscanned": snapshot["unscanned"],
        "sweep_elapsed": snapshot["elapsed"],
        "horizon_blocks": len(snapshot["templates"]),
        "templates": [
            {"count": t["count"], "weight": t["weight"], "fees_sats": t["fees_sats"]}
            for t in snapshot["templates"]
        ],
        "diesel": project_diesel(snapshot, scanner, previous_block),
        "amm": project_amm(snapshot, scanner, pool_state, token_meta),
        "frbtc": project_frbtc(snapshot, scanner),
        "census": intent_census(snapshot, scanner),
        "flow": flow or {"pairs": [], "total_swaps": 0, "blocks_scanned": 0},
    }
