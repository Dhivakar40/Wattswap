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

    function test_validateReading_ok_andBoundary() public view {
        // 5 kW x 900 s x 120% / 3600 = 1500 Wh
        assertEq(registry.maxEnergyWh(5_000, 900), 1_500);
        (address o, address s) = registry.validateReading(METER, 1_500, 900);
        assertEq(o, seller);
        assertEq(s, device);
    }

    function test_validateReading_reverts() public {
        vm.expectRevert(
            abi.encodeWithSelector(MeterRegistry.ReadingImplausible.selector, 1_501, 1_500)
        );
        registry.validateReading(METER, 1_501, 900);

        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.InvalidInterval.selector, 59));
        registry.validateReading(METER, 1, 59);
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.InvalidInterval.selector, 1 days + 1));
        registry.validateReading(METER, 1, 1 days + 1);

        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterNotFound.selector, M2));
        registry.validateReading(M2, 1, 900);

        vm.prank(registrar);
        registry.setActive(METER, false);
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterInactive.selector, METER));
        registry.validateReading(METER, 1, 900);
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
