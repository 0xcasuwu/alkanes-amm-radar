"""
radar/chain.py — confirmed-block actuals, i.e. the scoreboard.

WHY A PREDICTION TOOL MUST READ THE PAST
    A projection nobody scores is just a confident-looking number. Every
    quantity this radar predicts for block N+1 becomes an OBSERVABLE fact once
    N+1 is mined, so we recompute it from the confirmed block and keep the
    pair. That turns "the dashboard says 0.0006 DIESEL/mint" into "the
    dashboard has been within X% on the last K blocks", which is the only form
    of the claim worth acting on.

HOW ACTUALS ARE DERIVED (no indexer required)
    getblock(hash, verbosity=2) returns every tx with scriptPubKey hex, so we
    replay the SAME decode the consensus precompile does:
      mints  : count txs with a [2,0] opcode-77 protostone, once per tx
               (src/vm/host_functions.rs _get_number_diesel_mints `break`s
               after the first match - we mirror that exactly)
      fee    : sum of the COINBASE transaction's output values, in sats
               (_get_total_miner_fee) - subsidy + fees, not fees alone
    From those two the payout formula is fully determined, so the "actual"
    per-mint value here is computed the way the contract computes it rather
    than scraped from anywhere.

COST
    A mainnet block at verbosity 2 is ~10 MB and takes a few seconds. We cache
    by height and only ever fetch a height once.
"""

from . import diesel
from .protostone import decode_script


def _is_diesel_mint_script(spk_hex):
    if not spk_hex.startswith("6a5d"):
        return False
    try:
        d = decode_script(spk_hex)
    except Exception:
        return False
    if not d.get("runestone") or d.get("cenotaph"):
        return False
    for ps in d.get("protostones", []):
        cp = ps.get("cellpack")
        if not cp:
            continue
        tgt = cp.get("target") or {}
        inputs = cp.get("inputs") or []
        if tgt.get("block") == 2 and tgt.get("tx") == 0 and inputs and inputs[0] == 77:
            return True
    return False


def block_actuals(rpc, height, cache=None):
    """Recompute a confirmed block's DIESEL economics from its raw contents."""
    if cache is not None and height in cache:
        return cache[height]

    block = rpc.block(rpc.block_hash(height), 2, timeout=180)
    txs = block.get("tx") or []

    mints = 0
    swaps = 0
    for tx in txs:
        counted = False
        for o in tx.get("vout", []):
            spk = (o.get("scriptPubKey") or {}).get("hex", "")
            if not spk.startswith("6a5d"):
                continue
            if not counted and _is_diesel_mint_script(spk):
                mints += 1
                counted = True
            try:
                d = decode_script(spk)
            except Exception:
                break
            if d.get("runestone") and not d.get("cenotaph"):
                for ps in d.get("protostones", []):
                    cp = ps.get("cellpack") or {}
                    tgt = cp.get("target") or {}
                    ins = cp.get("inputs") or []
                    if tgt.get("block") == 4 and tgt.get("tx") == 65522 and ins and ins[0] == 13:
                        swaps += 1
            break  # alkanes uses the FIRST runestone output only

    # total_miner_fee == sum of coinbase outputs, in sats.
    coinbase = txs[0] if txs else {}
    total_miner_fee = 0
    for o in coinbase.get("vout", []):
        # getblock reports BTC; convert to sats the same way the node does.
        total_miner_fee += int(round(float(o.get("value", 0)) * 1e8))

    reward = diesel.block_reward(height)
    fee = diesel.diesel_fee_for(reward, total_miner_fee)
    per_mint = diesel.value_per_mint(height, mints, total_miner_fee) if mints else 0

    out = {
        "height": height,
        "hash": block.get("hash"),
        "tx_count": len(txs),
        "weight": block.get("weight"),
        "mints": mints,
        "amm_swaps": swaps,
        "total_miner_fee": total_miner_fee,
        "implied_fees_sats": max(0, total_miner_fee - reward),
        "block_reward": reward,
        "diesel_fee": fee,
        "value_per_mint": per_mint,
        "value_per_mint_display": per_mint / (10**diesel.DIESEL_DECIMALS),
    }
    if cache is not None:
        cache[height] = out
    return out


def score(prediction, actual):
    """Compare a stored prediction against the mined block. Percent errors."""
    if not prediction or not actual:
        return None

    def pct(p, a):
        if not a:
            return None
        return (p - a) / a * 100.0

    return {
        "height": actual["height"],
        "mints_predicted": prediction.get("mints_estimated"),
        "mints_actual": actual["mints"],
        "mints_error_pct": pct(prediction.get("mints_estimated") or 0, actual["mints"]),
        "per_mint_predicted": (prediction.get("projection") or {}).get("value_per_mint"),
        "per_mint_actual": actual["value_per_mint"],
        "per_mint_error_pct": pct(
            (prediction.get("projection") or {}).get("value_per_mint") or 0,
            actual["value_per_mint"],
        ),
    }
