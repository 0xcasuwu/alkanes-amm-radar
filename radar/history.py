"""
radar/history.py — confirmed AMM swap flow from mined blocks.

WHY THIS PANEL EXISTS
    Pending AMM swaps are SPARSE on mainnet: measured ~0.6 per block across
    963889-963893, and a mempool sample can easily contain zero. A dashboard
    about "price action from AMM purchases" that only reads the mempool would
    be blank most of the time. So we also read what already happened: every
    confirmed swap over a rolling window of blocks, aggregated per pool into
    directional flow.

    Pending = what is about to hit. Confirmed = what already hit. Together they
    are the full picture of AMM-driven price action.

WHAT WE DELIBERATELY DO NOT CLAIM: REALIZED PRICE IMPACT
    Computing a confirmed swap's true impact needs the pool reserves AS THEY
    WERE at that height. We probed whether that is reachable and it is NOT:
    `alkanes_simulate` accepts a `height` parameter but IGNORES it - pool
    opcode 999/97 returned byte-identical reserves at height 963900 and 963000
    (verified 2026-08-24). There is no historical-state read on the public
    gateway.

    Reconstructing past reserves by inverting swaps backward from current state
    is algebraically possible but breaks silently on any add/remove-liquidity
    in the window, so we do not do it. Instead we report what IS byte-provable:
    swap COUNT, DIRECTION and AMOUNT-IN per pool. Volume is a fact; a
    fabricated historical price is not.

INCREMENTAL BY DESIGN
    A verbosity-2 mainnet block is ~10 MB and takes seconds to pull. Scanning a
    long window every run would be wasteful and slow, so callers persist the
    result and we only ever scan blocks newer than what they already have.
"""

from .classify import AMM_FACTORY_ID, AMM_SWAP_OP, classify
from .protostone import decode_script


def swaps_in_block(rpc, height):
    """Return every decoded AMM swap in one confirmed block."""
    block = rpc.block(rpc.block_hash(height), 2, timeout=180)
    out = []
    for tx in block.get("tx") or []:
        for o in tx.get("vout", []):
            spk = (o.get("scriptPubKey") or {}).get("hex", "")
            if not spk.startswith("6a5d"):
                continue
            try:
                d = decode_script(spk)
            except Exception:
                break
            if not d.get("runestone") or d.get("cenotaph"):
                break
            for ps in d.get("protostones", []):
                cp = ps.get("cellpack") or {}
                tgt = cp.get("target") or {}
                ins = list(cp.get("inputs") or [])
                if not ins:
                    continue
                target = f"{tgt.get('block')}:{tgt.get('tx')}"
                if target != AMM_FACTORY_ID or ins[0] != AMM_SWAP_OP:
                    continue
                rec = classify(target, ins[0], ins[1:])
                if rec.get("intent") != "amm_swap":
                    continue
                out.append(
                    {
                        "height": height,
                        "txid": tx.get("txid"),
                        "path": rec["path"],
                        "amount_in": rec["amount_in"],
                        "min_out": rec["min_out"],
                        "deadline": rec["deadline"],
                    }
                )
            break  # alkanes uses the FIRST runestone output only
    return out


def scan_range(rpc, start_height, end_height, on_block=None):
    """Scan [start_height, end_height] inclusive. Returns a flat swap list."""
    found = []
    for h in range(int(start_height), int(end_height) + 1):
        try:
            got = swaps_in_block(rpc, h)
        except Exception:
            continue  # one unreadable block must not abort a backfill
        found.extend(got)
        if on_block:
            on_block(h, len(got))
    return found


def aggregate_flow(swaps, token_meta=None):
    """Aggregate swaps into per-pair directional flow.

    A swap is attributed to each CONSECUTIVE PAIR on its path, because a
    multi-hop route moves every pool it traverses - not just the endpoints.
    `amount_in` is exact only for the FIRST hop; downstream hops carry an
    amount we cannot know without simulating against historical reserves we do
    not have. So per-pair we count hops and sum first-hop volume only, and we
    say so in the field names.
    """
    pairs = {}
    for s in swaps:
        path = s["path"]
        for idx, (a, b) in enumerate(zip(path, path[1:])):
            key = "|".join(sorted((a, b)))
            e = pairs.setdefault(
                key,
                {
                    "pair": sorted((a, b)),
                    "swaps": 0,
                    "first_hop_volume_in": 0,
                    "first_hop_swaps": 0,
                    "sold": {},
                    "heights": [],
                },
            )
            e["swaps"] += 1
            e["sold"][a] = e["sold"].get(a, 0) + (s["amount_in"] if idx == 0 else 0)
            if idx == 0:
                e["first_hop_volume_in"] += s["amount_in"]
                e["first_hop_swaps"] += 1
            e["heights"].append(s["height"])

    out = []
    for key, e in pairs.items():
        a, b = e["pair"]
        sym = (lambda t: token_meta.symbol(t) if token_meta else t)
        # Net direction: which side of the pair was being SOLD INTO the pool.
        sold_a = e["sold"].get(a, 0)
        sold_b = e["sold"].get(b, 0)
        out.append(
            {
                "pair": e["pair"],
                "symbol_a": sym(a),
                "symbol_b": sym(b),
                "swaps": e["swaps"],
                "first_hop_swaps": e["first_hop_swaps"],
                "first_hop_volume_in": e["first_hop_volume_in"],
                "sold_a": sold_a,
                "sold_b": sold_b,
                "dominant_sell": a if sold_a >= sold_b else b,
                "dominant_sell_symbol": sym(a) if sold_a >= sold_b else sym(b),
                "last_height": max(e["heights"]) if e["heights"] else None,
            }
        )
    out.sort(key=lambda x: -x["swaps"])
    return out
