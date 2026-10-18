// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IPriceOracle {
    function PRICER_ROLE() external view returns (bytes32);
    function maxStaleness() external view returns (uint256);
    function hasRole(bytes32 role, address account) external view returns (bool);
    function latest() external view returns (uint256 price, uint256 timestamp);
}
