"""
radar/flowtracker.py — keeps the confirmed-AMM-swap window current.

RESPONSIBILITY
    Own the "which blocks have I already scanned" watermark, scan forward from
    it, persist, and hand back an aggregated view. Both the live daemon and the
    static site builder use this identically, so the two deployments cannot
    drift apart in what they report.

BACKFILL POLICY
    On a cold start there is no watermark, so we scan `backfill` blocks back
    from the tip. That is the only expensive run. Every subsequent run scans
    the handful of blocks mined since - cheap enough for a 20-minute cron.

    A large gap (the job was off for hours) is CLAMPED to `max_catchup` blocks
    so one delayed run cannot turn into a 200-block, multi-hundred-megabyte
    scan that times out the job. When clamped we say so in the result rather
    than silently leaving a hole in the window.
"""

from .history import aggregate_flow, scan_range
from .store import load_flow, save_flow


class FlowTracker:
    def __init__(self, rpc, path, backfill=60, max_catchup=40, keep_blocks=500):
        self.rpc = rpc
        self.path = path
        self.backfill = backfill
        self.max_catchup = max_catchup
        self.keep_blocks = keep_blocks
        self.swaps, self.scanned_to, self.scanned_from = load_flow(path)
        self.clamped = False

    def update(self, tip):
        """Scan any blocks newer than the watermark. Returns blocks scanned."""
        cold_start = self.scanned_to is None
        if cold_start:
            start = max(1, tip - self.backfill + 1)
        else:
            start = self.scanned_to + 1
        if start > tip:
            return 0
        # The catch-up clamp guards against an unbounded GAP (the job was off
        # for hours). A cold-start backfill is a DELIBERATE window, so it is
        # exempt - otherwise asking for 60 blocks silently yields 40.
        if not cold_start and tip - start + 1 > self.max_catchup:
            start = tip - self.max_catchup + 1
            self.clamped = True
        found = scan_range(self.rpc, start, tip)
        self.swaps.extend(found)
        self.scanned_to = tip
        if self.scanned_from is None or start < self.scanned_from:
            self.scanned_from = start
        self.swaps = (
            save_flow(self.path, self.swaps, tip, self.keep_blocks, self.scanned_from) or []
        )
        if self.scanned_from is not None:
            self.scanned_from = max(self.scanned_from, tip - self.keep_blocks + 1)
        return tip - start + 1

    def view(self, token_meta=None, window_blocks=None):
        """Aggregate the window into the payload the dashboard renders."""
        swaps = self.swaps
        if window_blocks and swaps:
            newest = max(s["height"] for s in swaps)
            swaps = [s for s in swaps if s["height"] > newest - window_blocks]
        # The window is what we SCANNED, never where swaps happened to fall.
        to_h = self.scanned_to
        from_h = self.scanned_from
        if to_h is not None and window_blocks:
            floor = to_h - window_blocks + 1
            from_h = max(from_h, floor) if from_h is not None else floor
        return {
            "pairs": aggregate_flow(swaps, token_meta),
            "total_swaps": len(swaps),
            "from_height": from_h,
            "to_height": to_h,
            "blocks_scanned": (to_h - from_h + 1) if (from_h and to_h) else 0,
            "clamped": self.clamped,
            "recent": sorted(swaps, key=lambda s: -s["height"])[:20],
        }
