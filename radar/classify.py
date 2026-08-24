"""
radar/classify.py — decoded cellpack -> economic intent.

PURPOSE
    Turn "[4:65522] opcode 13, args [...]" into a typed, priced event the
    projection engine can act on. Everything here is derived from BYTES ONLY -
    no trace, no indexer - so it describes what a tx REQUESTS, not what the VM
    will return. See radar/protostone.py "INTENT vs EFFECT".

THE CONTRACT MAP (mainnet, observed live 2026-08-24 in blocks 963889-963893)
    2:0        DIESEL genesis      op 77 = mint            (6,977 pending)
    4:65522    OYL AMM factory     op 13 = swap-exact      (3 in 5 blocks)
    32:0       frBTC               op 77 = wrap, 78 = unwrap
    4:31       (unidentified)      op 1                    (114 in 5 blocks)
    1:0        deploy/create       op 77
    4:7782     (unidentified)      op 5
    Unknown targets are preserved verbatim as intent="unknown" rather than
    dropped - an unrecognised contract suddenly taking flow is itself a signal.

SWAP ARGUMENT LAYOUT - REVERSE-ENGINEERED AND CROSS-VALIDATED
    Factory opcode 13 cellpack inputs decode as:
        [13, path_len, (block, tx) * path_len, amount_in, min_out, deadline]

    Confirmed against two independent live mainnet txs of DIFFERENT path
    lengths, which is what pins the layout (a single sample could not
    distinguish path_len from a leading flag):

      2-hop, tx dfc0591b41f3 @963889:
        [13, 2, 32,0, 2,0, 3718278, 5415860879, 963907]
        -> frBTC[32:0] -> DIESEL[2:0], in=3718278, min_out=5415860879,
           deadline=963907 (tip was 963893, so a plausible ~14-block deadline)

      3-hop, tx 03c28a6f860a @963890:
        [13, 3, 32,0, 2,0, 2,490, 299700, 12965148476184528, 963905]
        -> frBTC -> DIESEL -> [2:490], in=299700, deadline=963905

    Both parse cleanly under one rule and produce a sane future-height
    deadline, which would not happen under a mis-read layout.

TRAILING ZEROS ARE REAL
    The 15-byte protostone packing pads the final word with 0x00, and
    alkanes-rs does NOT trim them - they arrive as genuine trailing cellpack
    inputs. We slice positionally by path_len instead of trusting list length,
    so padding never shifts amount_in.

THE ZAP PATTERN
    A wrap and a swap routinely share one tx as separate protostones
    (p0 = [32:0] op77 wrap, p1 = [4:65522] op13 swap). We classify each
    protostone independently and let the projection engine see both legs.
"""

DIESEL_ID = "2:0"
FRBTC_ID = "32:0"
AMM_FACTORY_ID = "4:65522"

DIESEL_MINT_OP = 77
AMM_SWAP_OP = 13
FRBTC_WRAP_OP = 77
FRBTC_UNWRAP_OP = 78

KNOWN_CONTRACTS = {
    DIESEL_ID: "DIESEL genesis",
    FRBTC_ID: "frBTC",
    AMM_FACTORY_ID: "OYL AMM factory",
    "4:31": "unidentified (4:31)",
    "4:7782": "unidentified (4:7782)",
}


def parse_swap_args(args):
    """Decode factory op-13 args -> dict(path, amount_in, min_out, deadline).

    Returns None if the layout does not hold (truncated or malformed), so a
    weird tx degrades to intent="unknown" instead of injecting a bogus trade.
    """
    if not args:
        return None
    try:
        path_len = int(args[0])
    except (TypeError, ValueError):
        return None
    # Guard against absurd path_len from a mis-decode driving a huge slice.
    if path_len < 2 or path_len > 8:
        return None
    need = 1 + 2 * path_len + 3
    if len(args) < need:
        return None
    path = []
    i = 1
    for _ in range(path_len):
        path.append(f"{int(args[i])}:{int(args[i + 1])}")
        i += 2
    return {
        "path": path,
        "amount_in": int(args[i]),
        "min_out": int(args[i + 1]),
        "deadline": int(args[i + 2]),
    }


def classify(target, opcode, args):
    """Map one cellpack to an intent record."""
    base = {"target": target, "opcode": opcode, "contract": KNOWN_CONTRACTS.get(target)}

    if target == DIESEL_ID and opcode == DIESEL_MINT_OP:
        return {**base, "intent": "diesel_mint"}

    if target == AMM_FACTORY_ID and opcode == AMM_SWAP_OP:
        parsed = parse_swap_args(args)
        if parsed is None:
            return {**base, "intent": "amm_swap_unparsed", "args": [str(a) for a in args[:12]]}
        return {**base, "intent": "amm_swap", **parsed}

    if target == FRBTC_ID and opcode == FRBTC_WRAP_OP:
        return {**base, "intent": "frbtc_wrap"}

    if target == FRBTC_ID and opcode == FRBTC_UNWRAP_OP:
        # observed: [78, 2, 1374563] - trailing value is the unwrap amount
        amount = int(args[1]) if len(args) > 1 else None
        return {**base, "intent": "frbtc_unwrap", "amount": amount}

    return {**base, "intent": "unknown", "args": [str(a) for a in args[:8]]}


def classify_tx(decoded, iter_cellpacks):
    """Classify every protostone in one decoded tx. Returns a list of intents."""
    out = []
    for idx, target, opcode, args in iter_cellpacks(decoded):
        rec = classify(target, opcode, args)
        rec["protostone_index"] = idx
        out.append(rec)
    return out
