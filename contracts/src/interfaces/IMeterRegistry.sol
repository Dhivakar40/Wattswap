// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IMeterRegistry {
    /// @notice Validates that a reading is physically plausible for an active meter.
    /// @return owner The wallet that receives the minted energy.
    /// @return deviceSigner The key that must have signed the reading.
    function validateReading(bytes32 meterId, uint256 amountWh, uint256 intervalSeconds)
        external
        view
        returns (address owner, address deviceSigner);
}
