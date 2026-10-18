// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {EnergyToken} from "src/EnergyToken.sol";
import {MockUSDC} from "src/MockUSDC.sol";
import {MeterRegistry} from "src/MeterRegistry.sol";
import {PriceOracle} from "src/PriceOracle.sol";
import {WattSwapExchange} from "src/WattSwapExchange.sol";
import {OrderLib} from "src/libraries/OrderLib.sol";
import {IMeterRegistry} from "src/interfaces/IMeterRegistry.sol";
import {IPriceOracle} from "src/interfaces/IPriceOracle.sol";

/// @dev Shared deployment + EIP-712 signing helpers. Digests are computed independently of the
///      contracts' own hash helpers so a bug in either side is caught.
abstract contract Base is Test {
    bytes32 internal constant MARKET = keccak256("WATT-mUSDC");
    uint256 internal constant T0 = 1_800_000_000;

    uint256 internal constant FEE_BPS = 100; // 1%
    uint256 internal constant BAND_BPS = 500; // 5%
    uint256 internal constant QUOTE_PRICE = 150_000; // 0.15 mUSDC per kWh

    address internal admin = makeAddr("admin");
    address internal minter = makeAddr("minter");
    address internal registrar = makeAddr("registrar");
    address internal relayer = makeAddr("relayer");
    address internal treasury = makeAddr("treasury");

    uint256 internal pricerPk;
    address internal pricer;
    uint256 internal devicePk;
    address internal device;
    uint256 internal sellerPk;
    address internal seller;
    uint256 internal buyerPk;
    address internal buyer;

    EnergyToken internal watt;
    MockUSDC internal usdc;
    MeterRegistry internal registry;
    PriceOracle internal oracle;
    WattSwapExchange internal exchange;

    bytes32 internal constant METER = keccak256("meter-1");

    function setUp() public virtual {
        vm.warp(T0);
        (pricer, pricerPk) = makeAddrAndKey("pricer");
        (device, devicePk) = makeAddrAndKey("device");
        (seller, sellerPk) = makeAddrAndKey("seller");
        (buyer, buyerPk) = makeAddrAndKey("buyer");

        registry = new MeterRegistry(admin);
        watt = new EnergyToken(admin, IMeterRegistry(address(registry)));
        usdc = new MockUSDC();
        oracle = new PriceOracle(admin);
        exchange = new WattSwapExchange(
            admin,
            IERC20(address(watt)),
            IERC20(address(usdc)),
            IPriceOracle(address(oracle)),
            treasury,
            FEE_BPS,
            BAND_BPS
        );

        vm.startPrank(admin);
        registry.grantRole(registry.REGISTRAR_ROLE(), registrar);
        watt.grantRole(watt.MINTER_ROLE(), minter);
        oracle.grantRole(oracle.PRICER_ROLE(), pricer);
        exchange.grantRole(exchange.RELAYER_ROLE(), relayer);
        exchange.setMarket(MARKET, true);
        vm.stopPrank();

        vm.prank(registrar);
        registry.registerMeter(METER, seller, device, 5_000, 13_082_700, 80_270_700);

        // seller holds 100 kWh, buyer holds 1000 mUSDC; both approve the exchange
        deal(address(watt), seller, 100_000, true);
        deal(address(usdc), buyer, 1_000e6, true);
        vm.prank(seller);
        watt.approve(address(exchange), type(uint256).max);
        vm.prank(buyer);
        usdc.approve(address(exchange), type(uint256).max);
    }

    // ----------------------------------------------------------------- orders / quotes

    function _order(address maker, uint8 side, uint256 amountWh, uint256 limit, uint256 nonce)
        internal
        view
        returns (OrderLib.Order memory)
    {
        return OrderLib.Order({
            maker: maker,
            marketId: MARKET,
            side: side,
            amountWh: amountWh,
            limitPrice: limit,
            expiry: block.timestamp + 1 hours,
            nonce: nonce,
            salt: keccak256(abi.encode(maker, side, amountWh, limit, nonce))
        });
    }

    function _typedDigest(address verifying, bytes32 structHash) internal view returns (bytes32) {
        bytes32 domain = keccak256(
            abi.encode(
                keccak256(
                    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                ),
                keccak256(
                    bytes(verifying == address(exchange) ? "WattSwapExchange" : "WattSwap Energy")
                ),
                keccak256(bytes("1")),
                block.chainid,
                verifying
            )
        );
        return MessageHashUtils.toTypedDataHash(domain, structHash);
    }

    function _signHash(uint256 pk, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function _sign(uint256 pk, OrderLib.Order memory o) internal view returns (bytes memory) {
        return _signHash(pk, _typedDigest(address(exchange), OrderLib.orderHash(o)));
    }

    function _quote(uint256 price) internal view returns (OrderLib.PriceQuote memory) {
        return OrderLib.PriceQuote({marketId: MARKET, price: price, timestamp: block.timestamp});
    }

    function _signQuote(uint256 pk, OrderLib.PriceQuote memory q)
        internal
        view
        returns (bytes memory)
    {
        return _signHash(pk, _typedDigest(address(exchange), OrderLib.quoteHash(q)));
    }

    function _reading(bytes32 readingId, uint256 amountWh)
        internal
        view
        returns (EnergyToken.Reading memory)
    {
        return EnergyToken.Reading({
            meterId: METER,
            readingId: readingId,
            amountWh: amountWh,
            intervalStart: block.timestamp - 15 minutes,
            intervalSeconds: 15 minutes
        });
    }

    function _signReading(uint256 pk, EnergyToken.Reading memory r)
        internal
        view
        returns (bytes memory)
    {
        bytes32 structHash = keccak256(
            abi.encode(
                watt.READING_TYPEHASH(),
                r.meterId,
                r.readingId,
                r.amountWh,
                r.intervalStart,
                r.intervalSeconds
            )
        );
        return _signHash(pk, _typedDigest(address(watt), structHash));
    }

    /// @dev Full valid settle of `fill` Wh at `exec` between the default buyer and seller.
    function _settle(
        OrderLib.Order memory buy,
        OrderLib.Order memory sell,
        uint256 fill,
        uint256 exec
    ) internal {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        vm.prank(relayer);
        exchange.settle(
            buy,
            _sign(buyerPk, buy),
            sell,
            _sign(sellerPk, sell),
            fill,
            exec,
            q,
            _signQuote(pricerPk, q)
        );
    }
}
