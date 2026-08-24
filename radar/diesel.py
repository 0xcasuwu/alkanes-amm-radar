"""
radar/diesel.py — the DIESEL pro-rata mint model.

WHY THIS IS THE HEADLINE METRIC, NOT AN AFTERTHOUGHT
    Measured on mainnet 2026-08-24: of the top 8,000 mempool txs by fee rate,
    6,977 were DIESEL mints ([2:0] opcode 77) and exactly ZERO were AMM swaps.
    Across blocks 963889-963893 the confirmed mint counts were
    1930 / 1310 / 1891 / 4889 / 4892 - a 3.7x swing block to block.
    DIESEL issuance per mint is therefore the highest-frequency, highest-
    amplitude "price action to come" signal in the alkanes mempool. AMM swaps
    (~0.6 per block) are the rare, high-impact tail.

THE PAYOUT IS A PRO-RATA SPLIT, NOT A RACE
    Since the upgraded genesis alkane, every mint in a block splits one fixed
    block reward. Your yield is your_mints / total_mints. Paying a higher fee
    does NOT win you a bigger share - it only buys inclusion. This is why a
    mempool census predicts dilution directly.

EXACT FORMULA - alkanes-std-genesis-alkane-upgraded-eoa/src/lib.rs:326-333
    total_mints     = number_diesel_mints()        # precompile [800000000, 2]
    total_miner_fee = total_miner_fee()            # precompile [800000000, 3]
    block_reward    = current_block_reward()
    total_tx_fee    = max(0, total_miner_fee - block_reward)
    diesel_fee      = min(block_reward / 2, total_tx_fee)
    value_per_mint  = (block_reward - diesel_fee) / total_mints

THE UNIT COINCIDENCE THAT MAKES total_tx_fee MEANINGFUL
    `total_miner_fee` is NOT the fee total. It is the sum of the coinbase
    transaction's OUTPUT values, in SATS - i.e. subsidy + fees
    (src/vm/host_functions.rs _get_total_miner_fee).
    Meanwhile `block_reward` is in DIESEL base units, and mainnet DIESEL uses
    the identical schedule to Bitcoin's subsidy: 50e8 >> (height / 210000).
    So while both sit in the same halving epoch, the two are NUMERICALLY EQUAL
    and `total_miner_fee - block_reward` cleanly reduces to the block's FEE
    TOTAL IN SATS. It reads like a unit-mixing bug; it is load-bearing, and the
    equality is what makes the subtraction mean anything at all.
    ==> Consequence: at the NEXT halving these two terms decouple only if the
    DIESEL and BTC schedules drift apart. They do not (same divisor, same
    interval), so the identity holds. We still assert it in predict() and
    surface a warning rather than silently emitting a wrong number.

    Consequence #2: fees genuinely reduce DIESEL issuance. A block with
    5,000,000 sats of fees burns 5,000,000 DIESEL base units off the reward
    (capped at half). High-fee blocks pay mints less.

MINT COUNTING (src/vm/host_functions.rs _get_number_diesel_mints)
    The precompile re-decodes every runestone in the block and counts a tx ONCE
    (it `break`s after the first matching protostone) when the cellpack targets
    [2,0] with inputs[0] == 77. Protostone.message is Vec<u8>, so the
    counter's `to_be_bytes()` is a no-op identity on bytes - it sees exactly the
    same byte stream our decoder does. Our count and the precompile's agree.

    NOTE: the count includes txs whose mint would later REVERT (duplicate
    tx-hash, non-EOA caller, supply cap). Those still dilute the denominator.
    That makes our prediction an accurate model of the contract, including its
    quirks - not an idealised one.
"""

HALVING_INTERVAL = 210_000
INITIAL_REWARD = 5_000_000_000  # 50e8, mainnet - genesis-alkane .../lib.rs:93
MAX_SUPPLY = 156_250_000_000_000
DIESEL_DECIMALS = 8


def block_reward(height):
    """Mainnet DIESEL block reward at `height`.

    Source: alkanes-std-genesis-alkane-upgraded-eoa/src/lib.rs:93
        (50e8 as u128) / (1u128 << ((n as u128) / 210000u128))
    `n` is the ABSOLUTE bitcoin height (chain.rs:28 passes CONTEXT_HANDLE.height()),
    NOT height-minus-genesis_block. Using height-800000 would put us in the
    wrong halving epoch and overstate the reward 16x.
    """
    return INITIAL_REWARD // (1 << (int(height) // HALVING_INTERVAL))


def btc_subsidy(height):
    """Bitcoin subsidy in sats - same schedule, used to derive fees from coinbase."""
    return 5_000_000_000 // (1 << (int(height) // HALVING_INTERVAL))


def diesel_fee_for(reward, total_miner_fee):
    """diesel_fee = min(reward/2, max(0, total_miner_fee - reward))."""
    total_tx_fee = max(0, int(total_miner_fee) - int(reward))
    return min(int(reward) // 2, total_tx_fee)


def value_per_mint(height, total_mints, total_miner_fee):
    """The exact per-mint payout the contract will compute. Integer floor div."""
    total_mints = int(total_mints)
    if total_mints <= 0:
        return 0
    reward = block_reward(height)
    fee = diesel_fee_for(reward, total_miner_fee)
    return (reward - fee) // total_mints


def predict(height, pending_mints, predicted_fees_sats):
    """Project next-block DIESEL economics from a mempool census.

    `predicted_fees_sats` is the FEE total of the projected block (not including
    subsidy); we reconstruct the coinbase-output figure the contract will see as
    subsidy + fees.
    """
    reward = block_reward(height)
    subsidy = btc_subsidy(height)
    total_miner_fee = subsidy + int(predicted_fees_sats)
    fee = diesel_fee_for(reward, total_miner_fee)
    per_mint = value_per_mint(height, pending_mints, total_miner_fee)
    return {
        "height": int(height),
        "block_reward": reward,
        "btc_subsidy_sats": subsidy,
        "schedule_identity_holds": reward == subsidy,  # see module docstring
        "predicted_fees_sats": int(predicted_fees_sats),
        "total_miner_fee": total_miner_fee,
        "diesel_fee": fee,
        "distributable": reward - fee,
        "mints": int(pending_mints),
        "value_per_mint": per_mint,
        "value_per_mint_display": per_mint / (10**DIESEL_DECIMALS),
        "fee_drag_pct": (fee / reward * 100.0) if reward else 0.0,
    }


def dilution_vs(previous_per_mint, projected_per_mint):
    """Percent change in per-mint yield. Negative = each mint earns less."""
    if not previous_per_mint:
        return None
    return (projected_per_mint - previous_per_mint) / previous_per_mint * 100.0
