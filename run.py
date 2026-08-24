"""
run.py — entrypoint for the alkanes mempool radar.

USAGE
    python3 run.py                       # dashboard on http://127.0.0.1:8899
    python3 run.py --once                # single sweep, print JSON, exit
    python3 run.py --port 9000 --horizon 1 --interval 15

TUNING NOTES (measured 2026-08-24 on mainnet)
    --horizon N     how many projected blocks to scan. 1 is cheapest and is
                    the strict reading of "the next block". 3 gives a wider
                    view of what is queued behind it. Cost scales roughly
                    linearly.
    --max-fetch     per-sweep fetch budget. The first sweep on a cold cache
                    needs to cover the whole projected block (~5-6k txs at
                    ~143 tx/s, so ~40 s). Later sweeps only pick up new
                    arrivals and are far cheaper.
    --interval      seconds between sweeps. Below ~15 s you are mostly
                    re-reading an unchanged mempool.
"""

import argparse
import json

from radar.server import Radar, serve


def main():
    ap = argparse.ArgumentParser(description="alkanes mempool radar")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--horizon", type=int, default=2, help="projected blocks to scan")
    ap.add_argument("--interval", type=int, default=20, help="seconds between sweeps")
    ap.add_argument("--max-fetch", type=int, default=6000, help="tx fetches per sweep")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--rpc-url", default=None)
    ap.add_argument("--once", action="store_true", help="one sweep, print JSON, exit")
    args = ap.parse_args()

    radar = Radar(
        rpc_url=args.rpc_url,
        horizon_blocks=args.horizon,
        sweep_interval=args.interval,
        max_fetch=args.max_fetch,
        workers=args.workers,
    )

    if args.once:
        proj = radar.cycle()
        proj.pop("history", None)
        print(json.dumps(proj, indent=2, default=str))
        return

    serve(radar, port=args.port)


if __name__ == "__main__":
    main()
