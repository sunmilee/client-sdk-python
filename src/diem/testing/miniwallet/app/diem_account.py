# Copyright (c) The Diem Core Contributors
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from random import randrange
from typing import Tuple, Optional, Union, List
from diem import identifier, offchain, stdlib, utils, txnmetadata, diem_types
from diem.jsonrpc import AsyncClient
from diem.testing import LocalAccount, Faucet
from .models import Transaction, RefundReason


@dataclass
class DiemAccount:
    _account: LocalAccount
    _child_accounts: List[LocalAccount]
    _client: AsyncClient

    @property
    def hrp(self) -> str:
        return self._account.hrp

    def sign_by_compliance_key(self, msg: bytes) -> bytes:
        return self._account.compliance_key.sign(msg)

    def account_identifier(self, subaddress: Union[str, bytes, None] = None) -> str:
        return self._get_payment_account().account_identifier(subaddress)

    def decode_account_identifier(self, encoded_id: str) -> Tuple[diem_types.AccountAddress, Optional[bytes]]:
        return identifier.decode_account(encoded_id, self.hrp)

    def refund_metadata(self, version: int, reason: RefundReason) -> Tuple[bytes, bytes]:
        return (txnmetadata.refund_metadata(version, reason.to_diem_type()), b"")

    def general_metadata(self, from_subaddress: bytes, payee: str) -> Tuple[bytes, bytes]:
        to_account, to_subaddress = identifier.decode_account(payee, self.hrp)
        return (txnmetadata.general_metadata(from_subaddress, to_subaddress), b"")

    def travel_metadata(self, cmd: offchain.PaymentCommand) -> Tuple[bytes, bytes]:
        metadata = cmd.travel_rule_metadata(self.hrp)
        return (metadata, bytes.fromhex(str(cmd.payment.recipient_signature)))

    def payment_metadata(self, reference_id: str) -> Tuple[bytes, bytes]:
        return (txnmetadata.payment_metadata(reference_id), b"")

    async def diem_id_domains(self) -> List[str]:
        account = await self._client.get_account(self._account.account_address)
        return [] if account is None else account.role.diem_id_domains

    async def submit_p2p(
        self,
        txn: Transaction,
        metadata: Tuple[bytes, bytes],
        by_address: Optional[diem_types.AccountAddress] = None,
    ) -> diem_types.SignedTransaction:
        from_account = self._get_payment_account(by_address)

        await self._ensure_account_balance(from_account, txn)
        to_account = identifier.decode_account_address(str(txn.payee_account_identifier), self.hrp)
        payload = stdlib.encode_peer_to_peer_with_metadata_script_function(
            currency=utils.currency_code(txn.currency),
            amount=txn.amount,
            payee=to_account,
            metadata=metadata[0],
            metadata_signature=metadata[1],
        )
        return await from_account.submit_txn(self._client, payload)

    def _get_payment_account(self, address: Optional[diem_types.AccountAddress] = None) -> LocalAccount:
        if address is None:
            if self._child_accounts:
                return self._child_accounts[randrange(len(self._child_accounts))]
            return self._account
        for account in self._child_accounts:
            if account.account_address == address:
                return account
        raise ValueError(
            "could not find account by address: %s in child accounts: %s"
            % (address.to_hex(), list(map(lambda a: a.to_dict(), self._child_accounts)))
        )

    async def _ensure_account_balance(self, account: LocalAccount, txn: Transaction) -> None:
        data = await self._client.must_get_account(account.account_address)
        amount = max(txn.amount, 1_000_000_000_000)
        for balance in data.balances:
            if balance.currency == txn.currency and balance.amount < txn.amount:
                await Faucet(self._client).mint(account.auth_key.hex(), amount, txn.currency)
                return
