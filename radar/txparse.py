"""
radar/txparse.py — minimal consensus-encoding Bitcoin transaction parser.

PURPOSE
    Turn `getrawtransaction(txid, false)` hex into the few fields the radar
    needs, with zero dependencies and no second RPC round-trip.

WHY NOT ASK THE NODE TO DECODE?
    We fetch ~50-150 tx/s during a sweep. `getrawtransaction(txid, true)`
    returns a verbose JSON blob roughly an order of magnitude larger than the
    raw hex and costs the gateway a decode per call. Parsing locally keeps the
    scan cheap enough to run continuously: hex-only responses averaged ~500 B
    against multi-KB verbose ones (measured 2026-08-24).

SCOPE / DELIBERATE OMISSIONS
    We do NOT validate signatures, scripts, or amounts-in. The radar only needs
    outputs (to find the runestone OP_RETURN), input outpoints (to detect
    same-block dependency chains), and weight (for template packing).

FORMAT NOTES (BIP-141)
    Segwit marker is the two bytes 0x00 0x01 sitting where the input count
    would be. Legacy txs have no marker and no witness section. Witness stacks
    are serialised AFTER the outputs, one stack per input, and are what makes
    vsize != size.

WEIGHT MATH
    weight = base_size * 3 + total_size ; vsize = ceil(weight / 4)
    `base_size` is the serialisation with marker/flag and witnesses stripped.
    radar/blocktemplate.py packs on weight, so this has to be exact.

SOURCE
    Field layout mirrors bitcoin core consensus encoding; cross-checked against
    alkanes-rs `crates/ordinals/src/runestone.rs` output-scanning order, which
    takes the FIRST OP_RETURN whose script begins 6a5d (see find_runestone).
"""

import struct


def _varint(b, i):
    n = b[i]
    i += 1
    if n < 0xFD:
        return n, i
    if n == 0xFD:
        return struct.unpack_from("<H", b, i)[0], i + 2
    if n == 0xFE:
        return struct.unpack_from("<I", b, i)[0], i + 4
    return struct.unpack_from("<Q", b, i)[0], i + 8


def parse_tx(raw_hex):
    """Parse raw tx hex -> dict(version, vin, vout, locktime, size/vsize/weight).

    Raises ValueError on malformed input so callers can skip the tx.
    """
    try:
        b = bytes.fromhex(raw_hex)
    except ValueError as e:
        raise ValueError(f"bad hex: {e}") from e

    total_size = len(b)
    i = 0
    version = struct.unpack_from("<i", b, i)[0]
    i += 4

    segwit = False
    if i + 1 < len(b) and b[i] == 0x00 and b[i + 1] == 0x01:
        segwit = True
        i += 2

    nin, i = _varint(b, i)
    vin = []
    for _ in range(nin):
        prev_txid = b[i : i + 32][::-1].hex()
        i += 32
        prev_vout = struct.unpack_from("<I", b, i)[0]
        i += 4
        slen, i = _varint(b, i)
        i += slen
        seq = struct.unpack_from("<I", b, i)[0]
        i += 4
        vin.append({"txid": prev_txid, "vout": prev_vout, "sequence": seq})

    nout, i = _varint(b, i)
    vout = []
    for n in range(nout):
        value = struct.unpack_from("<Q", b, i)[0]
        i += 8
        slen, i = _varint(b, i)
        vout.append({"n": n, "value": value, "scriptPubKey": b[i : i + slen].hex()})
        i += slen

    # Everything up to here (minus marker/flag) is the base serialisation.
    base_size = i - (2 if segwit else 0)

    if segwit:
        for k in range(nin):
            nitems, i = _varint(b, i)
            items = []
            for _ in range(nitems):
                ln, i = _varint(b, i)
                items.append(b[i : i + ln].hex())
                i += ln
            vin[k]["witness"] = items

    locktime = struct.unpack_from("<I", b, i)[0]
    i += 4
    base_size += 4  # locktime is part of the base serialisation

    weight = base_size * 3 + total_size
    return {
        "version": version,
        "vin": vin,
        "vout": vout,
        "locktime": locktime,
        "size": total_size,
        "weight": weight,
        "vsize": (weight + 3) // 4,
    }


def find_runestone(tx):
    """Return the first output whose scriptPubKey starts with OP_RETURN OP_PUSHNUM_13.

    alkanes-rs scans ALL outputs and uses the FIRST match
    (crates/ordinals/src/runestone.rs). We replicate that exactly - taking the
    last match, or any match, would diverge from the indexer on multi-OP_RETURN
    transactions.
    """
    for o in tx["vout"]:
        if o["scriptPubKey"].startswith("6a5d"):
            return o
    return None


def has_runestone_magic(raw_hex):
    """Cheap pre-filter before a full parse. Substring test on the hex.

    False positives are possible (the bytes can appear inside a signature), so
    this only ever GATES a real parse - it never classifies on its own.
    """
    return "6a5d" in raw_hex
