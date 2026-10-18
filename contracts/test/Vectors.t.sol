// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {EnergyToken} from "src/EnergyToken.sol";
import {WattSwapExchange} from "src/WattSwapExchange.sol";
import {OrderLib} from "src/libraries/OrderLib.sol";
import {IMeterRegistry} from "src/interfaces/IMeterRegistry.sol";
import {IPriceOracle} from "src/interfaces/IPriceOracle.sol";

/// @notice Generates and guards the shared EIP-712 test vectors consumed by the Python (Phase 4)
///         and TypeScript (Phase 6) code: packages/shared/vectors/eip712.json.
///
///         Regenerate:  WRITE_VECTORS=1 forge test --match-contract VectorsTest
///         Normal runs fail if the committed file differs from what the contracts produce.
///
///         Contracts are placed at FIXED addresses and every input is a constant, so output is
///         reproducible. All uint values are < 2^53 so they are valid plain JSON numbers.
///         Keys below are TEST-ONLY and publicly known.
contract VectorsTest is Test {
    string internal constant PATH = "../packages/shared/vectors/eip712.json";

    address internal constant EXCHANGE_ADDR = 0x00000000000000000000000000000000000E1001;
    address internal constant TOKEN_ADDR = 0x00000000000000000000000000000000000E1002;

    uint256 internal constant BUYER_PK = uint256(keccak256("wattswap-test-vector-buyer"));
    uint256 internal constant SELLER_PK = uint256(keccak256("wattswap-test-vector-seller"));
    uint256 internal constant PRICER_PK = uint256(keccak256("wattswap-test-vector-pricer"));
    uint256 internal constant DEVICE_PK = uint256(keccak256("wattswap-test-vector-device"));

    bytes32 internal constant MARKET = keccak256("WATT-mUSDC");
    bytes32 internal constant METER = keccak256("meter-1");

    WattSwapExchange internal exchange;
    EnergyToken internal token;

    function setUp() public {
        vm.chainId(31337);
        address a = address(0xA11CE);
        deployCodeTo(
            "WattSwapExchange.sol:WattSwapExchange",
            abi.encode(
                a,
                IERC20(address(0x1)),
                IERC20(address(0x2)),
                IPriceOracle(address(0x3)),
                a,
                uint256(100),
                uint256(500)
            ),
            EXCHANGE_ADDR
        );
        deployCodeTo(
            "EnergyToken.sol:EnergyToken", abi.encode(a, IMeterRegistry(address(0x4))), TOKEN_ADDR
        );
        exchange = WattSwapExchange(EXCHANGE_ADDR);
        token = EnergyToken(TOKEN_ADDR);
    }

    // ------------------------------------------------------------------ helpers

    function _domainSeparator(string memory name, address verifying)
        internal
        view
        returns (bytes32)
    {
        return keccak256(
            abi.encode(
                keccak256(
                    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                ),
                keccak256(bytes(name)),
                keccak256(bytes("1")),
                block.chainid,
                verifying
            )
        );
    }

    function _sig(uint256 pk, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function _domainJson(string memory name, address verifying)
        internal
        view
        returns (string memory)
    {
        return string.concat(
            '{"name":"',
            name,
            '","version":"1","chainId":',
            vm.toString(block.chainid),
            ',"verifyingContract":"',
            vm.toString(verifying),
            '"}'
        );
    }

    /// @dev One vector entry (everything except `message` and `primaryType` types is shared).
    function _entry(
        string memory name,
        string memory primaryType,
        string memory typesJson,
        string memory domainJson,
        string memory messageJson,
        bytes32 domainSep,
        bytes32 structHash,
        uint256 pk
    ) internal pure returns (string memory) {
        bytes32 digest = MessageHashUtils.toTypedDataHash(domainSep, structHash);
        string memory head = string.concat(
            '{"name":"',
            name,
            '","primaryType":"',
            primaryType,
            '","typedData":{"types":',
            typesJson
        );
        string memory mid = string.concat(
            ',"primaryType":"',
            primaryType,
            '","domain":',
            domainJson,
            ',"message":',
            messageJson,
            "}"
        );
        string memory hashes = string.concat(
            ',"domainSeparator":"',
            vm.toString(domainSep),
            '","structHash":"',
            vm.toString(structHash),
            '","digest":"',
            vm.toString(digest)
        );
        return string.concat(
            head,
            mid,
            hashes,
            '","signer":"',
            vm.toString(vm.addr(pk)),
            '","signature":"',
            vm.toString(_sig(pk, digest)),
            '"}'
        );
    }

    string internal constant DESCRIPTION =
        '"description":"WattSwap EIP-712 test vectors. Generated by contracts/test/Vectors.t.sol from the real contracts. TEST KEYS ONLY. All uints < 2^53 so they are plain JSON numbers; bytes32/address are 0x-hex strings. typedData is usable with eth_account.messages.encode_typed_data(full_message=...) and viem hashTypedData/verifyTypedData (convert numbers to bigint)."';

    string internal constant DOMAIN_TYPE =
        '"EIP712Domain":[{"name":"name","type":"string"},{"name":"version","type":"string"},{"name":"chainId","type":"uint256"},{"name":"verifyingContract","type":"address"}]';

    function _orderTypes() internal pure returns (string memory) {
        return string.concat(
            "{",
            DOMAIN_TYPE,
            ',"Order":[{"name":"maker","type":"address"},{"name":"marketId","type":"bytes32"},{"name":"side","type":"uint8"},{"name":"amountWh","type":"uint256"},{"name":"limitPrice","type":"uint256"},{"name":"expiry","type":"uint256"},{"name":"nonce","type":"uint256"},{"name":"salt","type":"bytes32"}]}'
        );
    }

    function _orderMessage(OrderLib.Order memory o) internal pure returns (string memory) {
        return string.concat(
            '{"maker":"',
            vm.toString(o.maker),
            '","marketId":"',
            vm.toString(o.marketId),
            '","side":',
            vm.toString(uint256(o.side)),
            ',"amountWh":',
            vm.toString(o.amountWh),
            ',"limitPrice":',
            vm.toString(o.limitPrice),
            ',"expiry":',
            vm.toString(o.expiry),
            ',"nonce":',
            vm.toString(o.nonce),
            ',"salt":"',
            vm.toString(o.salt),
            '"}'
        );
    }

    function _orderEntry(string memory name, OrderLib.Order memory o, uint256 pk)
        internal
        view
        returns (string memory)
    {
        bytes32 ds = _domainSeparator("WattSwapExchange", EXCHANGE_ADDR);
        assertEq(ds, exchange.domainSeparator(), "exchange domain separator");
        bytes32 sh = OrderLib.orderHash(o);
        assertEq(MessageHashUtils.toTypedDataHash(ds, sh), exchange.hashOrder(o), "order digest");
        return _entry(
            name,
            "Order",
            _orderTypes(),
            _domainJson("WattSwapExchange", EXCHANGE_ADDR),
            _orderMessage(o),
            ds,
            sh,
            pk
        );
    }

    function _quoteEntry() internal view returns (string memory) {
        return _quoteVector("price_quote", MARKET, 150_000, 1_800_000_000);
    }

    /// @dev One PriceQuote vector: digest cross-checked against the exchange's own `hashQuote`.
    ///      Values stay below 2^53 so the JSON numbers survive JavaScript parsing.
    function _quoteVector(string memory name, bytes32 market, uint256 price, uint256 timestamp)
        internal
        view
        returns (string memory)
    {
        OrderLib.PriceQuote memory q =
            OrderLib.PriceQuote({marketId: market, price: price, timestamp: timestamp});
        bytes32 ds = _domainSeparator("WattSwapExchange", EXCHANGE_ADDR);
        bytes32 sh = OrderLib.quoteHash(q);
        assertEq(MessageHashUtils.toTypedDataHash(ds, sh), exchange.hashQuote(q), "quote digest");
        string memory types = string.concat(
            "{",
            DOMAIN_TYPE,
            ',"PriceQuote":[{"name":"marketId","type":"bytes32"},{"name":"price","type":"uint256"},{"name":"timestamp","type":"uint256"}]}'
        );
        string memory message = string.concat(
            '{"marketId":"',
            vm.toString(q.marketId),
            '","price":',
            vm.toString(price),
            ',"timestamp":',
            vm.toString(timestamp),
            "}"
        );
        return _entry(
            name,
            "PriceQuote",
            types,
            _domainJson("WattSwapExchange", EXCHANGE_ADDR),
            message,
            ds,
            sh,
            PRICER_PK
        );
    }

    /// @dev Edge cases for the pricing engine's signer: smallest price, a typical night price,
    ///      the largest price that is a safe JSON integer, and a second market id.
    function _quoteEdgeEntries() internal view returns (string memory) {
        return string.concat(
            _quoteVector("price_quote_min_price", MARKET, 1, 1_800_000_001),
            ",",
            _quoteVector("price_quote_night", MARKET, 142_000, 1_800_003_600),
            ",",
            _quoteVector(
                "price_quote_max_safe_price", MARKET, 9_007_199_254_740_991, 9_007_199_254_740_991
            ),
            ",",
            _quoteVector(
                "price_quote_second_market", keccak256("WATT-mUSDC-2"), 150_000, 1_800_000_000
            )
        );
    }

    function _readingEntry() internal view returns (string memory) {
        EnergyToken.Reading memory r = EnergyToken.Reading({
            meterId: METER,
            readingId: keccak256("reading-1"),
            amountWh: 1_200,
            intervalStart: 1_799_999_100,
            intervalSeconds: 900
        });
        bytes32 ds = _domainSeparator("WattSwap Energy", TOKEN_ADDR);
        assertEq(ds, token.DOMAIN_SEPARATOR(), "token domain separator");
        bytes32 sh = keccak256(
            abi.encode(
                token.READING_TYPEHASH(),
                r.meterId,
                r.readingId,
                r.amountWh,
                r.intervalStart,
                r.intervalSeconds
            )
        );
        assertEq(MessageHashUtils.toTypedDataHash(ds, sh), token.hashReading(r), "reading digest");
        string memory types = string.concat(
            "{",
            DOMAIN_TYPE,
            ',"Reading":[{"name":"meterId","type":"bytes32"},{"name":"readingId","type":"bytes32"},{"name":"amountWh","type":"uint256"},{"name":"intervalStart","type":"uint256"},{"name":"intervalSeconds","type":"uint256"}]}'
        );
        string memory message = string.concat(
            '{"meterId":"',
            vm.toString(r.meterId),
            '","readingId":"',
            vm.toString(r.readingId),
            '","amountWh":1200,"intervalStart":1799999100,"intervalSeconds":900}'
        );
        return _entry(
            "meter_reading",
            "Reading",
            types,
            _domainJson("WattSwap Energy", TOKEN_ADDR),
            message,
            ds,
            sh,
            DEVICE_PK
        );
    }

    function _build() internal view returns (string memory) {
        OrderLib.Order memory buy = OrderLib.Order({
            maker: vm.addr(BUYER_PK),
            marketId: MARKET,
            side: OrderLib.BUY,
            amountWh: 10_000,
            limitPrice: 160_000,
            expiry: 1_800_003_600,
            nonce: 1,
            salt: keccak256("buy-salt")
        });
        OrderLib.Order memory sell = OrderLib.Order({
            maker: vm.addr(SELLER_PK),
            marketId: MARKET,
            side: OrderLib.SELL,
            amountWh: 10_000,
            limitPrice: 140_000,
            expiry: 1_800_003_600,
            nonce: 1,
            salt: keccak256("sell-salt")
        });

        string memory keys = string.concat(
            '"testKeys":{"buyer":"',
            vm.toString(bytes32(BUYER_PK)),
            '","seller":"',
            vm.toString(bytes32(SELLER_PK)),
            '","pricer":"',
            vm.toString(bytes32(PRICER_PK)),
            '","device":"',
            vm.toString(bytes32(DEVICE_PK)),
            '"}'
        );
        string memory vectors = string.concat(
            '"vectors":[',
            _orderEntry("order_buy", buy, BUYER_PK),
            ",",
            _orderEntry("order_sell", sell, SELLER_PK),
            ",",
            _quoteEntry(),
            ",",
            _readingEntry(),
            ",",
            _quoteEdgeEntries(),
            "]"
        );
        return string.concat("{", DESCRIPTION, ',"chainId":31337,', keys, ",", vectors, "}");
    }

    // ------------------------------------------------------------------ tests

    /// Regenerates the file when WRITE_VECTORS=1, otherwise verifies the committed file.
    function test_vectorsFileMatchesContracts() public {
        string memory json = _build();
        if (vm.envOr("WRITE_VECTORS", false)) {
            vm.writeFile(PATH, json);
            return;
        }
        assertEq(
            vm.readFile(PATH),
            json,
            "vectors out of date: run WRITE_VECTORS=1 forge test --match-contract VectorsTest"
        );
    }

    /// Every signature in the generated file recovers to its declared signer.
    function test_signaturesRecoverToSigners() public {
        string memory json = _build();
        string[8] memory names = [
            "order_buy",
            "order_sell",
            "price_quote",
            "meter_reading",
            "price_quote_min_price",
            "price_quote_night",
            "price_quote_max_safe_price",
            "price_quote_second_market"
        ];
        for (uint256 i = 0; i < 8; i++) {
            string memory base = string.concat(".vectors[", vm.toString(i), "]");
            assertEq(vm.parseJsonString(json, string.concat(base, ".name")), names[i]);
            bytes32 digest = vm.parseJsonBytes32(json, string.concat(base, ".digest"));
            bytes memory sig = vm.parseJsonBytes(json, string.concat(base, ".signature"));
            address signer = vm.parseJsonAddress(json, string.concat(base, ".signer"));
            assertEq(ECDSA.recover(digest, sig), signer);
        }
    }
}
