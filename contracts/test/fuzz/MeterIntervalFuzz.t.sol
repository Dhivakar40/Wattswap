// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Base} from "../utils/Base.t.sol";
import {MeterRegistry} from "src/MeterRegistry.sol";
import {EnergyToken} from "src/EnergyToken.sol";

/// @dev Fuzzes the per-meter interval ordering rule (`intervalStart >= lastIntervalEnd`).
contract MeterIntervalFuzzTest is Base {
    function _record(uint256 wh, uint256 start, uint256 secs) internal {
        vm.prank(address(watt));
        registry.recordReading(METER, wh, start, secs);
    }

    /// Any interval that starts strictly inside an already-recorded one is rejected and
    /// leaves `lastIntervalEnd` untouched.
    function testFuzz_overlapAlwaysReverts(
        uint256 start1,
        uint256 secs1,
        uint256 secs2,
        uint256 back
    ) public {
        start1 = bound(start1, 0, type(uint64).max);
        secs1 = bound(secs1, 60, 1 days);
        secs2 = bound(secs2, 60, 1 days);
        _record(0, start1, secs1);
        uint256 end1 = start1 + secs1;
        back = bound(back, 1, end1); // start2 = end1 - back  < end1
        uint256 start2 = end1 - back;

        vm.expectRevert(
            abi.encodeWithSelector(MeterRegistry.IntervalOverlap.selector, METER, start2, end1)
        );
        _record(0, start2, secs2);
        assertEq(registry.lastIntervalEnd(METER), end1);
    }

    /// Adjacent (equal boundary) and gapped intervals are accepted and advance the cursor.
    function testFuzz_adjacentOrGappedAccepted(
        uint256 start1,
        uint256 secs1,
        uint256 gap,
        uint256 secs2
    ) public {
        start1 = bound(start1, 0, type(uint64).max);
        secs1 = bound(secs1, 60, 1 days);
        secs2 = bound(secs2, 60, 1 days);
        gap = bound(gap, 0, 365 days); // 0 = equal boundary
        _record(0, start1, secs1);
        uint256 start2 = start1 + secs1 + gap;
        _record(0, start2, secs2);
        assertEq(registry.lastIntervalEnd(METER), start2 + secs2);
    }

    /// Replaying an earlier interval after a later one is always rejected (no backdating).
    function testFuzz_outOfOrderAlwaysReverts(
        uint256 start1,
        uint256 secs1,
        uint256 secs2,
        uint256 gap
    ) public {
        start1 = bound(start1, 1 days, type(uint64).max);
        secs1 = bound(secs1, 60, 1 days);
        secs2 = bound(secs2, 60, 1 days);
        gap = bound(gap, 0, 365 days);
        _record(0, start1, secs1);
        _record(0, start1 + secs1 + gap, secs2);
        // an interval that sat entirely before the second one, in the old gap or earlier
        vm.expectRevert();
        _record(0, start1, secs1);
    }

    /// The plausibility ceiling follows the reading's own length, to the single Wh.
    function testFuzz_plausibilityUsesOwnInterval(uint256 secs, uint256 over) public {
        secs = bound(secs, 60, 1 days);
        uint256 maxWh = registry.maxEnergyWh(5_000, secs);
        assertEq(maxWh, 5_000 * secs * registry.safetyBps() / (3600 * registry.BPS()));
        over = bound(over, 1, 1_000_000);
        vm.expectRevert(
            abi.encodeWithSelector(MeterRegistry.ReadingImplausible.selector, maxWh + over, maxWh)
        );
        _record(maxWh + over, T0, secs);
        _record(maxWh, T0, secs); // the boundary itself is fine
    }

    /// Through the real mint path: a sequence of in-order readings mints exactly their sum.
    function testFuzz_mintInOrderSequence(uint256 a1, uint256 a2, uint256 gap) public {
        a1 = bound(a1, 1, 1_500);
        a2 = bound(a2, 1, 1_500);
        gap = bound(gap, 0, 1 hours);
        uint256 t = block.timestamp;
        uint256 s1 = t - 3 hours;
        uint256 s2 = s1 + 15 minutes + gap;
        EnergyToken.Reading memory r1 =
            EnergyToken.Reading(METER, keccak256("a"), a1, s1, 15 minutes);
        EnergyToken.Reading memory r2 =
            EnergyToken.Reading(METER, keccak256("b"), a2, s2, 15 minutes);
        uint256 before_ = watt.balanceOf(seller);
        vm.startPrank(minter);
        watt.mint(r1, _signReading(devicePk, r1));
        watt.mint(r2, _signReading(devicePk, r2));
        vm.stopPrank();
        assertEq(watt.balanceOf(seller), before_ + a1 + a2);
    }
}
