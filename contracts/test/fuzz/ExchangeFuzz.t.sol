// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Base} from "../utils/Base.t.sol";
import {WattSwapExchange} from "src/WattSwapExchange.sol";
import {OrderLib} from "src/libraries/OrderLib.sol";

contract ExchangeFuzzTest is Base {
    struct Snap {
        uint256 wattSupply;
        uint256 usdcSupply;
        uint256 buyerUsdc;
        uint256 sellerWatt;
    }

    /// Any valid fill conserves value and splits cost exactly into seller proceeds + fee.
    function testFuzz_settle_conservesAndSplitsExactly(
        uint256 amount,
        uint256 fill,
        uint256 price,
        uint256 feeBps
    ) public {
        amount = bound(amount, 1, 100_000);
        fill = bound(fill, 1, amount);
        price = bound(price, 1_000, 5_000_000); // 0.001 .. 5 mUSDC / kWh
        feeBps = bound(feeBps, 0, exchange.MAX_FEE_BPS());
        uint256 cost = fill * price / 1_000;
        vm.assume(cost > 0);

        vm.prank(admin);
        exchange.setFee(feeBps);
        deal(address(usdc), buyer, 10_000_000e6, true);

        Snap memory before_ = Snap(
            watt.totalSupply(), usdc.totalSupply(), usdc.balanceOf(buyer), watt.balanceOf(seller)
        );
        _execFuzzTrade(amount, fill, price);

        uint256 fee = cost * feeBps / 10_000;
        assertEq(watt.totalSupply(), before_.wattSupply);
        assertEq(usdc.totalSupply(), before_.usdcSupply);
        assertEq(watt.balanceOf(buyer), fill);
        assertEq(watt.balanceOf(seller), before_.sellerWatt - fill);
        assertEq(usdc.balanceOf(buyer), before_.buyerUsdc - cost);
        assertEq(usdc.balanceOf(seller), cost - fee);
        assertEq(usdc.balanceOf(treasury), fee);
        assertLe(fee, cost);
    }

    /// Limits straddle price; quote == price so exec is exactly on the quote.
    function _execFuzzTrade(uint256 amount, uint256 fill, uint256 price) internal {
        OrderLib.Order memory b = _order(buyer, OrderLib.BUY, amount, price + 1, 1);
        OrderLib.Order memory s = _order(seller, OrderLib.SELL, amount, price - 1, 1);
        OrderLib.PriceQuote memory q = _quote(price);
        vm.prank(relayer);
        exchange.settle(
            b, _sign(buyerPk, b), s, _sign(sellerPk, s), fill, price, q, _signQuote(pricerPk, q)
        );
        assertEq(exchange.filled(exchange.hashOrder(b)), fill);
        assertEq(exchange.filled(exchange.hashOrder(s)), fill);
    }

    /// Sequential partial fills never exceed the order size, and the final fill is exact.
    function testFuzz_partialFills_neverOverfill(uint256 amount, uint256 f1, uint256 f2) public {
        amount = bound(amount, 2, 100_000);
        f1 = bound(f1, 1, amount - 1);
        OrderLib.Order memory b = _order(buyer, OrderLib.BUY, amount, 160_000, 1);
        OrderLib.Order memory s = _order(seller, OrderLib.SELL, amount, 140_000, 1);

        _settle(b, s, f1, 150_000);
        uint256 left = amount - f1;
        bytes32 bh = exchange.hashOrder(b);

        f2 = bound(f2, 1, left + 1_000);
        if (f2 > left) {
            OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
            bytes memory bs = _sign(buyerPk, b);
            bytes memory ss = _sign(sellerPk, s);
            bytes memory qs = _signQuote(pricerPk, q);
            vm.expectRevert(
                abi.encodeWithSelector(WattSwapExchange.Overfill.selector, bh, left, f2)
            );
            vm.prank(relayer);
            exchange.settle(b, bs, s, ss, f2, 150_000, q, qs);
        } else {
            _settle(b, s, f2, 150_000);
            assertEq(exchange.filled(bh), f1 + f2);
        }
        assertLe(exchange.filled(bh), amount);
    }

    /// @dev Signs everything with the right keys and calls settle as the relayer.
    function _go(
        OrderLib.Order memory b,
        OrderLib.Order memory s,
        OrderLib.PriceQuote memory q,
        uint256 fill,
        uint256 exec
    ) internal {
        bytes memory bs = _sign(buyerPk, b);
        bytes memory ss = _sign(sellerPk, s);
        bytes memory qs = _signQuote(pricerPk, q);
        vm.prank(relayer);
        exchange.settle(b, bs, s, ss, fill, exec, q, qs);
    }

    /// The band check accepts exactly the prices within +/- band of the quote.
    function testFuzz_band(uint256 quotePrice, uint256 exec, uint256 bandBps) public {
        quotePrice = bound(quotePrice, 10_000, 3_000_000);
        bandBps = bound(bandBps, 1, exchange.MAX_BAND_BPS());
        exec = bound(exec, quotePrice / 2, quotePrice * 2);
        vm.prank(admin);
        exchange.setBand(bandBps);

        OrderLib.Order memory b = _order(buyer, OrderLib.BUY, 1_000, type(uint128).max, 1);
        OrderLib.Order memory s = _order(seller, OrderLib.SELL, 1_000, 1, 1);
        OrderLib.PriceQuote memory q = _quote(quotePrice);

        uint256 diff = exec > quotePrice ? exec - quotePrice : quotePrice - exec;
        if (diff * 10_000 > quotePrice * bandBps) {
            vm.expectRevert(
                abi.encodeWithSelector(WattSwapExchange.PriceOutsideBand.selector, exec, quotePrice)
            );
        }
        _go(b, s, q, 1_000, exec);
    }

    /// Execution price outside [sell.limit, buy.limit] always reverts, never settles.
    function testFuzz_limits(uint256 buyLimit, uint256 sellLimit, uint256 exec) public {
        buyLimit = bound(buyLimit, 1_000, 1_000_000);
        sellLimit = bound(sellLimit, 1_000, 1_000_000);
        exec = bound(exec, 1_000, 1_000_000);
        OrderLib.Order memory b = _order(buyer, OrderLib.BUY, 1_000, buyLimit, 1);
        OrderLib.Order memory s = _order(seller, OrderLib.SELL, 1_000, sellLimit, 1);
        OrderLib.PriceQuote memory q = _quote(exec); // quote on exec so only limits can fail

        bool ok = exec >= sellLimit && exec <= buyLimit;
        if (!ok) {
            vm.expectRevert(
                abi.encodeWithSelector(WattSwapExchange.PriceOutOfLimits.selector, exec)
            );
        }
        _go(b, s, q, 1_000, exec);
        assertEq(watt.balanceOf(buyer), ok ? 1_000 : 0);
    }

    /// A signature never validates for a different order (any field flip invalidates it).
    function testFuzz_signatureBindsAllFields(uint256 field, uint256 delta) public {
        delta = bound(delta, 1, type(uint64).max);
        OrderLib.Order memory b = _order(buyer, OrderLib.BUY, 1_000, 160_000, 1);
        OrderLib.Order memory s = _order(seller, OrderLib.SELL, 1_000, 140_000, 1);
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory bs = _sign(buyerPk, b); // signed over the ORIGINAL fields
        bytes memory ss = _sign(sellerPk, s);
        bytes memory qs = _signQuote(pricerPk, q);

        field = field % 5;
        if (field == 0) b.amountWh += delta;
        else if (field == 1) b.limitPrice += delta;
        else if (field == 2) b.expiry += delta;
        else if (field == 3) b.nonce += delta;
        else b.salt = bytes32(uint256(b.salt) + delta);

        bytes32 h = exchange.hashOrder(b);
        vm.prank(relayer);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.InvalidOrderSignature.selector, h));
        exchange.settle(b, bs, s, ss, 1_000, 150_000, q, qs);
    }
}
