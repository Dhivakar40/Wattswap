// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Base} from "../utils/Base.t.sol";
import {EnergyToken} from "src/EnergyToken.sol";
import {MeterRegistry} from "src/MeterRegistry.sol";

/// @dev Fires valid, overlapping, out-of-order and implausible readings at the real mint path.
contract MeterIntervalHandler is Test {
    EnergyToken public immutable watt;
    MeterRegistry public immutable registry;
    address public immutable minter;
    uint256 public immutable devicePk;
    bytes32 public immutable meterId;

    uint256 public ghostMintedWh;
    uint256 public ghostCoveredSeconds;
    uint256 public ghostFirstStart;
    uint256 public ghostMaxWh; // sum of per-reading ceilings, computed independently
    uint256 public successes;
    uint256 public rejections;
    uint256 internal _n;

    constructor(
        EnergyToken watt_,
        MeterRegistry registry_,
        address minter_,
        uint256 devicePk_,
        bytes32 meterId_
    ) {
        watt = watt_;
        registry = registry_;
        minter = minter_;
        devicePk = devicePk_;
        meterId = meterId_;
    }

    function mint(uint256 modeSeed, uint256 offsetSeed, uint256 secsSeed, uint256 amountSeed)
        external
    {
        uint256 secs = bound(secsSeed, 60, 4 hours);
        uint256 last = registry.lastIntervalEnd(meterId);
        uint256 nowTs = block.timestamp;
        uint256 start;
        if (modeSeed % 3 == 0 && last != 0) {
            // overlap / out-of-order: anywhere from well before to just inside the last interval
            start = bound(offsetSeed, last > 3 days ? last - 3 days : 0, last == 0 ? 0 : last - 1);
        } else {
            // in order: at the cursor or after a gap
            start = (last == 0 ? nowTs - 6 hours : last) + bound(offsetSeed, 0, 2 hours)
                * (modeSeed % 2);
        }
        if (start + secs > nowTs) return; // interval not finished yet; warp first

        uint256 maxWh = registry.maxEnergyWh(5_000, secs);
        // sometimes ask for one Wh too much
        uint256 amount =
            modeSeed % 7 == 0 ? maxWh + 1 : bound(amountSeed, 1, maxWh == 0 ? 1 : maxWh);
        EnergyToken.Reading memory r = EnergyToken.Reading({
            meterId: meterId,
            readingId: keccak256(abi.encode("mi", ++_n)),
            amountWh: amount,
            intervalStart: start,
            intervalSeconds: secs
        });
        (uint8 v, bytes32 rr, bytes32 ss) = vm.sign(devicePk, watt.hashReading(r));
        vm.prank(minter);
        try watt.mint(r, abi.encodePacked(rr, ss, v)) {
            if (ghostCoveredSeconds == 0) ghostFirstStart = start;
            ghostMintedWh += amount;
            ghostCoveredSeconds += secs;
            ghostMaxWh += 5_000 * secs * 12_000 / (3600 * 10_000);
            successes++;
        } catch {
            rejections++;
        }
    }

    function warp(uint256 secs) external {
        vm.warp(block.timestamp + bound(secs, 1, 3 hours));
    }
}

contract MeterIntervalInvariantTest is Base {
    MeterIntervalHandler internal handler;
    uint256 internal initialSupply;

    function setUp() public override {
        super.setUp();
        initialSupply = watt.totalSupply();
        handler = new MeterIntervalHandler(watt, registry, minter, devicePk, METER);
        targetContract(address(handler));
    }

    /// Minted Wh never exceeds capacity x covered time x safety factor, where covered time is
    /// the sum of accepted interval lengths. Overlap would double-count time, but see below.
    function invariant_mintedNeverExceedsCapacityTimesCoveredTime() public view {
        uint256 ceiling = uint256(registry.getMeter(METER).ratedCapacityW)
            * handler.ghostCoveredSeconds() * registry.safetyBps() / (3600 * registry.BPS());
        assertLe(handler.ghostMintedWh(), ceiling);
        assertLe(handler.ghostMintedWh(), handler.ghostMaxWh());
        assertEq(watt.totalSupply(), initialSupply + handler.ghostMintedWh());
    }

    /// Covered time can never exceed the wall-clock span from the first interval to the
    /// cursor: accepted intervals are disjoint, so no stretch of time is paid for twice.
    function invariant_intervalsDisjoint() public view {
        if (handler.successes() == 0) return;
        assertLe(
            handler.ghostCoveredSeconds(),
            registry.lastIntervalEnd(METER) - handler.ghostFirstStart()
        );
        // therefore, end to end: minted <= capacity x elapsed span x safety
        uint256 span = registry.lastIntervalEnd(METER) - handler.ghostFirstStart();
        assertLe(
            handler.ghostMintedWh(),
            uint256(registry.getMeter(METER).ratedCapacityW) * span * registry.safetyBps()
                / (3600 * registry.BPS())
        );
    }

    /// Sanity: the handler exercises both accepted and rejected paths.
    function test_handlerReachesBothPaths() public {
        handler.mint(1, 0, 900, 100); // in-order first reading
        assertGt(handler.successes(), 0);
        handler.mint(0, 0, 900, 100); // mode%3==0 -> overlaps
        assertGt(handler.rejections(), 0);
        invariant_mintedNeverExceedsCapacityTimesCoveredTime();
        invariant_intervalsDisjoint();
    }
}
