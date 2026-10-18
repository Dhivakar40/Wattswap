// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {IMeterRegistry} from "./interfaces/IMeterRegistry.sol";

/// @title MeterRegistry
/// @notice Maps meter ids to owner wallet, device signing key, rated capacity and location.
///         Meters in the demo are produced by a labeled simulator; the registry itself is
///         hardware-agnostic: any device that can sign an EIP-712 Reading can be registered.
contract MeterRegistry is AccessControl, IMeterRegistry {
    bytes32 public constant REGISTRAR_ROLE = keccak256("REGISTRAR_ROLE");
    /// @notice Held by EnergyToken only: may record reading intervals during mint.
    bytes32 public constant RECORDER_ROLE = keccak256("RECORDER_ROLE");

    uint256 public constant BPS = 10_000;
    uint256 public constant MAX_SAFETY_BPS = 20_000;
    int32 public constant MAX_LAT_E6 = 90_000_000;
    int32 public constant MAX_LON_E6 = 180_000_000;

    struct Meter {
        address owner;
        address deviceSigner;
        uint32 ratedCapacityW;
        int32 latE6;
        int32 lonE6;
        bool active;
    }

    mapping(bytes32 meterId => Meter) private _meters;

    /// @notice End (exclusive) of the latest interval minted for a meter; 0 before the first.
    ///         Readings must start at or after this value, so intervals never overlap and are
    ///         minted in order. Gaps are allowed; equal boundaries are allowed.
    mapping(bytes32 meterId => uint256) public lastIntervalEnd;

    /// @notice Max plausible energy = capacity x interval x safetyBps / 10_000 (default 120%).
    uint256 public safetyBps = 12_000;
    uint256 public minIntervalSeconds = 60;
    uint256 public maxIntervalSeconds = 1 days;

    event MeterRegistered(
        bytes32 indexed meterId,
        address indexed owner,
        address indexed deviceSigner,
        uint32 ratedCapacityW,
        int32 latE6,
        int32 lonE6
    );
    event DeviceSignerRotated(
        bytes32 indexed meterId, address indexed oldSigner, address indexed newSigner
    );
    event MeterActiveSet(bytes32 indexed meterId, bool active);
    event MeterCapacityUpdated(bytes32 indexed meterId, uint32 oldCapacityW, uint32 newCapacityW);
    event IntervalRecorded(bytes32 indexed meterId, uint256 intervalStart, uint256 intervalEnd);
    event PlausibilityUpdated(
        uint256 safetyBps, uint256 minIntervalSeconds, uint256 maxIntervalSeconds
    );

    error ZeroAddress();
    error ZeroCapacity();
    error MeterAlreadyRegistered(bytes32 meterId);
    error MeterNotFound(bytes32 meterId);
    error MeterInactive(bytes32 meterId);
    error InvalidLocation();
    error InvalidInterval(uint256 intervalSeconds);
    error ReadingImplausible(uint256 amountWh, uint256 maxWh);
    error InvalidPlausibilityConfig();
    error IntervalOverlap(bytes32 meterId, uint256 intervalStart, uint256 lastIntervalEnd);

    constructor(address admin) {
        if (admin == address(0)) revert ZeroAddress();
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
    }

    function registerMeter(
        bytes32 meterId,
        address owner,
        address deviceSigner,
        uint32 ratedCapacityW,
        int32 latE6,
        int32 lonE6
    ) external onlyRole(REGISTRAR_ROLE) {
        if (owner == address(0) || deviceSigner == address(0)) revert ZeroAddress();
        if (ratedCapacityW == 0) revert ZeroCapacity();
        if (latE6 < -MAX_LAT_E6 || latE6 > MAX_LAT_E6 || lonE6 < -MAX_LON_E6 || lonE6 > MAX_LON_E6)
        {
            revert InvalidLocation();
        }
        if (_meters[meterId].owner != address(0)) revert MeterAlreadyRegistered(meterId);

        _meters[meterId] = Meter(owner, deviceSigner, ratedCapacityW, latE6, lonE6, true);
        emit MeterRegistered(meterId, owner, deviceSigner, ratedCapacityW, latE6, lonE6);
    }

    /// @notice Rotates the device key. Readings signed by the old key stop minting immediately.
    function rotateDeviceSigner(bytes32 meterId, address newSigner)
        external
        onlyRole(REGISTRAR_ROLE)
    {
        if (newSigner == address(0)) revert ZeroAddress();
        Meter storage m = _requireMeter(meterId);
        address old = m.deviceSigner;
        m.deviceSigner = newSigner;
        emit DeviceSignerRotated(meterId, old, newSigner);
    }

    function setActive(bytes32 meterId, bool active) external onlyRole(REGISTRAR_ROLE) {
        _requireMeter(meterId).active = active;
        emit MeterActiveSet(meterId, active);
    }

    function updateCapacity(bytes32 meterId, uint32 newCapacityW)
        external
        onlyRole(REGISTRAR_ROLE)
    {
        if (newCapacityW == 0) revert ZeroCapacity();
        Meter storage m = _requireMeter(meterId);
        emit MeterCapacityUpdated(meterId, m.ratedCapacityW, newCapacityW);
        m.ratedCapacityW = newCapacityW;
    }

    function setPlausibility(uint256 safetyBps_, uint256 minInterval, uint256 maxInterval)
        external
        onlyRole(DEFAULT_ADMIN_ROLE)
    {
        if (
            safetyBps_ == 0 || safetyBps_ > MAX_SAFETY_BPS || minInterval == 0
                || minInterval > maxInterval
        ) revert InvalidPlausibilityConfig();
        safetyBps = safetyBps_;
        minIntervalSeconds = minInterval;
        maxIntervalSeconds = maxInterval;
        emit PlausibilityUpdated(safetyBps_, minInterval, maxInterval);
    }

    function getMeter(bytes32 meterId) external view returns (Meter memory) {
        Meter memory m = _meters[meterId];
        if (m.owner == address(0)) revert MeterNotFound(meterId);
        return m;
    }

    /// @notice Maximum plausible energy (Wh) a meter may report for an interval.
    function maxEnergyWh(uint32 ratedCapacityW, uint256 intervalSeconds)
        public
        view
        returns (uint256)
    {
        return uint256(ratedCapacityW) * intervalSeconds * safetyBps / (3600 * BPS);
    }

    /// @inheritdoc IMeterRegistry
    /// @dev Plausibility uses the reading's own `intervalSeconds`. `intervalSeconds >= 1` is
    ///      guaranteed by `minIntervalSeconds >= 1`, so `intervalEnd > intervalStart` always holds.
    function recordReading(
        bytes32 meterId,
        uint256 amountWh,
        uint256 intervalStart,
        uint256 intervalSeconds
    ) external onlyRole(RECORDER_ROLE) returns (address owner, address deviceSigner) {
        Meter storage m = _requireMeter(meterId);
        if (!m.active) revert MeterInactive(meterId);
        if (intervalSeconds < minIntervalSeconds || intervalSeconds > maxIntervalSeconds) {
            revert InvalidInterval(intervalSeconds);
        }
        uint256 last = lastIntervalEnd[meterId];
        if (intervalStart < last) revert IntervalOverlap(meterId, intervalStart, last);
        uint256 maxWh = maxEnergyWh(m.ratedCapacityW, intervalSeconds);
        if (amountWh > maxWh) revert ReadingImplausible(amountWh, maxWh);

        uint256 end = intervalStart + intervalSeconds;
        lastIntervalEnd[meterId] = end;
        emit IntervalRecorded(meterId, intervalStart, end);
        return (m.owner, m.deviceSigner);
    }

    function _requireMeter(bytes32 meterId) private view returns (Meter storage m) {
        m = _meters[meterId];
        if (m.owner == address(0)) revert MeterNotFound(meterId);
    }
}
