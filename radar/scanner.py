"""
radar/scanner.py — the mempool sweep loop.

THE COST PROBLEM THIS SOLVES
    Measured 2026-08-24: mainnet mempool held 35-41k txs with ~54 NEW tx/s
    arriving. Fetching every tx every sweep is both wasteful and pointless -
    most of the mempool is low-fee traffic that will not confirm for many
    blocks, and it cannot move a price in the horizon we care about.

    So the scanner is CANDIDATE-DRIVEN, not mempool-driven:
      candidates = union of the next `horizon_blocks` projected block templates
    That set is what "granting all pending transactions come to pass" actually
    means on any tradeable timescale, and it is ~3x smaller than the mempool.

    A per-sweep fetch budget bounds the worst case (first sweep on a cold
    cache). Anything not fetched this sweep is simply picked up next sweep -
    coverage is REPORTED, never silently assumed complete.

THE CACHE IS THE WHOLE OPTIMISATION
    Classification is a pure function of the raw tx, and a tx's bytes never
    change while it sits in the mempool. So we classify once, keyed by txid,
    and evict on confirmation/drop. Steady state costs only the new arrivals
    inside the horizon - a few hundred fetches per sweep rather than tens of
    thousands.

    Negative results are cached too. ~22% of top-of-block txs carry no
    runestone; without caching the misses we would re-fetch them forever.

COVERAGE HONESTY
    Every snapshot reports scanned / candidates / unscanned. A projection built
    from 60% coverage is labelled as such in the UI. Reporting a confident
    number off a partial scan is the failure mode this project most wants to
    avoid.
"""

import time

from .blocktemplate import horizon_sets
from .classify import classify_tx
from .protostone import decode_tx, iter_cellpacks


class MempoolScanner:
    def __init__(self, rpc, horizon_blocks=3, max_fetch_per_sweep=6000, workers=24):
        self.rpc = rpc
        self.horizon_blocks = horizon_blocks
        self.max_fetch_per_sweep = max_fetch_per_sweep
        self.workers = workers
        # txid -> list[intent] (possibly empty for "runestone-free, checked")
        self.cache = {}
        self.last_sweep = None
        self.sweeps = 0

    def _evict(self, live_txids):
        """Drop cache entries for txs that left the mempool (confirmed or replaced)."""
        stale = self.cache.keys() - live_txids
        for t in stale:
            self.cache.pop(t, None)
        return len(stale)

    def sweep(self):
        """One full pass. Returns a snapshot dict consumed by radar/projection.py."""
        t0 = time.time()
        mempool = self.rpc.raw_mempool(verbose=True)
        tip = self.rpc.block_count()

        templates = horizon_sets(mempool, blocks=self.horizon_blocks)
        candidates = []
        seen = set()
        for tpl in templates:
            for txid in tpl["txids"]:
                if txid not in seen:
                    seen.add(txid)
                    candidates.append(txid)

        evicted = self._evict(set(mempool.keys()))

        # Fetch only what we have never classified, in template order so the
        # most-likely-to-confirm txs are covered first when the budget bites.
        todo = [t for t in candidates if t not in self.cache][: self.max_fetch_per_sweep]
        fetched = 0
        if todo:
            raws = self.rpc.raw_txs(todo, workers=self.workers)
            for txid, raw in zip(todo, raws):
                if not raw:
                    continue
                fetched += 1
                decoded = decode_tx(raw, txid)
                if not decoded:
                    self.cache[txid] = []  # negative cache: no runestone
                    continue
                if decoded.get("cenotaph"):
                    # A cenotaph executes NOTHING. Record it so it can be
                    # reported, but it must never feed a projection.
                    self.cache[txid] = [{"intent": "cenotaph", "flaw": decoded.get("flaw")}]
                    continue
                self.cache[txid] = classify_tx(decoded, iter_cellpacks)

        self.sweeps += 1
        snapshot = {
            "tip": tip,
            "next_height": tip + 1,
            "mempool_size": len(mempool),
            "templates": templates,
            "candidates": candidates,
            "candidate_count": len(candidates),
            "scanned": sum(1 for t in candidates if t in self.cache),
            "unscanned": sum(1 for t in candidates if t not in self.cache),
            "fetched_this_sweep": fetched,
            "evicted": evicted,
            "cache_size": len(self.cache),
            "elapsed": time.time() - t0,
            "at": time.time(),
            "mempool": mempool,
        }
        snapshot["coverage"] = (
            snapshot["scanned"] / snapshot["candidate_count"]
            if snapshot["candidate_count"]
            else 1.0
        )
        self.last_sweep = snapshot
        return snapshot

    def intents_for(self, txids):
        """Yield (txid, intent) for every classified intent among `txids`."""
        for t in txids:
            for rec in self.cache.get(t) or []:
                yield t, rec
