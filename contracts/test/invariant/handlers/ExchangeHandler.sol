// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {WattSwapExchange} from "src/WattSwapExchange.sol";
import {EnergyToken} from "src/EnergyToken.sol";
import {MockUSDC} from "src/MockUSDC.sol";
import {MeterRegistry} from "src/MeterRegistry.sol";
import {OrderLib} from "src/libraries/OrderLib.sol";

/// @dev Drives random-but-bounded protocol activity and keeps ghost accounting that the
///      invariant suite compares against on-chain state.
contract ExchangeHandler is Test {
    struct SignedOrder {
        OrderLib.Order order;
        bytes sig;
        bytes32 hash;
    }

    WattSwapExchange public immutable exchange;
    EnergyToken public immutable watt;
    MockUSDC public immutable usdc;

    address public immutable relayer;
    address public immutable minter;
    address public immutable treasury;
    uint256 public immutable pricerPk;
    uint256 public immutable devicePk;
    bytes32 public immutable market;
    bytes32 public immutable meterId;
    address public meterOwner;

    address[] public actors;
    uint256[] internal _pks;
    SignedOrder[] internal _buys;
    SignedOrder[] internal _sells;

    // ghost accounting
    uint256 public ghostWattMinted;
    uint256 public ghostCost;
    uint256 public ghostFees;
    uint256 public ghostTradedWh;
    uint256 public settles;
    uint256 public settleReverts;
    uint256 public mints;
    uint256 internal _readingCounter;

    struct Config {
        WattSwapExchange exchange;
        EnergyToken watt;
        MockUSDC usdc;
        address relayer;
        address minter;
        address treasury;
        uint256 pricerPk;
        uint256 devicePk;
        bytes32 market;
        bytes32 meterId;
        address meterOwner;
    }

    constructor(Config memory c, address[] memory actors_, uint256[] memory pks_) {
        exchange = c.exchange;
        watt = c.watt;
        usdc = c.usdc;
        relayer = c.relayer;
        minter = c.minter;
        treasury = c.treasury;
        pricerPk = c.pricerPk;
        devicePk = c.devicePk;
        market = c.market;
        meterId = c.meterId;
        meterOwner = c.meterOwner;
        actors = actors_;
        _pks = pks_;
        _seedOrders();
    }

    /// @dev Actors alternate buyer, seller, buyer, seller. Builds 3 signed orders per actor.
    function _seedOrders() private {
        for (uint256 i = 0; i < actors.length; i++) {
            bool isBuyer = i % 2 == 0;
            for (uint256 j = 0; j < 3; j++) {
                OrderLib.Order memory o = OrderLib.Order({
                    maker: actors[i],
                    marketId: market,
                    side: isBuyer ? OrderLib.BUY : OrderLib.SELL,
                    amountWh: 5_000 + 7_919 * (i + 1) * (j + 1),
                    limitPrice: isBuyer ? 150_000 + 25_000 * j : 100_000 + 20_000 * j,
                    expiry: block.timestamp + 30 days,
                    nonce: j,
                    salt: keccak256(abi.encode(i, j))
                });
                bytes32 h = exchange.hashOrder(o);
                (uint8 v, bytes32 r, bytes32 sg) = vm.sign(_pks[i], h);
                SignedOrder memory so = SignedOrder(o, abi.encodePacked(r, sg, v), h);
                if (isBuyer) _buys.push(so);
                else _sells.push(so);
            }
        }
    }

    function orderCount() external view returns (uint256) {
        return _buys.length + _sells.length;
    }

    function orderAt(uint256 i) external view returns (OrderLib.Order memory o, bytes32 h) {
        SignedOrder storage so = i < _buys.length ? _buys[i] : _sells[i - _buys.length];
        return (so.order, so.hash);
    }

    function settle(uint256 buyIdx, uint256 sellIdx, uint256 fillSeed, uint256 priceSeed) external {
        SignedOrder storage b = _buys[buyIdx % _buys.length];
        SignedOrder storage s = _sells[sellIdx % _sells.length];

        (uint256 quotePrice, uint256 exec) = _prices(b.order, s.order, priceSeed);
        uint256 fill = _fill(b.order, s.order, fillSeed);
        OrderLib.PriceQuote memory q =
            OrderLib.PriceQuote({marketId: market, price: quotePrice, timestamp: block.timestamp});
        bytes memory qSig = _signQuote(q);

        uint256 buyerBefore = usdc.balanceOf(b.order.maker);
        uint256 treasuryBefore = usdc.balanceOf(treasury);
        vm.prank(relayer);
        try exchange.settle(b.order, b.sig, s.order, s.sig, fill, exec, q, qSig) {
            settles++;
            ghostTradedWh += fill;
            ghostCost += buyerBefore - usdc.balanceOf(b.order.maker);
            ghostFees += usdc.balanceOf(treasury) - treasuryBefore;
        } catch {
            settleReverts++;
        }
    }

    /// @dev Quote anywhere in the crossing range; exec within the band around it.
    function _prices(OrderLib.Order storage b, OrderLib.Order storage s, uint256 seed)
        private
        view
        returns (uint256 quotePrice, uint256 exec)
    {
        uint256 lo = s.limitPrice;
        uint256 hi = b.limitPrice;
        if (lo > hi) (lo, hi) = (hi, lo);
        quotePrice = bound(seed, lo, hi);
        uint256 band = exchange.bandBps();
        exec = bound(
            uint256(keccak256(abi.encode(seed))),
            quotePrice * (10_000 - band) / 10_000,
            quotePrice * (10_000 + band) / 10_000
        );
    }

    /// @dev Up to the remaining size on both sides; occasionally a little more (Overfill path).
    function _fill(OrderLib.Order storage b, OrderLib.Order storage s, uint256 seed)
        private
        view
        returns (uint256)
    {
        uint256 maxFill = exchange.remaining(b);
        uint256 sRem = exchange.remaining(s);
        if (sRem < maxFill) maxFill = sRem;
        return bound(seed, 0, maxFill + (seed % 7 == 0 ? 3 : 0));
    }

    function _signQuote(OrderLib.PriceQuote memory q) private view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 sg) = vm.sign(pricerPk, exchange.hashQuote(q));
        return abi.encodePacked(r, sg, v);
    }

    function cancel(uint256 idx) external {
        uint256 n = _buys.length + _sells.length;
        SignedOrder storage so =
            idx % n < _buys.length ? _buys[idx % n] : _sells[idx % n - _buys.length];
        if (exchange.cancelled(so.hash)) return;
        vm.prank(so.order.maker);
        exchange.cancel(so.order);
    }

    function cancelUpTo(uint256 actorIdx, uint256 nonce) external {
        address a = actors[actorIdx % actors.length];
        uint256 floor = exchange.minNonce(a);
        if (floor >= 3) return;
        nonce = bound(nonce, floor + 1, 3);
        vm.prank(a);
        exchange.cancelUpTo(nonce);
    }

    function mintReading(uint256 amountSeed) external {
        uint256 amount = bound(amountSeed, 1, 1_500); // 15 min @ 5 kW @ 120%
        // Intervals must be minted in order and be complete: continue from the meter's last
        // interval end, or skip this call if the next 15-minute slot has not finished yet.
        uint256 start = MeterRegistry(address(watt.registry())).lastIntervalEnd(meterId);
        if (start == 0) start = block.timestamp - 15 minutes;
        if (start + 15 minutes > block.timestamp) return;
        EnergyToken.Reading memory r = EnergyToken.Reading({
            meterId: meterId,
            readingId: keccak256(abi.encode("reading", ++_readingCounter)),
            amountWh: amount,
            intervalStart: start,
            intervalSeconds: 15 minutes
        });
        (uint8 v, bytes32 rr, bytes32 ss) = vm.sign(devicePk, watt.hashReading(r));
        vm.prank(minter);
        watt.mint(r, abi.encodePacked(rr, ss, v));
        ghostWattMinted += amount;
        mints++;
    }

    function warp(uint256 secs) external {
        vm.warp(block.timestamp + bound(secs, 1, 2 hours));
    }
}
