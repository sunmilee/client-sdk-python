# Copyright (c) The Diem Core Contributors
# SPDX-License-Identifier: Apache-2.0


import typing, dataclasses, uuid, math, warnings, aiohttp

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from json.decoder import JSONDecodeError

from .command import Command
from .payment_command import PaymentCommand

from .types import (
    CommandType,
    CommandRequestObject,
    CommandResponseObject,
    CommandResponseStatus,
    PaymentObject,
    PaymentActorObject,
    PaymentActionObject,
    PaymentCommandObject,
    ErrorCode,
    FieldError,
    from_dict,
    to_dict,
    ReferenceIDCommandObject,
)
from .error import command_error, protocol_error

from . import jws, http_header
from .. import jsonrpc, diem_types, identifier, utils
from diem.jsonrpc.async_client import AsyncClient


DEFAULT_CONNECT_TIMEOUT_SECS: float = 2.0
DEFAULT_TIMEOUT_SECS: float = 30.0
T = typing.TypeVar("T")


class CommandResponseError(Exception):
    def __init__(self, resp: CommandResponseObject) -> None:
        super().__init__(resp)
        self.resp = resp


class InvalidCurrencyCodeError(ValueError):
    pass


class UnsupportedCurrencyCodeError(ValueError):
    pass


@dataclasses.dataclass
class Client:
    """Client for communicating with offchain service.

    Provides outbound and inbound request handlings and convertings between bytes and offchain data types.

    Initialization:
    ```
    >>> from diem import offchain, testing, identifier
    >>> import asyncio
    >>>
    >>> jsonrpc_client = testing.create_client()
    >>> faucet = testing.Faucet(jsonrpc_client)
    >>> async def main():
    ...     account = await faucet.gen_account()
    ...     await account.rotate_dual_attestation_info(jsonrpc_client, base_url="http://vasp.com/offchain")
    ...     compliance_key_account_address = account.account_address
    ...     client = offchain.Client(compliance_key_account_address, jsonrpc_client, identifier.TDM)
    ...     # use client to talk to couterparty VASP offchain service
    ...
    >>> asyncio.run(main())
    ```

    Send command:
    ```
    >>> # for command: offchain.PaymentCommand
    >>> await client.send_command(command, account.compliance_key.sign)
    ```

    See [mini-wallet application](https://diem.github.io/client-sdk-python/diem/testing/miniwallet/app/app.html) for full example
    """

    my_compliance_key_account_address: diem_types.AccountAddress
    jsonrpc_client: AsyncClient
    hrp: str
    supported_currency_codes: typing.Optional[typing.List[str]] = dataclasses.field(default=None)
    my_compliance_key_account_id: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.my_compliance_key_account_id = self.account_id(self.my_compliance_key_account_address)

    async def ping(
        self,
        counterparty_account_identifier: str,
        sign: typing.Callable[[bytes], bytes],
        cid: typing.Optional[str] = None,
    ) -> CommandResponseObject:
        request = CommandRequestObject(
            cid=cid or str(uuid.uuid4()),
            command_type=CommandType.PingCommand,
            command={"_ObjectType": CommandType.PingCommand},
        )
        jws_msg = jws.serialize(request, sign)
        return await self.send_request(self.my_compliance_key_account_id, counterparty_account_identifier, jws_msg)

    async def ref_id_exchange_request(
        self,
        sender: str,
        sender_address: str,
        receiver: str,
        reference_id: str,
        counterparty_account_identifier: str,
        sign: typing.Callable[[bytes], bytes],
        cid: typing.Optional[str] = None,
    ) -> CommandResponseObject:
        reference_id_command_object = ReferenceIDCommandObject(
            sender=sender, sender_address=sender_address, receiver=receiver, reference_id=reference_id
        )
        request = CommandRequestObject(
            cid=cid or str(uuid.uuid4()),
            command_type=CommandType.ReferenceIDCommand,
            command=to_dict(reference_id_command_object),
        )
        jws_msg = jws.serialize(request, sign)
        return await self.send_request(self.my_compliance_key_account_id, counterparty_account_identifier, jws_msg)

    async def send_command(self, command: Command, sign: typing.Callable[[bytes], bytes]) -> CommandResponseObject:
        return await self.send_request(
            request_sender_address=command.my_address(),
            counterparty_account_id=command.counterparty_address(),
            request_bytes=jws.serialize(command.new_request(), sign),
        )

    async def send_request(
        self, request_sender_address: str, counterparty_account_id: str, request_bytes: bytes
    ) -> CommandResponseObject:
        base_url, public_key = await self.get_base_url_and_compliance_key(counterparty_account_id)
        headers = {
            http_header.X_REQUEST_ID: str(uuid.uuid4()),
            http_header.X_REQUEST_SENDER_ADDRESS: request_sender_address,
        }
        url = f"{base_url.rstrip('/')}/v2/command"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=request_bytes, headers=headers) as response:
                if response.status not in [200, 400]:
                    response.raise_for_status()

                cmd_resp = _deserialize_jws(await response.read(), CommandResponseObject, public_key)
                if cmd_resp.status == CommandResponseStatus.failure:
                    raise CommandResponseError(cmd_resp)
                return cmd_resp

    async def process_inbound_request(self, request_sender_address: str, request_bytes: bytes) -> Command:
        """Deprecated

        validate and decode the `request_bytes` into `diem.offchain.command.Command` object.

        This function is split into `deserialize_inbound_request` and `process_inbound_payment_command_request`
        for handling command processing properly in the future when new commands are added.
        """

        warnings.warn(
            "`process_inbound_request` is deprecated, call `deserialize_inbound_request` instead, and then call `process_inbound_payment_command_request` if the inbound request is for `PaymentCommand`"
        )

        request = await self.deserialize_inbound_request(request_sender_address, request_bytes)
        return await self.process_inbound_payment_command_request(request_sender_address, request)

    async def deserialize_inbound_request(
        self, request_sender_address: str, request_bytes: bytes
    ) -> CommandRequestObject:
        """Validate and decode the `request_bytes` into `diem.offchain.command.CommandRequestObject` object.

        Raises `diem.offchain.error.Error` with `protocol_error` when:

        - `request_sender_address` is not provided.
        - Cannot find on-chain account by the `request_sender_address`.
        - Cannot get base url and public key from the on-chain account found by the `request_sender_address`.
        - `request_bytes` is an invalid JWS message: JWS message format or content is invalid, or signature is invalid.
        - Cannot decode `request_bytes` into `diem.offchain.types.command_types.CommandRequestObject`.
        - Decoded `diem.offchain.types.command_types.CommandRequestObject` is invalid according to DIP-1 data structure requirements.
        """

        if not request_sender_address:
            raise protocol_error(ErrorCode.missing_http_header, f"missing {http_header.X_REQUEST_SENDER_ADDRESS}")
        public_key = await self.get_inbound_request_sender_public_key(request_sender_address)
        return _deserialize_jws(request_bytes, CommandRequestObject, public_key)

    async def process_inbound_payment_command_request(
        self, request_sender_address: str, request: CommandRequestObject
    ) -> PaymentCommand:
        """Validate the `PaymentCommand` and returns a command wrapper object for next processing step.

        Raises `diem.offchain.error.Error` with `command_error` when `diem.offchain.types.command_types.CommandRequestObject.command` is `diem.offchain.payment_command.PaymentCommand`:

        - `diem.offchain.types.payment_types.PaymentObject.sender` or `diem.offchain.types.payment_types.PaymentObject.sender`.address is invalid.
        - `request_sender_address` is not sender or receiver actor address.
        - For initial payment object, the `diem.offchain.types.payment_types.PaymentActionObject.amount` is under dual attestation limit (travel rule limit).
        - When receiver actor statis is `ready_for_settlement`, the `recipient_signature` is not set or is invalid (verifying transaction metadata failed).

        """

        if request.command_type != CommandType.PaymentCommand:
            raise protocol_error(
                ErrorCode.unknown_command_type,
                f"unknown command_type: {request.command_type}",
                field="command_type",
            )

        payment = deserialize_command(request.command, PaymentCommandObject).payment
        self.validate_addresses(payment, request_sender_address)
        cmd = await self.create_inbound_payment_command(request.cid, payment)
        if cmd.is_initial():
            await self.validate_dual_attestation_limit_by_action(cmd.payment.action)
        elif cmd.is_rsend():
            public_key = await self.get_inbound_request_sender_public_key(request_sender_address)
            self.validate_recipient_signature(cmd, public_key)
        return cmd

    async def get_inbound_request_sender_public_key(self, request_sender_address: str) -> Ed25519PublicKey:
        """find the public key of the request sender address, raises protocol error if not found or public key is invalid"""

        try:
            _, public_key = await self.get_base_url_and_compliance_key(request_sender_address)
        except ValueError as e:
            raise protocol_error(ErrorCode.invalid_http_header, str(e)) from e
        return public_key

    def validate_recipient_signature(self, cmd: PaymentCommand, public_key: Ed25519PublicKey) -> None:
        msg = cmd.travel_rule_metadata_signature_message(self.hrp)
        try:
            sig = cmd.payment.recipient_signature
            if sig is None:
                raise ValueError("recipient_signature is not provided")
            public_key.verify(bytes.fromhex(sig), msg)
        except (ValueError, InvalidSignature) as e:
            raise command_error(
                ErrorCode.invalid_recipient_signature, str(e), "command.payment.recipient_signature"
            ) from e

    async def validate_dual_attestation_limit_by_action(self, action: PaymentActionObject) -> None:
        msg = await self.is_under_dual_attestation_limit(action.currency, action.amount)
        if msg:
            raise command_error(ErrorCode.no_kyc_needed, msg, "command.payment.action.amount")

    async def is_under_dual_attestation_limit(self, currency: str, amount: int) -> typing.Optional[str]:
        currencies = await self.jsonrpc_client.get_currencies()
        try:
            await self.validate_currency_code(currency, currencies)
        except InvalidCurrencyCodeError as e:
            raise command_error(ErrorCode.invalid_field_value, str(e), "command.payment.action.currency")
        except UnsupportedCurrencyCodeError as e:
            raise command_error(ErrorCode.unsupported_currency, str(e), "command.payment.action.currency")

        metadata = await self.jsonrpc_client.get_metadata()
        limit = metadata.dual_attestation_limit
        for info in currencies:
            if info.code == currency:
                if _is_under_the_threshold(limit, info.to_xdx_exchange_rate, amount):
                    return "payment amount is %s (rate: %s) under travel rule threshold %s" % (
                        amount,
                        info.to_xdx_exchange_rate,
                        limit,
                    )

    async def validate_currency_code(
        self, currency: str, currencies: typing.Optional[typing.List[jsonrpc.CurrencyInfo]] = None
    ) -> None:
        if currencies is None:
            currencies = await self.jsonrpc_client.get_currencies()
        currency_codes = list(map(lambda c: c.code, currencies))
        supported_codes = _filter_supported_currency_codes(self.supported_currency_codes, currency_codes)
        if currency not in currency_codes:
            raise InvalidCurrencyCodeError(f"currency code is invalid: {currency}")
        if currency not in supported_codes:
            raise UnsupportedCurrencyCodeError(f"currency code is not supported: {currency}")

    def validate_addresses(self, payment: PaymentObject, request_sender_address: str) -> None:
        self.validate_actor_address("sender", payment.sender)
        self.validate_actor_address("receiver", payment.receiver)
        self.validate_request_sender_address(request_sender_address, [payment.sender.address, payment.receiver.address])

    def validate_actor_address(self, actor_name: str, actor: PaymentActorObject) -> None:
        try:
            identifier.decode_account(actor.address, self.hrp)
        except ValueError as e:
            raise command_error(
                ErrorCode.invalid_field_value,
                f"could not decode account identifier: {e}",
                f"command.payment.{actor_name}.address",
            ) from e

    def validate_request_sender_address(self, request_sender_address: str, addresses: typing.List[str]) -> None:
        if request_sender_address not in addresses:
            raise command_error(
                ErrorCode.invalid_http_header,
                f"address {request_sender_address} is not one of {addresses}",
            )

    async def create_inbound_payment_command(self, cid: str, obj: PaymentObject) -> PaymentCommand:
        if await self.is_my_account_id(obj.sender.address):
            return PaymentCommand(cid=cid, my_actor_address=obj.sender.address, payment=obj, inbound=True)
        if await self.is_my_account_id(obj.receiver.address):
            return PaymentCommand(cid=cid, my_actor_address=obj.receiver.address, payment=obj, inbound=True)

        raise command_error(ErrorCode.unknown_address, "unknown actor addresses: {obj}")

    async def is_my_account_id(self, account_id: str) -> bool:
        account_address, _ = identifier.decode_account(account_id, self.hrp)
        if self.my_compliance_key_account_id == self.account_id(account_address):
            return True
        account = await self.jsonrpc_client.get_account(account_address)
        if account and account.role.parent_vasp_address:
            return self.my_compliance_key_account_id == self.account_id(account.role.parent_vasp_address)
        return False

    def account_id(self, address: typing.Union[diem_types.AccountAddress, bytes, str]) -> str:
        return identifier.encode_account(utils.account_address(address), None, self.hrp)

    async def get_base_url_and_compliance_key(self, account_id: str) -> typing.Tuple[str, Ed25519PublicKey]:
        account_address, _ = identifier.decode_account(account_id, self.hrp)
        return await self.jsonrpc_client.get_base_url_and_compliance_key(account_address)


def _filter_supported_currency_codes(
    supported_codes: typing.Optional[typing.List[str]], codes: typing.List[str]
) -> typing.List[str]:
    return list(filter(lambda code: supported_codes is None or code in supported_codes, codes))


def _deserialize_jws(
    content_bytes: bytes,
    klass: typing.Type[T],
    public_key: Ed25519PublicKey,
) -> T:
    try:
        return jws.deserialize(content_bytes, klass, public_key.verify)
    except JSONDecodeError as e:
        raise protocol_error(ErrorCode.invalid_json, f"decode json string failed: {e}", None) from e
    except FieldError as e:
        raise protocol_error(e.code, f"invalid {klass.__name__} json: {e}", e.field) from e
    except InvalidSignature as e:
        raise protocol_error(ErrorCode.invalid_jws_signature, f"invalid jws signature: {e}", None) from e
    except ValueError as e:
        raise protocol_error(ErrorCode.invalid_jws, f"deserialize JWS bytes failed: {e}", None) from e


def deserialize_command(command: typing.Dict[str, typing.Any], klass: typing.Type[T]) -> T:
    try:
        return from_dict(command, klass, field_path="command")
    except FieldError as e:
        raise command_error(e.code, f"invalid {klass.__name__} json: {e}", e.field) from e


def _is_under_the_threshold(limit: int, rate: float, amount: int) -> bool:
    # use ceil of the xdx exchanged amount to ensure a valid amount
    # is not considered as under the threshold because of float number
    # transport or calculation difference comparing with Move module
    # implementation.
    return math.ceil(rate * amount) < limit
