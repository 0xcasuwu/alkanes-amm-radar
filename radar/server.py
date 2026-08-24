"""
radar/server.py — the radar daemon: sweep loop + JSON API + dashboard host.

SHAPE
    One background thread runs sweeps continuously and publishes an immutable
    projection dict. HTTP handlers only ever read that dict, so a slow sweep
    never blocks the dashboard and the page always renders the last good state
    instead of a spinner.

THE SCORING LOOP (why the tip transition is special)
    When the tip advances, the block we were predicting just became fact:
      1. Recompute the mined block's actuals from its raw contents
         (radar/chain.py - no indexer needed).
      2. Score the prediction we stored for that exact height.
      3. Keep the pair in a bounded history the dashboard renders as a
         running accuracy record.
    This is what stops the tool from being an unfalsifiable number generator.

POOL REFRESH CADENCE
    Reserves come from the Espo table, which is a precomputed aggregate. We
    refresh on a timer (default 60 s) rather than every sweep - hammering it
    would be rude and reserves only move when a swap confirms. The projection
    reports reserve age so a stale read is visible rather than silently
    assumed fresh.

FAILURE POSTURE
    A sweep that throws is logged into the state and retried next interval;
    the daemon never dies on a transient RPC error. Losing the mempool for one
    cycle is normal operation, not an outage.
"""

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import projection
from .chain import block_actuals, score
from .flowtracker import FlowTracker
from .pools import TokenMeta, fetch_pools
from .rpc import Rpc
from .scanner import MempoolScanner

HERE = os.path.dirname(os.path.abspath(__file__))


class Radar:
    def __init__(
        self,
        rpc_url=None,
        horizon_blocks=3,
        sweep_interval=20,
        pool_refresh=60,
        max_fetch=6000,
        workers=24,
        state_dir=None,
    ):
        self.rpc = Rpc(rpc_url) if rpc_url else Rpc()
        self.scanner = MempoolScanner(
            self.rpc, horizon_blocks=horizon_blocks, max_fetch_per_sweep=max_fetch, workers=workers
        )
        self.state_dir = state_dir or os.path.join(HERE, "..", "state")
        self.tokens = TokenMeta(self.rpc, os.path.join(self.state_dir, "tokens.json"))
        # Confirmed-swap window: the substance panel when nothing is pending.
        self.flow = FlowTracker(self.rpc, os.path.join(self.state_dir, "flow.jsonl"))
        self.sweep_interval = sweep_interval
        self.pool_refresh = pool_refresh

        self.pool_state = None
        self.pools_fetched = 0
        self.projection = None
        self.error = None

        self.predictions = {}       # height -> diesel projection we published
        self.block_cache = {}       # height -> actuals
        self.history = []           # scored (prediction, actual) pairs
        self.last_tip = None
        self.started = time.time()
        self._lock = threading.Lock()

    # ---- data plane ----

    def refresh_pools(self, force=False):
        if not force and self.pool_state and (time.time() - self.pools_fetched) < self.pool_refresh:
            return
        try:
            self.pool_state = fetch_pools(lambda u: Rpc(u))
            self.pools_fetched = time.time()
        except Exception as e:  # pool data is optional; DIESEL projection stands without it
            if self.pool_state is None:
                self.pool_state = {"pools": {}, "fetched_at": time.time(), "count": 0}
            self.error = f"pool refresh: {e}"

    def on_new_tip(self, tip):
        """Score the prediction for the block that just got mined."""
        if self.last_tip is None:
            self.last_tip = tip
            return
        if tip == self.last_tip:
            return
        for height in range(self.last_tip + 1, tip + 1):
            try:
                actual = block_actuals(self.rpc, height, self.block_cache)
            except Exception as e:
                self.error = f"actuals {height}: {e}"
                continue
            pred = self.predictions.pop(height, None)
            entry = {"actual": actual, "score": score(pred, actual) if pred else None}
            self.history.append(entry)
            self.history = self.history[-40:]
        self.last_tip = tip

    def cycle(self):
        self.refresh_pools()
        snap = self.scanner.sweep()
        self.on_new_tip(snap["tip"])
        try:
            self.flow.update(snap["tip"])
        except Exception as e:  # confirmed-flow is additive; never sink a sweep
            self.error = f"flow update: {e}"
        proj = projection.build(
            snap,
            self.scanner,
            self.pool_state,
            self.tokens,
            self._previous_block(),
            flow=self.flow.view(self.tokens),
        )
        proj["history"] = [
            {"actual": h["actual"], "score": h["score"]} for h in reversed(self.history[-12:])
        ]
        proj["accuracy"] = self._accuracy()
        proj["rpc_stats"] = dict(self.rpc.stats)
        proj["uptime_sec"] = time.time() - self.started
        proj["error"] = self.error
        if proj["diesel"]:
            self.predictions[proj["next_height"]] = proj["diesel"]
        with self._lock:
            self.projection = proj
        return proj

    def _previous_block(self):
        for h in reversed(self.history):
            if h.get("actual"):
                return h["actual"]
        return None

    def _accuracy(self):
        scored = [h["score"] for h in self.history if h.get("score")]
        scored = [s for s in scored if s and s.get("mints_error_pct") is not None]
        if not scored:
            return None
        errs = [abs(s["mints_error_pct"]) for s in scored]
        per_mint = [
            abs(s["per_mint_error_pct"]) for s in scored if s.get("per_mint_error_pct") is not None
        ]
        return {
            "samples": len(scored),
            "mean_mint_error_pct": sum(errs) / len(errs),
            "median_mint_error_pct": sorted(errs)[len(errs) // 2],
            "mean_per_mint_error_pct": (sum(per_mint) / len(per_mint)) if per_mint else None,
        }

    def loop(self):
        while True:
            t0 = time.time()
            try:
                self.cycle()
                self.error = None
            except Exception as e:
                self.error = f"{e}\n{traceback.format_exc(limit=3)}"
            time.sleep(max(1.0, self.sweep_interval - (time.time() - t0)))


def make_handler(radar):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # keep the console readable; the sweep loop is the real log

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "dash.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                with radar._lock:
                    proj = radar.projection
                if proj is None:
                    body = json.dumps({"status": "warming", "uptime": time.time() - radar.started})
                else:
                    body = json.dumps(proj, default=str)
                return self._send(200, body.encode(), "application/json")
            if path == "/api/health":
                return self._send(
                    200,
                    json.dumps({"ok": radar.projection is not None, "error": radar.error}).encode(),
                    "application/json",
                )
            return self._send(404, b"not found", "text/plain")

    return Handler


def serve(radar, port=8899):
    t = threading.Thread(target=radar.loop, daemon=True)
    t.start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(radar))
    print(f"[radar] dashboard  http://127.0.0.1:{port}")
    print(f"[radar] json api   http://127.0.0.1:{port}/api/state")
    httpd.serve_forever()
