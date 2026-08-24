"""
tests/test_radar.py — unit tests for the radar's pure functions.

TESTING PHILOSOPHY (per the project's standing rules)
    Lightweight, in-memory, no network, no fixtures on disk. Every test here
    runs against either hand-computed expectations or REAL mainnet byte
    payloads captured live on 2026-08-24 and pasted inline. Nothing here needs
    an indexer, a node, or a browser.

    The goal is confidence in the parts that are easy to get silently wrong:
    the swap fee constant, integer floor division, the cellpack argument
    layout, the halving schedule, and block-template packing.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar import amm, diesel  # noqa: E402
from radar.blocktemplate import build_template  # noqa: E402
from radar.classify import classify, parse_swap_args  # noqa: E402
from radar.txparse import parse_tx  # noqa: E402


class TestAmmMath(unittest.TestCase):
    """Ported from oylswap-library/src/lib.rs:206-234."""

    def test_fee_is_one_percent_not_thirty_bps(self):
        # 1000 in on a symmetric 1e6 pool. With a 1% fee the contract's exact
        # integer math yields 989. A 0.3% fee would give 992 - this test is
        # the tripwire against "everyone knows AMM fees are 30 bps".
        self.assertEqual(amm.get_amount_out(1000, 1_000_000, 1_000_000), 989)

    def test_floor_division_is_preserved(self):
        # Deliberately picks values whose true quotient is fractional; the
        # contract truncates, and so must we.
        out = amm.get_amount_out(7, 1_000_003, 999_997)
        self.assertIsInstance(out, int)
        self.assertEqual(out, (990 * 7 * 999_997) // (1000 * 1_000_003 + 990 * 7))

    def test_degenerate_inputs_are_safe(self):
        self.assertEqual(amm.get_amount_out(0, 10, 10), 0)
        self.assertEqual(amm.get_amount_out(10, 0, 10), 0)
        self.assertEqual(amm.get_amount_out(10, 10, 0), 0)

    def test_get_amount_in_rounds_up(self):
        # get_amount_in adds 1 (the contract does), so a round-trip must never
        # under-fund the requested output.
        need = amm.get_amount_in(989, 1_000_000, 1_000_000)
        self.assertGreaterEqual(amm.get_amount_out(need, 1_000_000, 1_000_000), 989)

    def test_apply_swap_conserves_the_invariant_direction(self):
        r_in, r_out, out = amm.apply_swap(1_000_000, 1_000_000, 1000)
        self.assertEqual(r_in, 1_001_000)
        self.assertEqual(r_out, 1_000_000 - out)
        # k must not decrease - the fee accrues to the LPs inside the reserves.
        self.assertGreaterEqual(r_in * r_out, 1_000_000 * 1_000_000)

    def test_price_impact_includes_fee(self):
        impact = amm.price_impact(1000, 1_000_000, 1_000_000)
        self.assertGreater(impact, 0.01)   # at least the 1% fee
        self.assertLess(impact, 0.02)

    def test_simulate_path_multi_hop_and_commit(self):
        pools = {
            frozenset(("A", "B")): {"pool_id": "p1", "reserves": {"A": 1_000_000, "B": 1_000_000}},
            frozenset(("B", "C")): {"pool_id": "p2", "reserves": {"B": 1_000_000, "C": 1_000_000}},
        }
        sim = amm.simulate_path(pools, ["A", "B", "C"], 1000)
        self.assertTrue(sim["ok"])
        self.assertEqual(len(sim["hops"]), 2)
        # Two 1% hops compound: out must be below a single-hop result.
        self.assertLess(sim["amount_out"], 989)
        # simulate must NOT mutate until commit.
        self.assertEqual(pools[frozenset(("A", "B"))]["reserves"]["A"], 1_000_000)
        amm.commit_path(pools, sim)
        self.assertEqual(pools[frozenset(("A", "B"))]["reserves"]["A"], 1_001_000)

    def test_unknown_pool_bails_instead_of_guessing(self):
        sim = amm.simulate_path({}, ["A", "B"], 1000)
        self.assertFalse(sim["ok"])
        self.assertEqual(sim["reason"], "unknown-pool")

    def test_min_out_violation_is_flagged(self):
        pools = {frozenset(("A", "B")): {"pool_id": "p", "reserves": {"A": 1_000, "B": 1_000}}}
        sim = amm.simulate_path(pools, ["A", "B"], 10, min_out=10**9)
        self.assertTrue(sim["ok"])
        self.assertFalse(sim["honors_min_out"])


class TestDieselModel(unittest.TestCase):
    """Formula from alkanes-std-genesis-alkane-upgraded-eoa/src/lib.rs:326-333."""

    def test_halving_uses_absolute_height(self):
        # 963894 // 210000 == 4  ->  50e8 >> 4
        self.assertEqual(diesel.block_reward(963_894), 312_500_000)
        self.assertEqual(diesel.block_reward(0), 5_000_000_000)
        self.assertEqual(diesel.block_reward(210_000), 2_500_000_000)

    def test_schedule_identity_with_btc_subsidy(self):
        # The whole fee term depends on these being numerically equal.
        for h in (800_000, 963_894, 1_050_000):
            self.assertEqual(diesel.block_reward(h), diesel.btc_subsidy(h))

    def test_fee_is_capped_at_half_the_reward(self):
        reward = diesel.block_reward(963_894)
        huge = diesel.btc_subsidy(963_894) + 10**12
        self.assertEqual(diesel.diesel_fee_for(reward, huge), reward // 2)

    def test_no_fee_when_coinbase_is_only_subsidy(self):
        reward = diesel.block_reward(963_894)
        self.assertEqual(diesel.diesel_fee_for(reward, diesel.btc_subsidy(963_894)), 0)

    def test_matches_confirmed_block_963893(self):
        # Ground truth recomputed from the mined block on 2026-08-24:
        # 4892 mints, 962,050 sats of fees -> 0.00063683 DIESEL per mint.
        got = diesel.value_per_mint(
            963_893, 4892, diesel.btc_subsidy(963_893) + 962_050
        )
        self.assertEqual(got, 63_683)

    def test_matches_confirmed_block_963894(self):
        # 1644 mints, 3,061,510 sats of fees -> 0.00188222 DIESEL per mint.
        got = diesel.value_per_mint(
            963_894, 1644, diesel.btc_subsidy(963_894) + 3_061_510
        )
        self.assertEqual(got, 188_222)

    def test_more_mints_dilutes_each_mint(self):
        a = diesel.predict(963_894, 1310, 3_000_000)["value_per_mint"]
        b = diesel.predict(963_894, 4892, 3_000_000)["value_per_mint"]
        self.assertGreater(a, b)

    def test_zero_mints_does_not_divide_by_zero(self):
        self.assertEqual(diesel.value_per_mint(963_894, 0, 10**9), 0)


class TestSwapArgLayout(unittest.TestCase):
    """Layout confirmed against two live mainnet txs of different path lengths."""

    def test_two_hop_real_mainnet_tx(self):
        # tx dfc0591b41f3... @ block 963889
        args = [2, 32, 0, 2, 0, 3718278, 5415860879, 963907, 0, 0, 0, 0]
        p = parse_swap_args(args)
        self.assertEqual(p["path"], ["32:0", "2:0"])
        self.assertEqual(p["amount_in"], 3718278)
        self.assertEqual(p["min_out"], 5415860879)
        self.assertEqual(p["deadline"], 963907)

    def test_three_hop_real_mainnet_tx(self):
        # tx 03c28a6f860a... @ block 963890
        args = [3, 32, 0, 2, 0, 2, 490, 299700, 12965148476184528, 963905, 0]
        p = parse_swap_args(args)
        self.assertEqual(p["path"], ["32:0", "2:0", "2:490"])
        self.assertEqual(p["amount_in"], 299700)
        self.assertEqual(p["deadline"], 963905)

    def test_trailing_zero_padding_does_not_shift_fields(self):
        base = [2, 32, 0, 2, 0, 111, 222, 963907]
        self.assertEqual(parse_swap_args(base), parse_swap_args(base + [0] * 9))

    def test_malformed_layouts_return_none(self):
        self.assertIsNone(parse_swap_args([]))
        self.assertIsNone(parse_swap_args([2, 32, 0]))        # truncated
        self.assertIsNone(parse_swap_args([99, 1, 2, 3]))     # absurd path_len
        self.assertIsNone(parse_swap_args([1, 32, 0, 5, 6]))  # path_len < 2


class TestClassify(unittest.TestCase):
    def test_diesel_mint(self):
        self.assertEqual(classify("2:0", 77, [0] * 12)["intent"], "diesel_mint")

    def test_frbtc_wrap_and_unwrap(self):
        self.assertEqual(classify("32:0", 77, [])["intent"], "frbtc_wrap")
        u = classify("32:0", 78, [2, 1374563])
        self.assertEqual(u["intent"], "frbtc_unwrap")
        self.assertEqual(u["amount"], 1374563)

    def test_unparseable_swap_degrades_not_fabricates(self):
        rec = classify("4:65522", 13, [99])
        self.assertEqual(rec["intent"], "amm_swap_unparsed")

    def test_unknown_contract_is_preserved(self):
        rec = classify("4:31", 1, [1, 0])
        self.assertEqual(rec["intent"], "unknown")
        self.assertEqual(rec["target"], "4:31")


class TestTxParse(unittest.TestCase):
    def test_parses_legacy_tx_and_weight(self):
        # Minimal hand-built legacy tx: 1 input, 1 OP_RETURN output.
        raw = (
            "01000000" "01"
            + "00" * 32 + "ffffffff" + "00" + "ffffffff"
            + "01" + "0000000000000000" + "04" + "6a5d0102"
            + "00000000"
        )
        tx = parse_tx(raw)
        self.assertEqual(len(tx["vin"]), 1)
        self.assertEqual(tx["vout"][0]["scriptPubKey"], "6a5d0102")
        # Legacy tx: weight == base*3 + total == size*4
        self.assertEqual(tx["weight"], tx["size"] * 4)

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_tx("zzzz")


class TestBlockTemplate(unittest.TestCase):
    def _entry(self, fee_btc, weight, depends=None):
        vsize = max(1, weight // 4)
        return {
            "fees": {"modified": fee_btc, "ancestor": fee_btc, "base": fee_btc},
            "weight": weight,
            "vsize": vsize,
            "ancestorsize": vsize,
            "depends": depends or [],
        }

    def test_orders_by_fee_rate(self):
        mp = {
            "low": self._entry(0.00000100, 1000),
            "high": self._entry(0.00001000, 1000),
        }
        tpl = build_template(mp, max_weight=4000)
        self.assertEqual(tpl["txids"][0], "high")

    def test_respects_weight_budget(self):
        mp = {f"t{i}": self._entry(0.00001, 1000) for i in range(10)}
        tpl = build_template(mp, max_weight=3000)
        self.assertLessEqual(tpl["weight"], 3000)
        self.assertEqual(tpl["count"], 3)

    def test_cpfp_parent_is_pulled_in_before_child(self):
        # A zero-fee parent must ride in on its high-fee child, parent first.
        mp = {
            "parent": self._entry(0.0, 1000),
            "child": self._entry(0.001, 1000, depends=["parent"]),
            "filler": self._entry(0.0000005, 1000),
        }
        tpl = build_template(mp, max_weight=4000)
        self.assertIn("parent", tpl["included"])
        self.assertLess(tpl["txids"].index("parent"), tpl["txids"].index("child"))

    def test_fees_are_summed_in_sats(self):
        mp = {"a": self._entry(0.00001000, 1000)}  # 1000 sats
        tpl = build_template(mp, max_weight=4000)
        self.assertEqual(tpl["fees_sats"], 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
