import json

import pytest

from balance_guard import STATE_FILE, BalanceGuard
from wallet_manager import compute_order_shares


class TestComputeOrderShares:
    def test_kelly_budget_above_minimum(self):
        # $10 de mise Kelly à $0.50 → 20 parts, sous le plafond de 20 % de $100
        assert compute_order_shares(10.0, 0.50, balance=100.0, max_fraction=0.20) == 20

    def test_never_exceeds_mode_cap(self):
        # Kelly demande $50 mais le plafond est 10 % de $100 = $10 → 20 parts max
        shares = compute_order_shares(50.0, 0.50, balance=100.0, max_fraction=0.10)
        assert shares == 20
        assert shares * 0.50 <= 100.0 * 0.10

    def test_bumps_to_minimum_only_within_cap(self):
        # Kelly → $1 (2 parts) ; 5 parts = $2.50 ≤ 20 % de $24 = $4.80 → OK
        assert compute_order_shares(1.0, 0.50, balance=24.0, max_fraction=0.20) == 5

    def test_skips_when_minimum_exceeds_cap(self):
        # Ancien comportement : monter à 5 parts jusqu'à 99 % du solde.
        # 5 × $0.80 = $4 > 20 % de $10 → on saute le trade.
        assert compute_order_shares(1.0, 0.80, balance=10.0, max_fraction=0.20) == 0

    @pytest.mark.parametrize("price", [0.0, 1.0, -0.1])
    def test_invalid_price(self, price):
        assert compute_order_shares(10.0, price, balance=100.0, max_fraction=0.2) == 0

    def test_zero_cap_means_no_order(self):
        assert compute_order_shares(10.0, 0.50, balance=100.0, max_fraction=0.0) == 0


class TestBalanceGuardModes:
    def test_risk_decreases_as_balance_drops(self):
        modes = ["NORMAL", "SURVIE", "DERNIER", "RUINE"]
        caps = [BalanceGuard.params_for_mode(m)["kelly_cap"] for m in modes]
        fracs = [BalanceGuard.params_for_mode(m)["kelly_fraction"] for m in modes]
        mins = [BalanceGuard.params_for_mode(m)["min_confidence"] for m in modes]
        assert caps == sorted(caps, reverse=True)
        assert fracs == sorted(fracs, reverse=True)
        assert mins == sorted(mins)

    def test_last_resort_and_ruin_never_trade(self):
        for mode in ("DERNIER", "RUINE"):
            p = BalanceGuard.params_for_mode(mode)
            assert p["kelly_cap"] == 0
            assert p["min_confidence"] > 0.98  # confiance plafonnée à 0.98

    @pytest.mark.parametrize("balance,mode", [
        (100.0, "NORMAL"), (15.0, "NORMAL"), (14.99, "SURVIE"),
        (3.0, "SURVIE"), (2.99, "DERNIER"), (0.50, "DERNIER"), (0.49, "RUINE"),
    ])
    def test_mode_thresholds(self, balance, mode):
        assert BalanceGuard.get_mode_for_balance(balance) == mode

    def test_stale_state_file_cannot_restore_all_in(self):
        # Fichier écrit par l'ancienne version : DERNIER avec cap 100 % et Full Kelly
        with open(STATE_FILE, "w") as f:
            json.dump({"mode": "DERNIER", "kelly_cap": 1.0, "kelly_fraction": 1.0,
                       "min_confidence": 0.80}, f)
        state = BalanceGuard.read_current_mode()
        assert state["mode"] == "DERNIER"
        assert state["kelly_cap"] == 0.0

    def test_missing_or_unknown_state_defaults_to_normal(self):
        assert BalanceGuard.read_current_mode()["mode"] == "NORMAL"
        with open(STATE_FILE, "w") as f:
            json.dump({"mode": "ALL_IN"}, f)
        assert BalanceGuard.read_current_mode() == BalanceGuard.params_for_mode("NORMAL")
