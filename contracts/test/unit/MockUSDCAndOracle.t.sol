// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Base} from "../utils/Base.t.sol";
import {MockUSDC} from "src/MockUSDC.sol";
import {PriceOracle} from "src/PriceOracle.sol";

contract MockUSDCTest is Base {
    function test_faucet_mintsAndRateLimits() public {
        address a = makeAddr("faucet-user");
        assertEq(usdc.decimals(), 6);
        vm.expectEmit(true, false, false, true);
        emit MockUSDC.FaucetClaimed(a, 1_000e6);
        vm.prank(a);
        usdc.faucet();
        assertEq(usdc.balanceOf(a), 1_000e6);

        vm.expectRevert(abi.encodeWithSelector(MockUSDC.FaucetCooldown.selector, T0 + 1 days));
        vm.prank(a);
        usdc.faucet();

        vm.warp(T0 + 1 days); // exactly at cooldown end is allowed
        vm.prank(a);
        usdc.faucet();
        assertEq(usdc.balanceOf(a), 2_000e6);
    }
}

contract PriceOracleTest is Base {
    function _push(uint128 price, uint64 ts) internal {
        vm.prank(pricer);
        oracle.updatePrice(price, ts);
    }

    function test_update_andLatest() public {
        vm.expectEmit(true, true, true, true);
        emit PriceOracle.PriceUpdated(150_000, T0);
        _push(150_000, uint64(T0));
        (uint256 p, uint256 ts) = oracle.latest();
        assertEq(p, 150_000);
        assertEq(ts, T0);
        assertTrue(oracle.isFresh());
    }

    function test_latest_revertsWhenEmpty_andNotFresh() public {
        vm.expectRevert(PriceOracle.NoPrice.selector);
        oracle.latest();
        assertFalse(oracle.isFresh());
    }

    function test_update_reverts() public {
        vm.expectRevert();
        vm.prank(buyer);
        oracle.updatePrice(1, uint64(T0));

        vm.startPrank(pricer);
        vm.expectRevert(PriceOracle.ZeroPrice.selector);
        oracle.updatePrice(0, uint64(T0));
        vm.expectRevert(abi.encodeWithSelector(PriceOracle.TimestampInFuture.selector, T0 + 1));
        oracle.updatePrice(1, uint64(T0 + 1));
        oracle.updatePrice(1, uint64(T0));
        vm.expectRevert(abi.encodeWithSelector(PriceOracle.TimestampNotIncreasing.selector, T0, T0));
        oracle.updatePrice(2, uint64(T0));
        vm.stopPrank();
    }

    function test_staleness() public {
        _push(1, uint64(T0));
        vm.warp(T0 + 5 minutes);
        assertTrue(oracle.isFresh());
        vm.warp(T0 + 5 minutes + 1);
        assertFalse(oracle.isFresh());
    }

    function test_setMaxStaleness() public {
        vm.startPrank(admin);
        vm.expectEmit(true, true, true, true);
        emit PriceOracle.MaxStalenessUpdated(5 minutes, 1 hours);
        oracle.setMaxStaleness(1 hours);
        assertEq(oracle.maxStaleness(), 1 hours);
        vm.expectRevert(abi.encodeWithSelector(PriceOracle.InvalidStaleness.selector, 9));
        oracle.setMaxStaleness(9);
        vm.expectRevert(abi.encodeWithSelector(PriceOracle.InvalidStaleness.selector, 1 days + 1));
        oracle.setMaxStaleness(1 days + 1);
        vm.stopPrank();

        vm.expectRevert();
        vm.prank(buyer);
        oracle.setMaxStaleness(60);
    }

    function test_constructor_revertsZeroAdmin() public {
        vm.expectRevert(PriceOracle.ZeroAddress.selector);
        new PriceOracle(address(0));
    }
}
