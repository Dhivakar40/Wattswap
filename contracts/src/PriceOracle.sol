// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/// @title PriceOracle
/// @notice Latest reference price (mUSDC per kWh, 6 decimals) pushed by the pricing engine.
contract PriceOracle is AccessControl {
    bytes32 public constant PRICER_ROLE = keccak256("PRICER_ROLE");

    uint256 public constant MIN_STALENESS = 10 seconds;
    uint256 public constant MAX_STALENESS = 1 days;

    struct Price {
        uint128 price;
        uint64 timestamp;
    }

    Price private _latest;
    uint256 public maxStaleness = 5 minutes;

    event PriceUpdated(uint256 price, uint256 timestamp);
    event MaxStalenessUpdated(uint256 oldValue, uint256 newValue);

    error ZeroAddress();
    error ZeroPrice();
    error TimestampNotIncreasing(uint256 given, uint256 last);
    error TimestampInFuture(uint256 given);
    error InvalidStaleness(uint256 value);
    error NoPrice();

    constructor(address admin) {
        if (admin == address(0)) revert ZeroAddress();
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
    }

    function updatePrice(uint128 price, uint64 timestamp) external onlyRole(PRICER_ROLE) {
        if (price == 0) revert ZeroPrice();
        if (timestamp <= _latest.timestamp) {
            revert TimestampNotIncreasing(timestamp, _latest.timestamp);
        }
        if (timestamp > block.timestamp) revert TimestampInFuture(timestamp);
        _latest = Price(price, timestamp);
        emit PriceUpdated(price, timestamp);
    }

    function setMaxStaleness(uint256 value) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (value < MIN_STALENESS || value > MAX_STALENESS) revert InvalidStaleness(value);
        emit MaxStalenessUpdated(maxStaleness, value);
        maxStaleness = value;
    }

    function latest() external view returns (uint256 price, uint256 timestamp) {
        Price memory p = _latest;
        if (p.timestamp == 0) revert NoPrice();
        return (p.price, p.timestamp);
    }

    function isFresh() external view returns (bool) {
        uint256 ts = _latest.timestamp;
        return ts != 0 && block.timestamp - ts <= maxStaleness;
    }
}
