"""DEMO (testnet/Anvil only): two wallets place crossing orders against the running stack.

    make up && make deploy-local      # stack + contracts
    make sim                          # simulated meter mints WATT to Anvil account #5
    make trade-demo                   # this script

It plays both sides with the public Anvil dev keys #5 (seller, owns the simulated meter) and #6
(buyer). Everything goes through the public REST API and the real contracts: SIWE sign-in, the
approvals a real user would sign, EIP-712 orders, matching, the relayer's atomic settle() and the
listener's confirmation. Payment is the mock stablecoin mUSDC. No physical energy is delivered.
"""

import argparse
import json
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3
from web3.contract.contract import ContractFunction

from app.chain.orders import OrderData, order_digest

SELLER_KEY = "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba"  # Anvil #5
BUYER_KEY = "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e"  # Anvil #6
COOKIE = "wattswap_session"
MARKET_NAME = "WATT-mUSDC"


def siwe(client: httpx.Client, key: str, domain: str, chain_id: int) -> dict[str, str]:
    acct = Account.from_key(key)
    nonce = client.get("/api/v1/auth/nonce").json()["nonce"]
    now = datetime.now(UTC)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    message = (
        f"{domain} wants you to sign in with your Ethereum account:\n{acct.address}\n\n"
        f"Sign in to WattSwap (testnet demo).\n\nURI: http://{domain}\nVersion: 1\n"
        f"Chain ID: {chain_id}\nNonce: {nonce}\nIssued At: {now.strftime(fmt)}\n"
        f"Expiration Time: {datetime.fromtimestamp(now.timestamp() + 300, UTC).strftime(fmt)}"
    )
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    r = client.post("/api/v1/auth/login", json={"message": message, "signature": "0x" + sig})
    r.raise_for_status()
    cookie = (
        r.cookies.get(COOKIE) or r.headers["set-cookie"].split(f"{COOKIE}=", 1)[1].split(";", 1)[0]
    )
    return {"cookie": f"{COOKIE}={cookie}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--rpc", default="http://localhost:8545")
    ap.add_argument("--domain", default="localhost:3000", help="SIWE domain (SIWE_DOMAIN)")
    ap.add_argument("--wh", type=int, default=500, help="energy to trade, in Wh")
    ap.add_argument(
        "--spread-pct", type=int, default=10, help="limit prices straddle the quote by this"
    )
    args = ap.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    api = httpx.Client(base_url=args.api, timeout=30)
    seller, buyer = Account.from_key(SELLER_KEY), Account.from_key(BUYER_KEY)
    market = "0x" + Web3.keccak(text=MARKET_NAME).hex()

    cfg = api.get(f"/api/v1/markets/{market}/order-config").json()
    chain_id, exchange = int(cfg["chain_id"]), cfg["exchange_address"]
    abi_dir = Path(__file__).resolve().parents[3] / "packages" / "shared" / "src" / "abis" / "json"
    watt = w3.eth.contract(
        address=cfg["watt_address"], abi=json.loads((abi_dir / "EnergyToken.json").read_text())
    )
    usdc = w3.eth.contract(
        address=cfg["usdc_address"], abi=json.loads((abi_dir / "MockUSDC.json").read_text())
    )

    def send(fn: ContractFunction, key: str) -> None:
        acct = Account.from_key(key)
        tx = fn.build_transaction(
            {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address)}
        )
        h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
        if w3.eth.wait_for_transaction_receipt(h)["status"] != 1:
            raise SystemExit(f"transaction reverted: {fn}")

    price = api.get(f"/api/v1/markets/{market}/price")
    if price.status_code != 200:
        print("no engine price yet: is the pricing engine running? (make up; wait one tick)")
        return 1
    ref = int(price.json()["price"])
    sell_limit, buy_limit = (
        ref * (100 - args.spread_pct) // 100,
        ref * (100 + args.spread_pct) // 100,
    )
    cost_max = args.wh * buy_limit // 1000

    held = watt.functions.balanceOf(seller.address).call()
    if held < args.wh:
        print(f"seller (Anvil #5) holds {held} Wh of WATT, needs {args.wh}: run `make sim` first")
        return 1
    if usdc.functions.balanceOf(buyer.address).call() < cost_max:
        print("buyer needs mUSDC: claiming from the testnet faucet")
        send(usdc.functions.faucet(), BUYER_KEY)
    if watt.functions.allowance(seller.address, exchange).call() < args.wh:
        print("seller approves the exchange to move WATT (only when a trade settles)")
        send(watt.functions.approve(exchange, 2**255), SELLER_KEY)
    if usdc.functions.allowance(buyer.address, exchange).call() < cost_max:
        print("buyer approves the exchange to move mUSDC (only when a trade settles)")
        send(usdc.functions.approve(exchange, 2**255), BUYER_KEY)

    sessions = {
        "seller": siwe(api, SELLER_KEY, args.domain, chain_id),
        "buyer": siwe(api, BUYER_KEY, args.domain, chain_id),
    }
    before = (
        watt.functions.balanceOf(buyer.address).call(),
        usdc.functions.balanceOf(seller.address).call(),
    )

    def place(who: str, key: str, side: int, limit: int) -> str:
        acct = Account.from_key(key)
        data = OrderData(
            maker=acct.address.lower(), market_id=market, side=side, amount_wh=args.wh, limit_price=limit,
            expiry=int(time.time()) + 3600, nonce=int(time.time()), salt="0x" + secrets.token_hex(32),
        )  # fmt: skip
        digest = order_digest(chain_id, exchange, data)
        sig = Account._sign_hash(digest, key).signature.hex()
        body = {
            "maker": acct.address, "marketId": market, "side": side, "amountWh": str(args.wh),
            "limitPrice": str(limit), "expiry": str(data.expiry), "nonce": str(data.nonce),
            "salt": data.salt, "signature": "0x" + sig,
        }  # fmt: skip
        r = api.post("/api/v1/orders", json=body, headers=sessions[who])
        if r.status_code not in (200, 201):
            raise SystemExit(f"{who} order rejected: {r.status_code} {r.text}")
        print(
            f"{who}: {'sell' if side else 'buy'} {args.wh} Wh @ {limit / 1e6:.6f} mUSDC/kWh -> {r.json()['order_hash'][:14]}…"
        )
        return str(r.json()["order_hash"])

    print(
        f"engine reference price: {ref / 1e6:.6f} mUSDC/kWh (limits straddle it by {args.spread_pct}%)"
    )
    sell_hash = place("seller", SELLER_KEY, 1, sell_limit)
    buy_hash = place("buyer", BUYER_KEY, 0, buy_limit)

    deadline = time.time() + 120
    while time.time() < deadline:
        s = api.get(f"/api/v1/orders/{sell_hash}", headers=sessions["seller"]).json()
        b = api.get(f"/api/v1/orders/{buy_hash}", headers=sessions["buyer"]).json()
        print(f"  seller order: {s['status']:<17} buyer order: {b['status']}", end="\r")
        if s["status"] == "filled" and b["status"] == "filled":
            break
        time.sleep(1)
    else:
        print("\nnot settled in 120 s: check `make logs` and GET /api/v1/admin/trading/state")
        return 1
    trade = api.get("/api/v1/trades", headers=sessions["buyer"]).json()[0]
    after = (
        watt.functions.balanceOf(buyer.address).call(),
        usdc.functions.balanceOf(seller.address).call(),
    )
    print(f"\nSETTLED atomically in tx {trade['tx_hash']} (block {trade['block_number']})")
    print(
        f"  {int(trade['fill_wh'])} Wh at {int(trade['exec_price']) / 1e6:.6f} mUSDC/kWh; cost {int(trade['cost']) / 1e6:.6f} mUSDC, fee {int(trade['fee']) / 1e6:.6f}"
    )
    print(
        f"  buyer WATT: {before[0]} -> {after[0]} Wh   seller mUSDC: {before[1] / 1e6:.6f} -> {after[1] / 1e6:.6f}"
    )
    print("  (testnet demo: mock stablecoin, simulated meter, no physical energy delivery)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
