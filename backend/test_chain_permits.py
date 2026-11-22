"""Regression tests for GasFree Permit Transfer parsing and counterparty.

Live bug 2026-11-22: Tronscan's ``transaction-info`` lists the GasFree fee
transfer first (1–1.5 USDT to the relayer) and the real transfer second
(500 USDT to the recipient). The resolver normalised ``[0]``, so a 500 USDT
withdrawal exported as "1.5 USDT" with the fee collector as counterparty.
"""

import os
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("S3LM_DATA", _tmp.name)
os.environ.setdefault("S3LM_CHAIN_WORKER", "0")

from . import chain  # noqa: E402
from .app import _flow_counterparty  # noqa: E402

FEE_COLLECTOR = "TLntW9Z59LYY5KEi9cmwk3PKjQga828ird"
RECIPIENT = "TDUtcXXMv3mckf5JYtS8M1sads4CD5LLSN"
SENDER = "TNCnqLvyYAEcu3ABfqwiMbyfKNc8kvsbTg"


def _word(value: str) -> str:
    return value.rjust(64, "0")


# Real Permit Transfer call: transferWithPermit(token, from, to, value, fee)
PERMIT_DATA = (
    "6f21b898"
    + _word("a614f803b6fd780986a42c78ec9c7f77e6ded13c")
    + _word("3816cfb8575cfb857517f9bbb99e47305d99a0d7")
    + _word("2685819b295a569a67a8a7914b465c8db7a3a5e6")
    + _word(format(500_000_000, "x"))
    + _word(format(1_500_000, "x"))
)

FEE_TRANSFER = {
    "from_address": SENDER,
    "to_address": FEE_COLLECTOR,
    "amount_str": "1500000",
    "decimals": 6,
    "symbol": "USDT",
}
MAIN_TRANSFER = {
    "from_address": SENDER,
    "to_address": RECIPIENT,
    "amount_str": "500000000",
    "decimals": 6,
    "symbol": "USDT",
}


class PermitTransferTests(unittest.TestCase):
    def test_contract_call_picks_real_transfer_not_fee(self) -> None:
        picked = chain._pick_trc20_transfer(
            {
                "contractData": {"data": PERMIT_DATA},
                "trc20TransferInfo": [FEE_TRANSFER, MAIN_TRANSFER],
            }
        )
        self.assertEqual(picked["to_address"], RECIPIENT)
        self.assertEqual(picked["amount_str"], "500000000")

    def test_fallback_skips_known_fee_collector(self) -> None:
        picked = chain._pick_trc20_transfer(
            {"trc20TransferInfo": [FEE_TRANSFER, MAIN_TRANSFER]}
        )
        self.assertEqual(picked["to_address"], RECIPIENT)

    def test_single_transfer_is_unchanged(self) -> None:
        picked = chain._pick_trc20_transfer(
            {"trc20TransferInfo": [MAIN_TRANSFER]}
        )
        self.assertEqual(picked["to_address"], RECIPIENT)

    def test_payload_hex_matches_contract_argument(self) -> None:
        self.assertEqual(
            chain._tron_payload_hex(RECIPIENT),
            "2685819b295a569a67a8a7914b465c8db7a3a5e6",
        )
        self.assertEqual(chain._tron_payload_hex("not-an-address"), "")


class MainTransferSubjectTests(unittest.TestCase):
    def test_deposit_uses_recipient(self) -> None:
        self.assertEqual(
            chain._main_transfer_subject({"from": SENDER, "to": RECIPIENT}, "deposit"),
            RECIPIENT,
        )

    def test_withdrawal_uses_sender(self) -> None:
        self.assertEqual(
            chain._main_transfer_subject(
                {"from": SENDER, "to": RECIPIENT}, "withdrawal"
            ),
            SENDER,
        )

    def test_invalid_address_returns_empty(self) -> None:
        self.assertEqual(
            chain._main_transfer_subject({"from": "x", "to": "y"}, "deposit"), ""
        )


class CounterpartyTests(unittest.TestCase):
    def test_deposit_wallet_is_sender(self) -> None:
        self.assertEqual(
            _flow_counterparty(
                {
                    "direction": "deposit",
                    "rail_id": "TSender",
                    "tx_from": "TSender",
                    "tx_to": "TPlatform",
                }
            ),
            "TPlatform",
        )

    def test_deposit_wallet_is_recipient(self) -> None:
        self.assertEqual(
            _flow_counterparty(
                {
                    "direction": "deposit",
                    "rail_id": "TPlatform",
                    "tx_from": "TSender",
                    "tx_to": "TPlatform",
                }
            ),
            "TSender",
        )

    def test_withdrawal_wallet_is_recipient(self) -> None:
        self.assertEqual(
            _flow_counterparty(
                {
                    "direction": "withdrawal",
                    "rail_id": "TUser",
                    "tx_from": "TPlatform",
                    "tx_to": "TUser",
                }
            ),
            "TPlatform",
        )

    def test_absent_wallet_falls_back_to_direction(self) -> None:
        self.assertEqual(
            _flow_counterparty(
                {
                    "direction": "deposit",
                    "rail_id": "TOther",
                    "tx_from": "TSender",
                    "tx_to": "TPlatform",
                }
            ),
            "TPlatform",
        )
        self.assertEqual(
            _flow_counterparty(
                {
                    "direction": "withdrawal",
                    "rail_id": "TOther",
                    "tx_from": "TPlatform",
                    "tx_to": "TUser",
                }
            ),
            "TPlatform",
        )


if __name__ == "__main__":
    unittest.main()
