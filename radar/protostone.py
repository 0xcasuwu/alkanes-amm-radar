"""
radar/protostone.py — raw tx -> decoded alkanes protostones.

PURPOSE
    Thin, well-documented wrapper over the byte-verified decoder vendored as
    radar/_protostone_vendor.py.

PROVENANCE OF THE VENDORED DECODER
    Copied verbatim from ~/.claude/skills/alkanes-local-decode/lib/decode_protostone.py.
    That decoder is byte-verified against alkanes-rs:
      * crates/ordinals/src/runestone.rs        - magic (6a5d), payload concat,
                                                  cenotaph gate
      * crates/ordinals/src/runestone/tag.rs    - Tag::Protocol == 16383
      * crates/protorune-support/src/byte_utils.rs   - snap_to_15_bytes
      * crates/protorune-support/src/protostone.rs   - decipher, edict deltas
      * crates/alkanes-support/src/cellpack.rs  - Cellpack::try_from
    It is VENDORED rather than imported from ~/.claude/skills so this bot runs
    standalone on any box. If the skill decoder is corrected, re-copy it.

THE TWO DECODE MISTAKES THIS AVOIDS
    1. Tag 16383 is odd, so a naive reader dismisses it as an ignorable odd
       Runes tag and concludes "inert runestone". It is in fact the alkanes
       carrier. Miss it and 100% of alkanes traffic is invisible.
    2. Protocol words repack at FIFTEEN bytes per u128 (high byte dropped),
       not sixteen. The JS snippet in ~/.claude/subfrost-brain/concepts/
       protostone-encoding.md uses 16 and is WRONG; the Rust source
       (snap_to_15_bytes) and this vendored decoder use 15. Reading 16 yields
       plausible-looking garbage rather than an obvious failure, which is what
       makes it dangerous.

CENOTAPHS
    A cenotaph voids the whole runestone - the indexer executes NOTHING. We
    surface those separately (see decode_tx) and must never let one count as
    pending price action; that would inflate every projection with txs the VM
    will refuse to run.

INTENT vs EFFECT (read before trusting any number downstream)
    A protostone is a REQUEST, not a receipt. These bytes prove what a tx ASKS
    the alkanes VM to do. They do NOT prove it will succeed: swaps revert on
    slippage, mints revert on duplicate-tx or supply cap, edicts CLAMP to the
    available balance. Every projection built on this module is therefore an
    upper bound on "if all of this executes as requested".
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _protostone_vendor as _vendor  # noqa: E402

from .txparse import find_runestone, parse_tx  # noqa: E402


def decode_script(spk_hex):
    """Decode one runestone scriptPubKey hex -> decoder dict."""
    return _vendor.decode_op_return(spk_hex)


def decode_tx(raw_hex, txid=None):
    """Full path: raw tx hex -> {txid, tx, runestone, cenotaph, protostones}.

    Returns None when the tx carries no runestone at all (the common case:
    ~78% of top-of-mempool is alkanes right now, but the tail is ordinary
    Bitcoin traffic).
    """
    try:
        tx = parse_tx(raw_hex)
    except Exception:
        return None
    out = find_runestone(tx)
    if out is None:
        return None
    try:
        decoded = decode_script(out["scriptPubKey"])
    except Exception as e:  # a malformed runestone must not sink the sweep
        return {
            "txid": txid,
            "tx": tx,
            "runestone": False,
            "cenotaph": False,
            "error": str(e),
            "protostones": [],
        }
    if not decoded.get("runestone"):
        return None
    return {
        "txid": txid,
        "tx": tx,
        "runestone": True,
        "cenotaph": bool(decoded.get("cenotaph")),
        "flaw": decoded.get("flaw"),
        "protostones": decoded.get("protostones", []),
        "vout_count": len(tx["vout"]),
    }


def iter_cellpacks(decoded):
    """Yield (protostone_index, target_id_str, opcode, args) for live protostones.

    Cenotaphs yield nothing - the indexer would run nothing.
    A bare [block,tx] cellpack has NO opcode (alkanes-support/src/cellpack.rs);
    we yield opcode None rather than mislabelling it as opcode 0.
    """
    if not decoded or decoded.get("cenotaph") or not decoded.get("runestone"):
        return
    for idx, ps in enumerate(decoded.get("protostones", [])):
        cp = ps.get("cellpack")
        if not cp:
            continue
        tgt = cp.get("target") or {}
        if isinstance(tgt, dict):
            target = f"{tgt.get('block')}:{tgt.get('tx')}"
        else:
            target = str(tgt)
        inputs = list(cp.get("inputs") or [])
        opcode = inputs[0] if inputs else None
        args = inputs[1:] if len(inputs) > 1 else []
        yield idx, target, opcode, args


def shadow_vout(protostone_index, vout_count):
    """shadow_vout = i + tx.output.len() + 1  (protorune/src/lib.rs:961).

    This is the vout to pass to alkanes_trace for THIS protostone - needed if a
    caller later wants execution truth instead of decoded intent.
    """
    return protostone_index + vout_count + 1
