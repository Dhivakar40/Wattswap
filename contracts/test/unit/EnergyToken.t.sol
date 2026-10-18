// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {Base} from "../utils/Base.t.sol";
import {EnergyToken} from "src/EnergyToken.sol";
import {MeterRegistry} from "src/MeterRegistry.sol";
import {IMeterRegistry} from "src/interfaces/IMeterRegistry.sol";
import {MockSmartWallet} from "../utils/MockSmartWallet.sol";

contract EnergyTokenTest is Base {
    bytes32 internal constant R1 = keccak256("reading-1");

    function _mint(EnergyToken.Reading memory r, bytes memory sig) internal {
        vm.prank(minter);
        watt.mint(r, sig);
    }

    function test_metadata() public view {
        assertEq(watt.decimals(), 3);
        assertEq(watt.symbol(), "WATT");
    }

    function test_mint_happyPath() public {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        uint256 before_ = watt.balanceOf(seller);
        vm.expectEmit(true, true, true, true);
        emit EnergyToken.ReadingMinted(R1, METER, seller, 1_000, r.intervalStart, r.intervalSeconds);
        _mint(r, _signReading(devicePk, r));
        assertEq(watt.balanceOf(seller), before_ + 1_000);
        assertTrue(watt.readingMinted(R1));
    }

    function test_mint_digestMatchesContract() public view {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
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
        assertEq(watt.hashReading(r), _typedDigest(address(watt), structHash));
    }

    function test_mint_revertsWithoutMinterRole() public {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        bytes memory sig = _signReading(devicePk, r);
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector, buyer, watt.MINTER_ROLE()
            )
        );
        vm.prank(buyer);
        watt.mint(r, sig);
    }

    function test_mint_revertsWrongSigner() public {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        bytes memory sig = _signReading(sellerPk, r); // owner signs, not the device
        vm.expectRevert(EnergyToken.InvalidDeviceSignature.selector);
        _mint(r, sig);
    }

    function test_mint_revertsTamperedReading() public {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        bytes memory sig = _signReading(devicePk, r);
        r.amountWh = 1_001;
        vm.expectRevert(EnergyToken.InvalidDeviceSignature.selector);
        _mint(r, sig);
    }

    function test_mint_revertsMalformedSignature() public {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        vm.expectRevert(EnergyToken.InvalidDeviceSignature.selector);
        _mint(r, hex"1234");
    }

    function test_mint_rotatedKey_oldRejected_newAccepted() public {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        bytes memory oldSig = _signReading(devicePk, r);

        (address newDevice, uint256 newPk) = makeAddrAndKey("device-v2");
        vm.prank(registrar);
        registry.rotateDeviceSigner(METER, newDevice);

        vm.expectRevert(EnergyToken.InvalidDeviceSignature.selector);
        _mint(r, oldSig);

        _mint(r, _signReading(newPk, r));
        assertEq(watt.balanceOf(seller), 100_000 + 1_000);
    }

    function test_mint_revertsReplayedReadingId() public {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        bytes memory sig = _signReading(devicePk, r);
        _mint(r, sig);

        vm.expectRevert(abi.encodeWithSelector(EnergyToken.ReadingAlreadyMinted.selector, R1));
        _mint(r, sig);

        // same readingId with a different payload (validly signed) is also rejected
        EnergyToken.Reading memory r2 = _reading(R1, 500);
        bytes memory sig2 = _signReading(devicePk, r2);
        vm.expectRevert(abi.encodeWithSelector(EnergyToken.ReadingAlreadyMinted.selector, R1));
        _mint(r2, sig2);
    }

    function test_mint_revertsZeroAmount() public {
        EnergyToken.Reading memory r = _reading(R1, 0);
        bytes memory sig = _signReading(devicePk, r);
        vm.expectRevert(EnergyToken.ZeroAmount.selector);
        _mint(r, sig);
    }

    function test_mint_revertsIntervalNotComplete() public {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        r.intervalStart = block.timestamp - 10 minutes; // ends 5 min in the future
        bytes memory sig = _signReading(devicePk, r);
        vm.expectRevert(
            abi.encodeWithSelector(
                EnergyToken.ReadingNotComplete.selector, r.intervalStart + r.intervalSeconds
            )
        );
        _mint(r, sig);
    }

    function test_mint_revertsImplausibleAmount() public {
        EnergyToken.Reading memory r = _reading(R1, 1_501); // max is 1_500 for 15 min @ 5 kW
        bytes memory sig = _signReading(devicePk, r);
        vm.expectRevert(
            abi.encodeWithSelector(MeterRegistry.ReadingImplausible.selector, 1_501, 1_500)
        );
        _mint(r, sig);
    }

    function test_mint_revertsUnknownAndInactiveMeter() public {
        EnergyToken.Reading memory r = _reading(R1, 100);
        r.meterId = keccak256("ghost");
        bytes memory sig = _signReading(devicePk, r);
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterNotFound.selector, r.meterId));
        _mint(r, sig);

        EnergyToken.Reading memory r2 = _reading(R1, 100);
        sig = _signReading(devicePk, r2);
        vm.prank(registrar);
        registry.setActive(METER, false);
        vm.expectRevert(abi.encodeWithSelector(MeterRegistry.MeterInactive.selector, METER));
        _mint(r2, sig);
    }

    function test_mint_supportsContractWalletDevice() public {
        MockSmartWallet dev = new MockSmartWallet(device);
        vm.prank(registrar);
        registry.rotateDeviceSigner(METER, address(dev));
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        _mint(r, _signReading(devicePk, r));
        assertTrue(watt.readingMinted(R1));
    }

    function test_pause_blocksMintAndTransfers() public {
        EnergyToken.Reading memory r = _reading(R1, 1_000);
        bytes memory sig = _signReading(devicePk, r);
        vm.prank(admin);
        watt.pause();

        vm.expectRevert(Pausable.EnforcedPause.selector);
        _mint(r, sig);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vm.prank(seller);
        watt.transfer(buyer, 1);

        vm.prank(admin);
        watt.unpause();
        _mint(r, sig);
    }

    function test_pause_requiresRole() public {
        vm.expectRevert();
        vm.prank(buyer);
        watt.pause();
        vm.expectRevert();
        vm.prank(buyer);
        watt.unpause();
    }

    function test_constructor_revertsZero() public {
        vm.expectRevert(EnergyToken.ZeroAddress.selector);
        new EnergyToken(address(0), IMeterRegistry(address(registry)));
        vm.expectRevert(EnergyToken.ZeroAddress.selector);
        new EnergyToken(admin, IMeterRegistry(address(0)));
    }
}
