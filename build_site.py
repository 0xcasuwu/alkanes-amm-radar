"""
build_site.py — render the static GitHub Pages snapshot.

WHAT THIS PRODUCES
    docs/index.html            the dashboard (a copy of radar/dash.html)
    docs/state.json            the snapshot the page fetches
    docs/data/flow.jsonl       the accumulating confirmed-swap window
    docs/data/predictions.json open predictions awaiting a mined block
    docs/data/history.jsonl    scored predicted-vs-mined records

WHY A SNAPSHOT AND NOT A SERVER
    GitHub Pages serves static files only - it cannot run the sweep loop. So a
    scheduled workflow runs this script, commits the output, and Pages serves
    it. The page's staleness banner is driven off `generated_at`, so a snapshot
    older than ~15 minutes says so plainly. That honesty matters more here than
    on localhost: a mempool projection whose block was mined 40 minutes ago is
    describing the past, and the page must not imply otherwise.

WHY THE SCOREBOARD STILL WORKS WITHOUT A DAEMON
    Each run writes its next-block prediction into predictions.json. A LATER
    run notices that height is now mined, recomputes the block's actuals from
    its raw contents, scores the pair, and appends it to history.jsonl. The
    accumulating record therefore survives the process being torn down between
    runs - which is the whole difficulty of scoring predictions from cron.

STATE LIVES IN THE REPO ON PURPOSE
    Committing docs/data means the window and the scoreboard grow across runs
    instead of restarting cold every time. Same pattern as the operator's
    btc-cycle-desk daily rebuild.
"""

import argparse
import json
import os
import shutil
import time

from radar import projection
from radar.chain import block_actuals, score
from radar.flowtracker import FlowTracker
from radar.pools import TokenMeta, fetch_pools
from radar.rpc import Rpc
from radar.scanner import MempoolScanner

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _append_jsonl(path, rec):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def _tail_jsonl(path, n):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out[-n:]


def main():
    ap = argparse.ArgumentParser(description="build the static radar snapshot")
    ap.add_argument("--out", default=os.path.join(HERE, "docs"))
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--max-fetch", type=int, default=6500)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--backfill", type=int, default=60, help="cold-start block window")
    ap.add_argument("--window", type=int, default=144, help="blocks shown in confirmed flow")
    args = ap.parse_args()

    out = args.out
    data = os.path.join(out, "data")
    os.makedirs(data, exist_ok=True)

    rpc = Rpc()
    tokens = TokenMeta(rpc, os.path.join(data, "tokens.json"))

    # ---- confirmed AMM flow (incremental, persisted in the repo) ----
    flow = FlowTracker(rpc, os.path.join(data, "flow.jsonl"), backfill=args.backfill)
    tip = rpc.block_count()
    scanned = flow.update(tip)
    print(f"[build] tip={tip} flow blocks scanned this run={scanned} "
          f"window={flow.scanned_to} swaps={len(flow.swaps)}")

    # ---- score any predictions whose block is now mined ----
    pred_path = os.path.join(data, "predictions.json")
    hist_path = os.path.join(data, "history.jsonl")
    preds = _load_json(pred_path, {})
    for height_str in sorted(list(preds.keys())):
        height = int(height_str)
        if height > tip:
            continue
        try:
            actual = block_actuals(rpc, height)
        except Exception as e:
            print(f"[build] actuals {height} failed: {e}")
            continue
        _append_jsonl(hist_path, {"actual": actual, "score": score(preds[height_str], actual)})
        preds.pop(height_str, None)
        print(f"[build] scored {height}: actual mints={actual['mints']}")

    # ---- mempool sweep ----
    pool_state = None
    try:
        pool_state = fetch_pools(lambda u: Rpc(u))
        print(f"[build] pools={pool_state['count']}")
    except Exception as e:
        print(f"[build] pool fetch failed: {e}")
        pool_state = {"pools": {}, "fetched_at": time.time(), "count": 0}

    scanner = MempoolScanner(
        rpc, horizon_blocks=args.horizon, max_fetch_per_sweep=args.max_fetch, workers=args.workers
    )
    snap = scanner.sweep()
    print(f"[build] sweep coverage={snap['coverage']:.3f} "
          f"candidates={snap['candidate_count']} in {snap['elapsed']:.1f}s")

    history = _tail_jsonl(hist_path, 12)
    previous = history[-1]["actual"] if history else None

    proj = projection.build(
        snap, scanner, pool_state, tokens, previous, flow=flow.view(tokens, args.window)
    )
    proj["history"] = list(reversed(history))

    scored = [h["score"] for h in history if h.get("score")]
    scored = [s for s in scored if s and s.get("mints_error_pct") is not None]
    if scored:
        errs = [abs(s["mints_error_pct"]) for s in scored]
        proj["accuracy"] = {
            "samples": len(scored),
            "mean_mint_error_pct": sum(errs) / len(errs),
            "median_mint_error_pct": sorted(errs)[len(errs) // 2],
        }
    else:
        proj["accuracy"] = None

    # Record this run's prediction so a future run can score it.
    if proj.get("diesel"):
        preds[str(proj["next_height"])] = proj["diesel"]
    # Never let the open-prediction set grow without bound.
    preds = {k: v for k, v in preds.items() if int(k) > tip - 200}
    with open(pred_path, "w") as f:
        json.dump(preds, f, default=str)

    proj.pop("mempool", None)  # 21 MB of raw mempool must never reach the page
    with open(os.path.join(out, "state.json"), "w") as f:
        json.dump(proj, f, default=str)
    shutil.copyfile(os.path.join(HERE, "radar", "dash.html"), os.path.join(out, "index.html"))
    # Pages would otherwise run the output through Jekyll and drop _-prefixed paths.
    open(os.path.join(out, ".nojekyll"), "w").close()

    size = os.path.getsize(os.path.join(out, "state.json"))
    print(f"[build] wrote {out}/state.json ({size/1024:.0f} KB), index.html, data/")


if __name__ == "__main__":
    main()
