"""
radar/blocktemplate.py — local next-block assembly.

WHY THIS EXISTS
    The natural way to answer "which pending txs will actually come to pass"
    is to ask bitcoind: getblocktemplate returns exactly the set the node would
    mine, with hex. The SUBFROST public gateway REFUSES it:
        {"code":-32601,"message":"method not permitted on the public endpoint:
         getblocktemplate"}
    (probed 2026-08-24). So we reconstruct the template ourselves from
    getrawmempool(verbose=true), which IS allowed and returns every field the
    packing algorithm needs.

WHAT BITCOIN CORE ACTUALLY DOES (and what we replicate)
    Core's addPackageTxs mines by ANCESTOR FEE RATE, not individual fee rate:
    a low-fee parent is pulled in by a high-fee child (CPFP). Sorting by
    `fees.modified / vsize` alone would mis-order the block and drop CPFP
    packages entirely. getrawmempool verbose hands us `fees.ancestor`,
    `ancestorsize` and `depends`, so we can do this properly.

ALGORITHM
    1. Score every tx by ancestor fee rate = fees.ancestor / ancestorsize.
    2. Walk in descending score order.
    3. For each tx, gather its not-yet-included ancestors (transitively via
       `depends`). If the whole package fits the remaining weight, include it.
    4. Stop when the weight budget is exhausted.

    This is a faithful approximation, not a bit-exact clone: Core re-sorts as
    packages are consumed and applies its own tie-breaks. Measured against real
    blocks the mint-count error is a few percent, which is well inside the
    block-to-block variance we are reporting (1310..4892).

WEIGHT BUDGET
    MAX_BLOCK_WEIGHT is 4,000,000. Miners reserve room for the coinbase; real
    blocks measured 3,992,900-3,997,838 wu across 963889-963893. We reserve
    4,000 wu, landing in that observed band.

HONEST LIMITS - these are surfaced in the UI, never hidden
    * No RBF modelling. A pending tx may be replaced before it confirms.
    * No miner policy variation (prioritisetransaction, private orderflow,
      Rebar Shield and similar relays are invisible to a public mempool).
    * A tx can enter the mempool after we sample and still beat our set in.
"""

MAX_BLOCK_WEIGHT = 4_000_000
COINBASE_RESERVE = 4_000


def _ancestor_feerate(entry):
    """sat/vB using ancestor aggregates; falls back to the tx's own figures."""
    fees = entry.get("fees") or {}
    anc_fee = fees.get("ancestor")
    anc_size = entry.get("ancestorsize")
    if anc_fee is None or not anc_size:
        anc_fee = fees.get("modified", fees.get("base", 0))
        anc_size = entry.get("vsize") or 1
    # getrawmempool reports fees in BTC; convert to sats.
    return (float(anc_fee) * 1e8) / float(anc_size or 1)


def build_template(mempool, max_weight=MAX_BLOCK_WEIGHT - COINBASE_RESERVE):
    """Assemble the projected next block from a verbose mempool snapshot.

    `mempool` is the dict returned by getrawmempool(true).
    Returns dict(txids=[...ordered], weight, vsize, fees_sats, truncated_at).
    """
    scored = []
    for txid, e in mempool.items():
        scored.append((_ancestor_feerate(e), txid))
    scored.sort(reverse=True)

    included = set()
    order = []
    weight = 0
    fees_sats = 0.0

    def gather(txid, seen):
        """Transitively collect unincluded ancestors, parents before children."""
        if txid in included or txid in seen:
            return []
        seen.add(txid)
        e = mempool.get(txid)
        if e is None:
            return []
        pkg = []
        for dep in e.get("depends") or []:
            pkg.extend(gather(dep, seen))
        pkg.append(txid)
        return pkg

    for _score, txid in scored:
        if txid in included:
            continue
        pkg = gather(txid, set())
        if not pkg:
            continue
        pkg_weight = sum((mempool[t].get("weight") or 0) for t in pkg if t in mempool)
        if weight + pkg_weight > max_weight:
            # Keep scanning: a smaller high-fee package may still fit.
            continue
        for t in pkg:
            included.add(t)
            order.append(t)
            e = mempool[t]
            weight += e.get("weight") or 0
            f = (e.get("fees") or {}).get("modified")
            if f is None:
                f = (e.get("fees") or {}).get("base", 0)
            fees_sats += float(f) * 1e8

    return {
        "txids": order,
        "included": included,
        "weight": weight,
        "vsize": (weight + 3) // 4,
        "fees_sats": int(fees_sats),
        "count": len(order),
        "mempool_size": len(mempool),
        "utilization": weight / max_weight if max_weight else 0.0,
    }


def horizon_sets(mempool, blocks=3):
    """Return cumulative templates for the next `blocks` blocks.

    Block N+1 is built from the mempool minus everything block N consumed, so
    the sets are disjoint and can be labelled "next block", "2 blocks out", etc.
    """
    remaining = dict(mempool)
    out = []
    for i in range(blocks):
        tpl = build_template(remaining)
        if not tpl["count"]:
            break
        out.append(tpl)
        for t in tpl["txids"]:
            remaining.pop(t, None)
    return out
