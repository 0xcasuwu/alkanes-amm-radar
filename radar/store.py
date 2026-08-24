"""
radar/store.py — durable state for the confirmed-swap window.

WHY A FILE AND NOT MEMORY
    The live daemon is long-lived and could hold the swap window in RAM, but
    the GitHub Pages deployment runs as a SCHEDULED JOB: a fresh process every
    time, with no memory of the last run. Re-scanning a wide block window on
    every run would cost minutes (a verbosity-2 mainnet block is ~10 MB).

    So the window lives in a JSONL file that the workflow commits back to the
    repo. Each run scans only the blocks mined since the last run - typically
    one or two - and the window accumulates. This is the same
    build-and-commit pattern the operator already runs for btc-cycle-desk.

JSONL, NOT JSON
    Append-only line-delimited records survive a partially-written file: a
    truncated last line is discarded and the rest still loads. A half-written
    JSON object would take the whole history with it.

TRIMMING
    The window is capped by BLOCK SPAN, not record count, so "last N blocks"
    stays honest whether those blocks held zero swaps or fifty.
"""

import json
import os


def load_flow(path):
    """Load persisted swaps. Returns (swaps, scanned_to, scanned_from).

    Both watermarks are persisted because the WINDOW is what the dashboard
    reports ("N blocks scanned"), and that cannot be recovered from the swap
    records themselves - a window with zero swaps in it is still a scanned
    window, and deriving the start from min(swap.height) would silently shrink
    it to wherever the first swap happened to land.
    """
    swaps = []
    scanned_to = None
    scanned_from = None
    if not path or not os.path.exists(path):
        return swaps, scanned_to, scanned_from
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a torn final line
                if rec.get("_meta"):
                    scanned_to = rec.get("scanned_to")
                    scanned_from = rec.get("scanned_from")
                    continue
                swaps.append(rec)
    except OSError:
        return [], None, None
    return swaps, scanned_to, scanned_from


def save_flow(path, swaps, scanned_to, keep_blocks=500, scanned_from=None):
    """Persist swaps plus a watermark, trimmed to the last `keep_blocks`."""
    if not path:
        return
    if scanned_to is not None:
        cutoff = scanned_to - keep_blocks
        swaps = [s for s in swaps if s["height"] > cutoff]
        if scanned_from is None or scanned_from < cutoff:
            scanned_from = cutoff + 1
    # De-duplicate: a re-scanned block must not double-count its swaps.
    seen = set()
    unique = []
    for s in sorted(swaps, key=lambda x: (x["height"], x.get("txid") or "")):
        key = (s["height"], s.get("txid"), tuple(s.get("path") or []), s.get("amount_in"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(
            json.dumps({"_meta": True, "scanned_to": scanned_to, "scanned_from": scanned_from})
            + "\n"
        )
        for s in unique:
            f.write(json.dumps(s) + "\n")
    os.replace(tmp, path)  # atomic: readers never see a half-written window
    return unique
