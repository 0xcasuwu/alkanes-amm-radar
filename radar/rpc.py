"""
radar/rpc.py — JSON-RPC transport for the alkanes mempool radar.

PURPOSE
    One dependency-light client for the SUBFROST public gateway. Every other
    module in this package gets its bytes through here.

GROUND TRUTH (probed live 2026-08-24 against https://mainnet.subfrost.io/v4/subfrost)
    The gateway multiplexes THREE method families on one URL:
      * bitcoind passthrough : getrawmempool, getrawtransaction, getblock,
                               getblockhash, getblockcount
      * metashrew / alkanes  : metashrew_height, alkanes_simulate,
                               alkanes_protorunesbyoutpoint
      * esplora passthrough  : esplora_mempool, "esplora_mempool:txids"
                               (path segments are expressed with ":")

FOOT-GUNS DISCOVERED WHILE BUILDING THIS (each cost a probe cycle)
    1. USER-AGENT FILTER. The edge (tlsd) returns HTTP 403 for the default
       "Python-urllib/3.x" UA. ANY other UA passes. This is why _UA exists and
       why it must never be removed. Verified: curl UA -> 200, no UA -> 403.
    2. TLS CERTS. python.org Python on macOS ships no system trust store, so
       urllib raises CERTIFICATE_VERIFY_FAILED. We pin certifi, falling back to
       /etc/ssl/cert.pem (present on macOS) so the bot runs on a bare box.
    3. NO JSON-RPC BATCHING. subfrost.io rejects batch arrays outright
       (see ~/.claude/subfrost-brain/services/subfrost-io-rpc.md). Concurrency
       must be many parallel HTTP POSTs, never one batched body -> see call_many.
    4. METHOD ALLOWLIST. getblocktemplate is refused with
       "method not permitted on the public endpoint". That is WHY this project
       reimplements block-template assembly locally in radar/blocktemplate.py
       instead of just asking bitcoind what the next block looks like.
    5. jsonrpc VERSION. bitcoind passthrough replies carry "jsonrpc":"1.0";
       alkanes methods carry "2.0". We do not validate the version field.

MEASURED THROUGHPUT (2026-08-24, 24 worker threads, residential uplink)
    getrawtransaction: ~143 tx/s sustained, ~140 ms median latency.
    getrawmempool[true]: 21 MB / ~3 s for a 37 k-tx mempool.
    Mempool inflow measured at ~54 new tx/s, so a 24-thread pool keeps up
    with full-fidelity scanning with headroom.
"""

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_URL = os.environ.get("RADAR_RPC_URL", "https://mainnet.subfrost.io/v4/subfrost")

# Foot-gun #1: anything but Python-urllib. Do not remove.
_UA = "alkanes-mempool-radar/0.1"


def _ssl_context():
    """Foot-gun #2: build a context with an explicit CA bundle."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        if os.path.exists("/etc/ssl/cert.pem"):
            return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
        return ssl.create_default_context()


class RpcError(RuntimeError):
    pass


class Rpc:
    """Thread-safe JSON-RPC client. Safe to share across a ThreadPoolExecutor."""

    def __init__(self, url=DEFAULT_URL, timeout=30, retries=2):
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self.ctx = _ssl_context()
        self._id = 0
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "errors": 0, "retries": 0}

    def _next_id(self):
        with self._lock:
            self._id += 1
            return self._id

    def call(self, method, params=None, timeout=None):
        body = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or [], "id": self._next_id()}
        ).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": _UA},
        )
        last = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(
                    req, timeout=timeout or self.timeout, context=self.ctx
                ) as r:
                    payload = json.load(r)
                with self._lock:
                    self.stats["calls"] += 1
                if "error" in payload and payload["error"]:
                    raise RpcError(f"{method}: {payload['error']}")
                return payload.get("result")
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                last = e
                with self._lock:
                    self.stats["retries"] += 1
                # Transport-level failure: back off briefly and retry.
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        with self._lock:
            self.stats["errors"] += 1
        raise RpcError(f"{method} failed after {self.retries + 1} attempts: {last}")

    def call_many(self, method, params_list, workers=24, on_error=None):
        """Foot-gun #3: parallel POSTs, never a batch array.

        Returns a list positionally aligned with params_list; failures become
        None (or on_error(exc) if supplied) so one bad tx never sinks a scan.
        """

        def one(p):
            try:
                return self.call(method, p)
            except Exception as e:  # noqa: BLE001 - a single tx must not abort the sweep
                return on_error(e) if on_error else None

        if not params_list:
            return []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(one, params_list))

    # ---- convenience wrappers (bitcoind passthrough) ----

    def block_count(self):
        return int(self.call("getblockcount"))

    def block_hash(self, height):
        return self.call("getblockhash", [int(height)])

    def block(self, block_hash, verbosity=2, timeout=120):
        return self.call("getblock", [block_hash, verbosity], timeout=timeout)

    def raw_mempool(self, verbose=True, timeout=180):
        return self.call("getrawmempool", [bool(verbose)], timeout=timeout)

    def raw_tx(self, txid):
        return self.call("getrawtransaction", [txid, False])

    def raw_txs(self, txids, workers=24):
        return self.call_many("getrawtransaction", [[t, False] for t in txids], workers=workers)
