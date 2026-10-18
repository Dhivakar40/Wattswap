// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IMeterRegistry {
    /// @notice Validates a reading for an active meter and records its interval as covered.
    /// @dev State-changing and restricted to RECORDER_ROLE (the EnergyToken). Reverts unless the
    ///      interval starts at or after the meter's `lastIntervalEnd` (no overlap, in-order only),
    ///      then advances `lastIntervalEnd` to the end of this interval.
    /// @return owner The wallet that receives the minted energy.
    /// @return deviceSigner The key that must have signed the reading.
    function recordReading(
        bytes32 meterId,
        uint256 amountWh,
        uint256 intervalStart,
        uint256 intervalSeconds
    ) external returns (address owner, address deviceSigner);
}
