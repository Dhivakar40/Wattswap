// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {Base} from "../utils/Base.t.sol";
import {MeterRegistry} from "src/MeterRegistry.sol";

contract MeterRegistryTest is Base {
    bytes32 internal constant M2 = keccak256("meter-2");

    function test_register_storesAndEmits() public {
        vm.expectEmit(true, true, true, true);
        emit MeterRegistry.MeterRegistered(M2, buyer, device, 3_000, -33_000_000, 151_000_000);
        vm.prank(registrar);
        registry.registerMeter(M2, buyer, device, 3_000, -33_000_000, 151_000_000);

        MeterRegistry.Meter memory m = registry.getMeter(M2);
        assertEq(m.owner, buyer);
        assertEq(m.deviceSigner, device);
        assertEq(m.ratedCapacityW, 3_000);
        assertEq(m.latE6, -33_000_000);
        assertEq(m.lonE6, 151_000_000);
        assertTrue(m.active);
    }

    function test_register_revertsWithoutRole() public {
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector,
                buyer,
                registry.REGISTRAR_ROLE()
            )
        );
        vm.prank(buyer);
        registry.registerMeter(M2, buyer, device, 3_000, 0, 0);
    }

    function test_register_revertsZeroOwnerOrSigner() public {
        vm.startPrank(registrar);
        vm.expectRevert(MeterRegistry.ZeroAddress.selector);
        registry.registerMeter(M2, address(0), device, 3_000, 0, 0);
        vm.expectRevert(MeterRegistry.ZeroAddress.selector);
        registry.registerMeter(M2, buyer, address(0), 3_000, 0, 0);
        vm.stopPrank();
    }

    function test_register_revertsZeroCapacity() public {
        vm.prank(registrar);
        vm.expectRevert(MeterRegistry.ZeroCapacity.selector);
        registry.registerMeter(M2, buyer, device, 0, 0, 0);
    }

    function test_register_revertsBadLocation() public {
        vm.startPrank(registrar);
        vm.expectRevert(MeterRegistry.InvalidLocation.selector);
        registry.registerMeter(M2, buyer, device, 1, 90_000_001, 0);
        vm.expectRevert(MeterRegistry.InvalidLocation.selector);
        registry.registerMeter(M2, buyer, device, 1, -90_000_001, 0);
        vm.expectRevert(MeterRegistry.InvalidLocation.selector);
        registry.registerMeter(M2, buyer, device, 1, 0, 180_000_001);
        vm.expectRevert(MeterRegistry.InvalidLocation.selector);
        registry.registerMeter(M2, buyer, device, 1, 0, -180_000_001);
        // boundaries are valid
        registry.registerMeter(M2, buyer, device, 1, 90_000_000, -180_000_000);
        vm.stopPrank();
    }

    function test_register_revertsDuplicate() public {
        vm.prank(registrar);
        vm.expectRevert(
            abi.encodeWithSelector(MeterRegistry.MeterAlreadyRegistered.selector, METER)
        );
        registry.registerMeter(METER, buyer, device, 1, 0, 0);
    }

    function test_rotateSigner() public {
        address newKey = makeAddr("newKey");
        vm.expectEmit(true, true, true, true);
        emit MeterRegistry.DeviceSignerRotated(METER, device, newKey);
        vm.prank(registrar);
        registry.rotateDeviceSigner(METER, newKey);
        assertEq(registry.getMeter(METER).deviceSigner, newKey);
    }

    function test_rotateSigner_reverts() public {
        vm.startPrank(registrar);
        vm.expectRevert(MeterRegistry.ZeroAddress.selector);
        registry.rotateDeviceSigner(METER, address(0));
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterNotFound.selector, M2));
        registry.rotateDeviceSigner(M2, device);
        vm.stopPrank();

        vm.expectRevert();
        vm.prank(buyer);
        registry.rotateDeviceSigner(METER, device);
    }

    function test_setActive_andNotFound() public {
        vm.startPrank(registrar);
        registry.setActive(METER, false);
        assertFalse(registry.getMeter(METER).active);
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterNotFound.selector, M2));
        registry.setActive(M2, true);
        vm.stopPrank();
    }

    function test_updateCapacity() public {
        vm.startPrank(registrar);
        registry.updateCapacity(METER, 9_000);
        assertEq(registry.getMeter(METER).ratedCapacityW, 9_000);
        vm.expectRevert(MeterRegistry.ZeroCapacity.selector);
        registry.updateCapacity(METER, 0);
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterNotFound.selector, M2));
        registry.updateCapacity(M2, 1);
        vm.stopPrank();
    }

    function test_getMeter_revertsUnknown() public {
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterNotFound.selector, M2));
        registry.getMeter(M2);
    }

    // ------------------------------------------------------------ recordReading

    function _record(bytes32 id, uint256 wh, uint256 start, uint256 secs) internal {
        vm.prank(address(watt));
        registry.recordReading(id, wh, start, secs);
    }

    function test_maxEnergyWh_formula() public view {
        // 5 kW x 900 s x 120% / 3600 = 1500 Wh
        assertEq(registry.maxEnergyWh(5_000, 900), 1_500);
    }

    function test_recordReading_ok_andBoundary() public {
        vm.expectEmit(true, true, true, true);
        emit MeterRegistry.IntervalRecorded(METER, T0, T0 + 900);
        vm.prank(address(watt));
        (address o, address s) = registry.recordReading(METER, 1_500, T0, 900);
        assertEq(o, seller);
        assertEq(s, device);
        assertEq(registry.lastIntervalEnd(METER), T0 + 900);
    }

    function test_recordReading_requiresRecorderRole() public {
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector,
                buyer,
                registry.RECORDER_ROLE()
            )
        );
        vm.prank(buyer);
        registry.recordReading(METER, 1, T0, 900);
    }

    function test_recordReading_plausibilityUsesOwnIntervalLength() public {
        // 5 kW: 60 s -> 100 Wh, 3600 s -> 6000 Wh (not the 900 s default)
        _record(METER, 100, T0, 60);
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.ReadingImplausible.selector, 101, 100));
        _record(METER, 101, T0 + 60, 60);
        _record(METER, 6_000, T0 + 60, 3600);
        vm.expectRevert(
            abi.encodeWithSelector(MeterRegistry.ReadingImplausible.selector, 6_001, 6_000)
        );
        _record(METER, 6_001, T0 + 3_660, 3600);
    }

    function test_recordReading_revertsImplausible() public {
        vm.expectRevert(
            abi.encodeWithSelector(MeterRegistry.ReadingImplausible.selector, 1_501, 1_500)
        );
        _record(METER, 1_501, T0, 900);
    }

    function test_recordReading_revertsBadInterval() public {
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.InvalidInterval.selector, 0));
        _record(METER, 0, T0, 0); // zero-length: intervalEnd == intervalStart
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.InvalidInterval.selector, 59));
        _record(METER, 1, T0, 59);
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.InvalidInterval.selector, 1 days + 1));
        _record(METER, 1, T0, 1 days + 1);
    }

    function test_recordReading_revertsUnknownAndInactive() public {
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterNotFound.selector, M2));
        _record(M2, 1, T0, 900);

        vm.prank(registrar);
        registry.setActive(METER, false);
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterInactive.selector, METER));
        _record(METER, 1, T0, 900);
    }

    function test_interval_overlapRejected() public {
        _record(METER, 100, T0, 900);
        // starts one second before the previous interval ended
        vm.expectRevert(
            abi.encodeWithSelector(
                MeterRegistry.IntervalOverlap.selector, METER, T0 + 899, T0 + 900
            )
        );
        _record(METER, 100, T0 + 899, 900);
        // identical interval
        vm.expectRevert(
            abi.encodeWithSelector(MeterRegistry.IntervalOverlap.selector, METER, T0, T0 + 900)
        );
        _record(METER, 100, T0, 900);
    }

    function test_interval_equalBoundaryAllowed() public {
        _record(METER, 100, T0, 900);
        _record(METER, 100, T0 + 900, 900); // start == previous end
        assertEq(registry.lastIntervalEnd(METER), T0 + 1_800);
    }

    function test_interval_gapAllowed() public {
        _record(METER, 100, T0, 900);
        _record(METER, 100, T0 + 5_000, 900);
        assertEq(registry.lastIntervalEnd(METER), T0 + 5_900);
    }

    function test_interval_outOfOrderRejected() public {
        _record(METER, 100, T0 + 1_800, 900);
        vm.expectRevert(
            abi.encodeWithSelector(MeterRegistry.IntervalOverlap.selector, METER, T0, T0 + 2_700)
        );
        _record(METER, 100, T0, 900); // an earlier, non-overlapping interval is still too late
    }

    function test_interval_isPerMeter() public {
        vm.prank(registrar);
        registry.registerMeter(M2, buyer, device, 5_000, 0, 0);
        _record(METER, 100, T0, 900);
        _record(M2, 100, T0, 900); // same interval on another meter is fine
        assertEq(registry.lastIntervalEnd(M2), T0 + 900);
    }

    function test_interval_failedRecordLeavesStateUnchanged() public {
        _record(METER, 100, T0, 900);
        vm.expectRevert();
        _record(METER, 99_999, T0 + 900, 900); // implausible
        assertEq(registry.lastIntervalEnd(METER), T0 + 900);
    }

    function test_setPlausibility() public {
        vm.startPrank(admin);
        vm.expectEmit(true, true, true, true);
        emit MeterRegistry.PlausibilityUpdated(10_000, 30, 7_200);
        registry.setPlausibility(10_000, 30, 7_200);
        assertEq(registry.safetyBps(), 10_000);
        assertEq(registry.minIntervalSeconds(), 30);
        assertEq(registry.maxIntervalSeconds(), 7_200);

        vm.expectRevert(MeterRegistry.InvalidPlausibilityConfig.selector);
        registry.setPlausibility(0, 30, 60);
        vm.expectRevert(MeterRegistry.InvalidPlausibilityConfig.selector);
        registry.setPlausibility(20_001, 30, 60);
        vm.expectRevert(MeterRegistry.InvalidPlausibilityConfig.selector);
        registry.setPlausibility(10_000, 0, 60);
        vm.expectRevert(MeterRegistry.InvalidPlausibilityConfig.selector);
        registry.setPlausibility(10_000, 61, 60);
        vm.stopPrank();

        vm.expectRevert();
        vm.prank(buyer);
        registry.setPlausibility(10_000, 30, 60);
    }

    function test_constructor_revertsZeroAdmin() public {
        vm.expectRevert(MeterRegistry.ZeroAddress.selector);
        new MeterRegistry(address(0));
    }
}
