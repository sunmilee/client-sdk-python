# The official Diem Client SDK for Python.

[![pypi](https://img.shields.io/pypi/v/diem)](https://pypi.org/project/diem/)
![Apache V2 License](https://img.shields.io/pypi/l/diem)
![Python versoins](https://img.shields.io/pypi/pyversions/diem)

[API Reference](https://diem.github.io/client-sdk-python/diem)

## Pypi package

https://pypi.org/project/diem/

## Examples

```python3

>>> from diem.jsonrpc import AsyncClient
>>> from diem.testing import JSON_RPC_URL
>>> import asyncio
>>>
>>> async def main():
...     client = AsyncClient(JSON_RPC_URL)
...     print(await client.get_metadata())
...
>>> asyncio.run(main())

version: 3300304
timestamp: 1601492912847973
chain_id: 2
......

```

You can find more examples under the [`examples`](./examples/) directory:

* [Create Child VASP account](./examples/create_child_vasp.py)
* [Peer To Peer Transfer](./examples/p2p_transfer.py)
* [Intent Identifier](./examples/intent_identifier.py)

For building a wallet application, see [MiniWallet Application](./src/diem/testing/miniwallet/app) for example.


### Off-chain service example

Checkout [MiniWallet](./src/diem/testing/miniwallet/app) implementation for off-chain service implementation example.

## MiniWallet and MiniWallet Test Suite

See [mini_wallet.md](mini-wallet.md)


## Build & Test

```
make init
make test
```

run specific test:

```
make test t=<test file / test name match pattern>
```

run with local docker testnet (requires initializing diem submodule):

```
make test t=<test file / test name match pattern> dt=1
```

## Re-generate diem_types, stdlib, jsonrpc response data structures

```
git submodule update --init diem
cd diem
git pull origin main
cd ..
make gen
```

## Modules Overview

> SPEC = specification

> DIP-X = Diem Improvement Protocol

Root module name: `diem`

Sub-modules:

- `jsonrpc`: diem JSON-RPC APIs client and API response types. [SPEC](https://github.com/diem/diem/blob/master/json-rpc/json-rpc-spec.md)
- `stdlib`: generated code, move stdlib script utils for constructing transaction script playload.
- `diem_types`: generated code, Diem on-chain data structure types for encoding and decoding [BCS](https://crates.io/crates/bcs) data.
- `utils`: utility functions, account address utils, currency code, hashing, hex encoding / decoding, transaction utils.
- `AuthKey` | `auth_key`: auth key utils
- `identifier`: Diem Account Identifier and Diem Intent Identifier. [DIP-5](https://dip.diem.com/dip-5/)
- `txnmetadata`: utils for creating peer to peer transaction metadata. [DIP-4](https://dip.diem.com/dip-4/)
- `testnet`: Testnet utility, minting coins, create Testnet client, chain id, Testnet JSON-RPC URL.
- `testing`: Testing utility, MiniWallet application, MiniWallet test suites, `LocalAccount` for managing local account keys and generating random local account.
- `chain_ids`: list of static chain ids
