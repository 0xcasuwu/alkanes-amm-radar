"""
radar/pools.py — AMM pool reserves and token metadata.

TWO SOURCES, DELIBERATELY RANKED
    1. api.alkanode.com/rpc  ->  ammdata.get_pools
       The Espo tertiary indexer's precomputed pool table. One call returns
       every pool with base/quote ids and reserves. This is the authoritative
       MAINNET pool source (~/.claude/subfrost-brain/services/api-alkanode.md).
    2. alkanes_simulate on the pool alkane (opcode 999 GetReserves)
       Per-pool, slower, but reads straight from the indexer. Used to
       spot-check a single pool when a projection looks wrong.

    ammdata.get_pools ALWAYS RETURNS MAINNET DATA regardless of the caller's
    network, because token ids (2:0, 32:0) are identical across networks. On
    regtest that silently yields ~191x inflated quotes. This radar is a
    mainnet tool, so that is correct here - but never reuse this module on
    regtest without swapping to source 2.
    (Reference: incidents/2026-02-01-btc-diesel-swap-three-bugs.)

RESERVE FRESHNESS IS THE ACCURACY FLOOR
    Every projection is "current reserves + pending swaps". If reserves are
    stale by a block, the projection inherits that staleness. We stamp
    `fetched_at` and the dashboard shows the age; a projection is never
    presented as fresher than its inputs.

TOKEN METADATA
    Standard alkanes token opcodes (alkanes-std-genesis-alkane-upgraded-eoa/
    src/lib.rs:43-53): 99 = GetName, 100 = GetSymbol, 101 = GetTotalSupply.
    Note there is NO decimals opcode on the token standard - 8 is the
    ecosystem convention (DIESEL and frBTC both use it) and is what we assume
    for DISPLAY ONLY. All arithmetic stays in integer base units, so a wrong
    decimals guess can mis-label a chart but can never corrupt a projection.
"""

import json
import os
import time

ALKANODE_URL = os.environ.get("RADAR_ALKANODE_URL", "https://api.alkanode.com/rpc")
DEFAULT_DECIMALS = 8

# Display metadata for ids we know by heart; anything else is resolved on demand.
SEED_TOKENS = {
    "2:0": {"symbol": "DIESEL", "decimals": 8},
    "32:0": {"symbol": "frBTC", "decimals": 8},
}


def fetch_pools(rpc_factory):
    """Fetch the Espo pool table. `rpc_factory` builds an Rpc for ALKANODE_URL."""
    rpc = rpc_factory(ALKANODE_URL)
    result = rpc.call("ammdata.get_pools", [], timeout=45)
    pools = (result or {}).get("pools", {}) or {}
    index = {}
    for pool_id, p in pools.items():
        base, quote = p.get("base"), p.get("quote")
        if not base or not quote:
            continue
        try:
            br = int(p.get("base_reserve") or 0)
            qr = int(p.get("quote_reserve") or 0)
        except (TypeError, ValueError):
            continue
        if br <= 0 or qr <= 0:
            continue  # an empty pool cannot price a swap; skip rather than div-by-zero
        index[frozenset((base, quote))] = {
            "pool_id": pool_id,
            "tokens": [base, quote],
            "reserves": {base: br, quote: qr},
            "source": p.get("source"),
        }
    return {"pools": index, "fetched_at": time.time(), "count": len(index)}


def clone_reserves(pool_index):
    """Deep-copy just the mutable reserve numbers for a projection run.

    The projection mutates reserves as it applies pending swaps; the live
    snapshot must stay pristine so the "current" column keeps meaning
    "current".
    """
    return {
        k: {**v, "reserves": dict(v["reserves"])}
        for k, v in pool_index.items()
    }


class TokenMeta:
    """Lazy symbol/decimals resolver with an on-disk cache."""

    def __init__(self, rpc, cache_path=None):
        self.rpc = rpc
        self.cache_path = cache_path
        self.meta = dict(SEED_TOKENS)
        if cache_path and os.path.exists(cache_path):
            try:
                self.meta.update(json.load(open(cache_path)))
            except Exception:
                pass

    def _simulate(self, token_id, opcode):
        block, tx = token_id.split(":")
        params = [
            {
                "target": {"block": block, "tx": tx},
                "inputs": [str(opcode)],
                "alkanes": [],
                "transaction": "0x",
                "block": "0x",
                "height": "20000",
                "txindex": 0,
                "pointer": 0,
                "refundPointer": 0,
                "vout": 0,
            }
        ]
        res = self.rpc.call("alkanes_simulate", params, timeout=20)
        data = ((res or {}).get("execution") or {}).get("data") or ""
        if data.startswith("0x"):
            data = data[2:]
        return data

    def symbol(self, token_id):
        """Resolve a token's ticker; falls back to the raw id on any failure."""
        cached = self.meta.get(token_id)
        if cached and cached.get("symbol"):
            return cached["symbol"]
        sym = token_id
        try:
            raw = self._simulate(token_id, 100)
            if raw:
                decoded = bytes.fromhex(raw).decode("utf-8", "ignore").strip("\x00").strip()
                if decoded:
                    sym = decoded
        except Exception:
            pass  # an unresolvable ticker is cosmetic; never fail a projection for it
        self.meta[token_id] = {"symbol": sym, "decimals": DEFAULT_DECIMALS}
        self.save()
        return sym

    def decimals(self, token_id):
        return (self.meta.get(token_id) or {}).get("decimals", DEFAULT_DECIMALS)

    def save(self):
        if not self.cache_path:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            json.dump(self.meta, open(self.cache_path, "w"))
        except Exception:
            pass


def human(amount, decimals=DEFAULT_DECIMALS):
    """Base units -> display float. DISPLAY ONLY - never feed back into math."""
    try:
        return int(amount) / (10 ** int(decimals))
    except Exception:
        return 0.0
