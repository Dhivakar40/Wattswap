// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Base} from "../utils/Base.t.sol";
import {ExchangeHandler} from "./handlers/ExchangeHandler.sol";
import {OrderLib} from "src/libraries/OrderLib.sol";

contract ExchangeInvariantTest is Base {
    ExchangeHandler internal handler;

    uint256 internal initialWatt;
    uint256 internal initialUsdc;
    address[] internal actors;

    function setUp() public override {
        super.setUp();

        // Four actors: buyer, seller, buyer2, seller2 (the first two come from Base).
        (address buyer2, uint256 buyer2Pk) = makeAddrAndKey("buyer2");
        (address seller2, uint256 seller2Pk) = makeAddrAndKey("seller2");
        actors.push(buyer);
        actors.push(seller);
        actors.push(buyer2);
        actors.push(seller2);
        uint256[] memory pks = new uint256[](4);
        pks[0] = buyerPk;
        pks[1] = sellerPk;
        pks[2] = buyer2Pk;
        pks[3] = seller2Pk;

        deal(address(watt), seller2, 100_000, true);
        deal(address(usdc), buyer2, 1_000e6, true);
        vm.prank(seller2);
        watt.approve(address(exchange), type(uint256).max);
        vm.prank(buyer2);
        usdc.approve(address(exchange), type(uint256).max);

        initialWatt = watt.totalSupply();
        initialUsdc = usdc.totalSupply();

        handler = new ExchangeHandler(
            ExchangeHandler.Config({
                exchange: exchange,
                watt: watt,
                usdc: usdc,
                relayer: relayer,
                minter: minter,
                treasury: treasury,
                pricerPk: pricerPk,
                devicePk: devicePk,
                market: MARKET,
                meterId: METER,
                meterOwner: seller
            }),
            actors,
            pks
        );

        targetContract(address(handler));
        bytes4[] memory selectors = new bytes4[](5);
        selectors[0] = ExchangeHandler.settle.selector;
        selectors[1] = ExchangeHandler.cancel.selector;
        selectors[2] = ExchangeHandler.cancelUpTo.selector;
        selectors[3] = ExchangeHandler.mintReading.selector;
        selectors[4] = ExchangeHandler.warp.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: selectors}));
    }

    /// WATT is conserved by settlement: supply only grows via signed mints, and every unit is
    /// held by a known actor (the exchange never custodies anything).
    function invariant_wattConserved() public view {
        assertEq(watt.totalSupply(), initialWatt + handler.ghostWattMinted());
        uint256 sum;
        for (uint256 i = 0; i < actors.length; i++) {
            sum += watt.balanceOf(actors[i]);
        }
        assertEq(sum, watt.totalSupply());
        assertEq(watt.balanceOf(address(exchange)), 0);
    }

    /// mUSDC is conserved: payments + fees only move value between actors and the treasury.
    function invariant_usdcConserved() public view {
        uint256 sum = usdc.balanceOf(treasury);
        for (uint256 i = 0; i < actors.length; i++) {
            sum += usdc.balanceOf(actors[i]);
        }
        assertEq(sum, initialUsdc);
        assertEq(usdc.totalSupply(), initialUsdc);
        assertEq(usdc.balanceOf(address(exchange)), 0);
    }

    /// No order is ever filled beyond its signed amount.
    function invariant_noOverfill() public view {
        uint256 n = handler.orderCount();
        for (uint256 i = 0; i < n; i++) {
            (OrderLib.Order memory o, bytes32 h) = handler.orderAt(i);
            assertLe(exchange.filled(h), o.amountWh);
        }
    }

    /// Fee math: treasury receives exactly the fees, fees never exceed cost x feeBps cap, and
    /// the seller side never underflows (cost - fee >= 0 by construction).
    function invariant_feeMath() public view {
        assertEq(usdc.balanceOf(treasury), handler.ghostFees());
        assertLe(handler.ghostFees(), handler.ghostCost());
        assertLe(handler.ghostFees() * 10_000, handler.ghostCost() * exchange.MAX_FEE_BPS());
    }

    /// Sum of fills seen on both sides of the book is symmetric (each trade fills both orders).
    function invariant_fillsSymmetric() public view {
        uint256 n = handler.orderCount();
        uint256 buySide;
        uint256 sellSide;
        for (uint256 i = 0; i < n; i++) {
            (OrderLib.Order memory o, bytes32 h) = handler.orderAt(i);
            if (o.side == OrderLib.BUY) buySide += exchange.filled(h);
            else sellSide += exchange.filled(h);
        }
        assertEq(buySide, sellSide);
        assertEq(buySide, handler.ghostTradedWh());
    }

    /// Sanity: the handler can actually drive successful settlements (so the invariants above
    /// are not passing vacuously because every call reverts).
    function test_handlerReachesSuccessfulSettlement() public {
        handler.settle(0, 0, type(uint256).max / 3, 0);
        handler.settle(0, 0, 1_000, 1);
        assertGt(handler.settles(), 0);
        assertGt(handler.ghostTradedWh(), 0);
        assertGt(handler.ghostFees(), 0);
        invariant_wattConserved();
        invariant_usdcConserved();
        invariant_feeMath();
        invariant_fillsSymmetric();
    }
}
