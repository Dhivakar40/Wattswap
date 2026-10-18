// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {Base} from "../utils/Base.t.sol";
import {MockSmartWallet} from "../utils/MockSmartWallet.sol";
import {WattSwapExchange} from "src/WattSwapExchange.sol";
import {OrderLib} from "src/libraries/OrderLib.sol";
import {IPriceOracle} from "src/interfaces/IPriceOracle.sol";

contract WattSwapExchangeTest is Base {
    uint256 internal constant EXEC = 150_000;

    OrderLib.Order internal buy;
    OrderLib.Order internal sell;

    function setUp() public override {
        super.setUp();
        buy = _order(buyer, OrderLib.BUY, 10_000, 160_000, 1);
        sell = _order(seller, OrderLib.SELL, 10_000, 140_000, 1);
    }

    function _call(
        OrderLib.Order memory b,
        bytes memory bSig,
        OrderLib.Order memory s,
        bytes memory sSig,
        uint256 fill,
        uint256 exec,
        OrderLib.PriceQuote memory q,
        bytes memory qSig
    ) internal {
        vm.prank(relayer);
        exchange.settle(b, bSig, s, sSig, fill, exec, q, qSig);
    }

    // ------------------------------------------------------------------ happy paths

    function test_settle_movesBothLegsAtomically() public {
        // 10 kWh x 0.15 = 1.5 mUSDC; fee 1% = 0.015 mUSDC
        bytes32 bh = exchange.hashOrder(buy);
        bytes32 sh = exchange.hashOrder(sell);
        vm.expectEmit(true, true, true, true);
        emit WattSwapExchange.TradeSettled(
            MARKET, bh, sh, buyer, seller, 10_000, EXEC, 1_500_000, 15_000
        );
        _settle(buy, sell, 10_000, EXEC);

        assertEq(watt.balanceOf(buyer), 10_000);
        assertEq(watt.balanceOf(seller), 90_000);
        assertEq(usdc.balanceOf(buyer), 1_000e6 - 1_500_000);
        assertEq(usdc.balanceOf(seller), 1_500_000 - 15_000);
        assertEq(usdc.balanceOf(treasury), 15_000);
        assertEq(exchange.filled(bh), 10_000);
        assertEq(exchange.filled(sh), 10_000);
        assertEq(exchange.remaining(buy), 0);
        assertEq(usdc.balanceOf(address(exchange)), 0);
        assertEq(watt.balanceOf(address(exchange)), 0);
    }

    function test_settle_partialFills() public {
        _settle(buy, sell, 4_000, EXEC);
        assertEq(exchange.remaining(buy), 6_000);
        _settle(buy, sell, 6_000, EXEC);
        assertEq(exchange.remaining(sell), 0);
        assertEq(watt.balanceOf(buyer), 10_000);
    }

    function test_settle_zeroFee() public {
        vm.prank(admin);
        exchange.setFee(0);
        _settle(buy, sell, 10_000, EXEC);
        assertEq(usdc.balanceOf(treasury), 0);
        assertEq(usdc.balanceOf(seller), 1_500_000);
    }

    function test_settle_priceAtLimitsAndBandEdges() public {
        // buy limit == sell limit == exec, and exec exactly at +5% band edge
        OrderLib.Order memory b = _order(buyer, OrderLib.BUY, 1_000, 157_500, 2);
        OrderLib.Order memory s = _order(seller, OrderLib.SELL, 1_000, 157_500, 2);
        _settle(b, s, 1_000, 157_500); // 150_000 * 1.05
    }

    function test_settle_smartContractWalletMaker() public {
        MockSmartWallet w = new MockSmartWallet(seller);
        deal(address(watt), address(w), 50_000, true);
        w.approve(IERC20(address(watt)), address(exchange), type(uint256).max);
        OrderLib.Order memory s = _order(address(w), OrderLib.SELL, 5_000, 140_000, 9);
        // wallet validates signatures made by `seller`'s key
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, s);
        _call(buy, bSig, s, sSig, 5_000, EXEC, q, qSig);
        assertEq(watt.balanceOf(buyer), 5_000);
    }

    function test_settle_smartWalletBadSignatureRejected() public {
        MockSmartWallet w = new MockSmartWallet(seller);
        OrderLib.Order memory s = _order(address(w), OrderLib.SELL, 5_000, 140_000, 9);
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(buyerPk, s); // wrong key
        bytes32 h = exchange.hashOrder(s);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.InvalidOrderSignature.selector, h));
        _call(buy, bSig, s, sSig, 5_000, EXEC, q, qSig);
    }

    // ------------------------------------------------------------------ revert paths

    function _expectSettleRevert(bytes memory err, uint256 fill, uint256 exec) internal {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        vm.expectRevert(err);
        _call(buy, bSig, sell, sSig, fill, exec, q, qSig);
    }

    function test_revert_notRelayer() public {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector,
                buyer,
                exchange.RELAYER_ROLE()
            )
        );
        vm.prank(buyer);
        exchange.settle(buy, bSig, sell, sSig, 1_000, EXEC, q, qSig);
    }

    function test_revert_paused() public {
        vm.prank(admin);
        exchange.pause();
        _expectSettleRevert(abi.encodeWithSelector(Pausable.EnforcedPause.selector), 1_000, EXEC);
        vm.prank(admin);
        exchange.unpause();
        _settle(buy, sell, 1_000, EXEC);
    }

    function test_revert_invalidSide() public {
        buy.side = OrderLib.SELL;
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.InvalidSide.selector), 1_000, EXEC
        );
        buy.side = OrderLib.BUY;
        sell.side = OrderLib.BUY;
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.InvalidSide.selector), 1_000, EXEC
        );
    }

    function test_revert_marketMismatch() public {
        sell.marketId = keccak256("OTHER");
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.MarketMismatch.selector), 1_000, EXEC
        );
    }

    function test_revert_marketDisabled() public {
        vm.prank(admin);
        exchange.setMarket(MARKET, false);
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.MarketDisabled.selector, MARKET), 1_000, EXEC
        );
    }

    function test_revert_selfTrade() public {
        sell.maker = buyer;
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.SelfTrade.selector), 1_000, EXEC
        );
    }

    function test_revert_zeroFill() public {
        _expectSettleRevert(abi.encodeWithSelector(WattSwapExchange.ZeroFill.selector), 0, EXEC);
    }

    function test_revert_priceOutOfLimits() public {
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.PriceOutOfLimits.selector, 139_999),
            1_000,
            139_999
        );
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.PriceOutOfLimits.selector, 160_001),
            1_000,
            160_001
        );
    }

    function test_revert_expired_boundary() public {
        // exactly at expiry is valid, one second later is not
        vm.warp(buy.expiry);
        _settle(buy, sell, 1_000, EXEC);
        vm.warp(buy.expiry + 1);
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        bytes32 h = exchange.hashOrder(buy);
        vm.expectRevert(
            abi.encodeWithSelector(WattSwapExchange.OrderExpired.selector, h, buy.expiry)
        );
        _call(buy, bSig, sell, sSig, 1_000, EXEC, q, qSig);
    }

    function test_revert_cancelledOrder() public {
        vm.prank(buyer);
        exchange.cancel(buy);
        bytes32 h = exchange.hashOrder(buy);
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.OrderIsCancelled.selector, h), 1_000, EXEC
        );
    }

    function test_revert_nonceBelowFloor() public {
        vm.prank(seller);
        exchange.cancelUpTo(2);
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.NonceTooLow.selector, 1, 2), 1_000, EXEC
        );
    }

    function test_revert_invalidSignature() public {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bad = _sign(sellerPk, buy); // signed by the wrong key
        bytes memory sSig = _sign(sellerPk, sell);
        bytes32 h = exchange.hashOrder(buy);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.InvalidOrderSignature.selector, h));
        _call(buy, bad, sell, sSig, 1_000, EXEC, q, qSig);
    }

    function test_revert_signatureForTamperedOrder() public {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        sell.limitPrice = 1; // relayer lowers the seller's limit after signing
        bytes32 h = exchange.hashOrder(sell);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.InvalidOrderSignature.selector, h));
        _call(buy, bSig, sell, sSig, 1_000, EXEC, q, qSig);
    }

    function test_revert_overfill() public {
        _settle(buy, sell, 6_000, EXEC);
        bytes32 h = exchange.hashOrder(buy);
        _expectSettleRevert(
            abi.encodeWithSelector(WattSwapExchange.Overfill.selector, h, 4_000, 4_001), 4_001, EXEC
        );
    }

    function test_revert_overfillSellSide() public {
        OrderLib.Order memory small = _order(seller, OrderLib.SELL, 500, 140_000, 3);
        bytes32 h = exchange.hashOrder(small);
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, small);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.Overfill.selector, h, 500, 501));
        _call(buy, bSig, small, sSig, 501, EXEC, q, qSig);
    }

    function test_revert_dustFill() public {
        // 1 Wh at 0.15 mUSDC/kWh = 150 micro-mUSDC... use price 999 -> 0.999 micro -> floors to 0
        OrderLib.Order memory b = _order(buyer, OrderLib.BUY, 1, 999, 5);
        OrderLib.Order memory s = _order(seller, OrderLib.SELL, 1, 999, 5);
        OrderLib.PriceQuote memory q = _quote(999);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, b);
        bytes memory sSig = _sign(sellerPk, s);
        vm.expectRevert(WattSwapExchange.DustFill.selector);
        _call(b, bSig, s, sSig, 1, 999, q, qSig);
    }

    function test_revert_quoteMarketMismatch() public {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        q.marketId = keccak256("OTHER");
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        vm.expectRevert(WattSwapExchange.QuoteMarketMismatch.selector);
        _call(buy, bSig, sell, sSig, 1_000, EXEC, q, qSig);
    }

    function test_revert_quoteFromFuture() public {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        q.timestamp = block.timestamp + 1;
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        vm.expectRevert(
            abi.encodeWithSelector(WattSwapExchange.QuoteFromFuture.selector, q.timestamp)
        );
        _call(buy, bSig, sell, sSig, 1_000, EXEC, q, qSig);
    }

    function test_revert_staleQuote_boundary() public {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);

        vm.warp(block.timestamp + 5 minutes); // exactly maxStaleness: still fresh
        _call(buy, bSig, sell, sSig, 1_000, EXEC, q, qSig);

        vm.warp(block.timestamp + 1); // now 5m+1s old
        vm.expectRevert(
            abi.encodeWithSelector(WattSwapExchange.StaleQuote.selector, q.timestamp, 5 minutes)
        );
        _call(buy, bSig, sell, sSig, 1_000, EXEC, q, qSig);
    }

    function test_revert_quoteSignerNotPricer() public {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        bytes memory qSig = _signQuote(sellerPk, q); // valid signature, but no PRICER_ROLE
        vm.expectRevert(WattSwapExchange.InvalidQuoteSigner.selector);
        _call(buy, bSig, sell, sSig, 1_000, EXEC, q, qSig);
    }

    function test_revert_quoteGarbageSignature() public {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        vm.expectRevert(WattSwapExchange.InvalidQuoteSigner.selector);
        _call(buy, bSig, sell, sSig, 1_000, EXEC, q, hex"deadbeef");
    }

    function test_revert_quoteTampered() public {
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        q.price = 151_000; // changed after signing: recovers a random address
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        vm.expectRevert(WattSwapExchange.InvalidQuoteSigner.selector);
        _call(buy, bSig, sell, sSig, 1_000, EXEC, q, qSig);
    }

    function test_revert_priceOutsideBand() public {
        // quote 150_000, band 5% -> max exec 157_500; wide limits so only the band blocks
        OrderLib.Order memory b = _order(buyer, OrderLib.BUY, 1_000, 200_000, 7);
        OrderLib.Order memory s = _order(seller, OrderLib.SELL, 1_000, 100_000, 7);
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, b);
        bytes memory sSig = _sign(sellerPk, s);
        vm.expectRevert(
            abi.encodeWithSelector(WattSwapExchange.PriceOutsideBand.selector, 157_501, QUOTE_PRICE)
        );
        _call(b, bSig, s, sSig, 1_000, 157_501, q, qSig);
        vm.expectRevert(
            abi.encodeWithSelector(WattSwapExchange.PriceOutsideBand.selector, 142_499, QUOTE_PRICE)
        );
        _call(b, bSig, s, sSig, 1_000, 142_499, q, qSig);
    }

    function test_revert_insufficientAllowanceOrBalance_isFullyAtomic() public {
        vm.prank(buyer);
        usdc.approve(address(exchange), 0); // payment leg will fail AFTER the WATT leg ran
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        vm.expectRevert();
        _call(buy, bSig, sell, sSig, 10_000, EXEC, q, qSig);

        // nothing moved, nothing recorded
        assertEq(watt.balanceOf(buyer), 0);
        assertEq(watt.balanceOf(seller), 100_000);
        assertEq(usdc.balanceOf(buyer), 1_000e6);
        assertEq(exchange.filled(exchange.hashOrder(sell)), 0);
    }

    function test_revert_feeLegFailure_rollsBackEverything() public {
        // allowance covers the seller payment (1.485 mUSDC) but not the fee leg on top
        vm.prank(buyer);
        usdc.approve(address(exchange), 1_485_000);
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, buy);
        bytes memory sSig = _sign(sellerPk, sell);
        vm.expectRevert();
        _call(buy, bSig, sell, sSig, 10_000, EXEC, q, qSig);
        assertEq(usdc.balanceOf(seller), 0);
        assertEq(watt.balanceOf(buyer), 0);
    }

    // ------------------------------------------------------------------ cancel

    function test_cancel_emitsAndBlocksFurtherFills() public {
        bytes32 h = exchange.hashOrder(sell);
        vm.expectEmit(true, true, true, true);
        emit WattSwapExchange.OrderCancelled(seller, h);
        vm.prank(seller);
        exchange.cancel(sell);
        assertTrue(exchange.cancelled(h));
    }

    function test_cancel_reverts() public {
        vm.expectRevert(WattSwapExchange.NotMaker.selector);
        vm.prank(buyer);
        exchange.cancel(sell);

        vm.startPrank(seller);
        exchange.cancel(sell);
        vm.expectRevert(
            abi.encodeWithSelector(
                WattSwapExchange.AlreadyCancelled.selector, exchange.hashOrder(sell)
            )
        );
        exchange.cancel(sell);
        vm.stopPrank();
    }

    function test_cancelUpTo() public {
        vm.startPrank(seller);
        vm.expectEmit(true, true, true, true);
        emit WattSwapExchange.NonceFloorRaised(seller, 5);
        exchange.cancelUpTo(5);
        assertEq(exchange.minNonce(seller), 5);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.NonceNotIncreasing.selector, 5, 5));
        exchange.cancelUpTo(5);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.NonceNotIncreasing.selector, 4, 5));
        exchange.cancelUpTo(4);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------ admin + views

    function test_admin_setters() public {
        vm.startPrank(admin);
        vm.expectEmit(true, true, true, true);
        emit WattSwapExchange.BandUpdated(BAND_BPS, 300);
        exchange.setBand(300);
        assertEq(exchange.bandBps(), 300);

        vm.expectEmit(true, true, true, true);
        emit WattSwapExchange.FeeUpdated(FEE_BPS, 250);
        exchange.setFee(250);
        assertEq(exchange.feeBps(), 250);

        address t2 = makeAddr("t2");
        vm.expectEmit(true, true, true, true);
        emit WattSwapExchange.TreasuryUpdated(treasury, t2);
        exchange.setTreasury(t2);
        assertEq(exchange.treasury(), t2);

        vm.expectEmit(true, true, true, true);
        emit WattSwapExchange.MarketUpdated(keccak256("X"), true);
        exchange.setMarket(keccak256("X"), true);
        assertTrue(exchange.marketEnabled(keccak256("X")));
        vm.stopPrank();
    }

    function test_admin_bandChangeAffectsSettlement() public {
        vm.prank(admin);
        exchange.setBand(1); // 0.01%
        OrderLib.Order memory b = _order(buyer, OrderLib.BUY, 1_000, 200_000, 7);
        OrderLib.Order memory s = _order(seller, OrderLib.SELL, 1_000, 100_000, 7);
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        bytes memory qSig = _signQuote(pricerPk, q);
        bytes memory bSig = _sign(buyerPk, b);
        bytes memory sSig = _sign(sellerPk, s);
        vm.expectRevert(
            abi.encodeWithSelector(WattSwapExchange.PriceOutsideBand.selector, 150_016, QUOTE_PRICE)
        );
        _call(b, bSig, s, sSig, 1_000, 150_016, q, qSig);
        _call(b, bSig, s, sSig, 1_000, 150_015, q, qSig); // 15 / 150000 = 0.01%
    }

    function test_admin_reverts() public {
        vm.startPrank(admin);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.InvalidFee.selector, 501));
        exchange.setFee(501);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.InvalidBand.selector, 0));
        exchange.setBand(0);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.InvalidBand.selector, 2_001));
        exchange.setBand(2_001);
        vm.expectRevert(WattSwapExchange.ZeroAddress.selector);
        exchange.setTreasury(address(0));
        vm.stopPrank();

        bytes memory unauthorized = abi.encodeWithSelector(
            IAccessControl.AccessControlUnauthorizedAccount.selector, buyer, bytes32(0)
        );
        vm.startPrank(buyer);
        vm.expectRevert(unauthorized);
        exchange.setFee(1);
        vm.expectRevert(unauthorized);
        exchange.setBand(1);
        vm.expectRevert(unauthorized);
        exchange.setTreasury(buyer);
        vm.expectRevert(unauthorized);
        exchange.setMarket(MARKET, false);
        vm.expectRevert(unauthorized);
        exchange.pause();
        vm.expectRevert(unauthorized);
        exchange.unpause();
        vm.stopPrank();
    }

    function test_constructor_reverts() public {
        IERC20 w = IERC20(address(watt));
        IERC20 u = IERC20(address(usdc));
        IPriceOracle o = IPriceOracle(address(oracle));
        vm.expectRevert(WattSwapExchange.ZeroAddress.selector);
        new WattSwapExchange(address(0), w, u, o, treasury, 1, 1);
        vm.expectRevert(WattSwapExchange.ZeroAddress.selector);
        new WattSwapExchange(admin, IERC20(address(0)), u, o, treasury, 1, 1);
        vm.expectRevert(WattSwapExchange.ZeroAddress.selector);
        new WattSwapExchange(admin, w, IERC20(address(0)), o, treasury, 1, 1);
        vm.expectRevert(WattSwapExchange.ZeroAddress.selector);
        new WattSwapExchange(admin, w, u, IPriceOracle(address(0)), treasury, 1, 1);
        vm.expectRevert(WattSwapExchange.ZeroAddress.selector);
        new WattSwapExchange(admin, w, u, o, address(0), 1, 1);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.InvalidFee.selector, 501));
        new WattSwapExchange(admin, w, u, o, treasury, 501, 1);
        vm.expectRevert(abi.encodeWithSelector(WattSwapExchange.InvalidBand.selector, 0));
        new WattSwapExchange(admin, w, u, o, treasury, 1, 0);
    }

    function test_views_matchIndependentDigests() public view {
        assertEq(exchange.hashOrder(buy), _typedDigest(address(exchange), OrderLib.orderHash(buy)));
        OrderLib.PriceQuote memory q = _quote(QUOTE_PRICE);
        assertEq(exchange.hashQuote(q), _typedDigest(address(exchange), OrderLib.quoteHash(q)));
        assertTrue(exchange.domainSeparator() != bytes32(0));
    }

    function test_marketIdIsPartOfOrderHash() public view {
        OrderLib.Order memory other = buy;
        other.marketId = keccak256("OTHER");
        assertTrue(exchange.hashOrder(buy) != exchange.hashOrder(other));
    }
}
