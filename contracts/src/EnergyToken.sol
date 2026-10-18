// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {IMeterRegistry} from "./interfaces/IMeterRegistry.sol";

/// @title EnergyToken (WATT)
/// @notice 1 WATT unit = 1 Wh; `decimals() == 3`, so 1.000 WATT = 1 kWh.
///         (18 decimals would imply sub-milliwatt-hour precision that no meter provides.)
///         Tokens are minted only against an EIP-712 Reading signed by the meter's device key.
///         Testnet/demo token: it does not represent physically delivered energy.
contract EnergyToken is ERC20, ERC20Permit, AccessControl, Pausable {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");

    bytes32 public constant READING_TYPEHASH = keccak256(
        "Reading(bytes32 meterId,bytes32 readingId,uint256 amountWh,uint256 intervalStart,uint256 intervalSeconds)"
    );

    struct Reading {
        bytes32 meterId;
        bytes32 readingId;
        uint256 amountWh;
        uint256 intervalStart;
        uint256 intervalSeconds;
    }

    IMeterRegistry public immutable registry;

    mapping(bytes32 readingId => bool) public readingMinted;

    event ReadingMinted(
        bytes32 indexed readingId,
        bytes32 indexed meterId,
        address indexed owner,
        uint256 amountWh,
        uint256 intervalStart,
        uint256 intervalSeconds
    );

    error ZeroAddress();
    error ZeroAmount();
    error ReadingAlreadyMinted(bytes32 readingId);
    error ReadingNotComplete(uint256 intervalEnd);
    error InvalidDeviceSignature();

    constructor(address admin, IMeterRegistry registry_)
        ERC20("WattSwap Energy", "WATT")
        ERC20Permit("WattSwap Energy")
    {
        if (admin == address(0) || address(registry_) == address(0)) revert ZeroAddress();
        registry = registry_;
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PAUSER_ROLE, admin);
    }

    function decimals() public pure override returns (uint8) {
        return 3;
    }

    function hashReading(Reading calldata r) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    READING_TYPEHASH,
                    r.meterId,
                    r.readingId,
                    r.amountWh,
                    r.intervalStart,
                    r.intervalSeconds
                )
            )
        );
    }

    /// @notice Mints WATT to the meter owner for a signed reading.
    /// @dev Caller must be MINTER_ROLE (the oracle) AND the device key must have signed `r`.
    function mint(Reading calldata r, bytes calldata deviceSig)
        external
        onlyRole(MINTER_ROLE)
        whenNotPaused
    {
        if (r.amountWh == 0) revert ZeroAmount();
        if (readingMinted[r.readingId]) revert ReadingAlreadyMinted(r.readingId);
        uint256 end = r.intervalStart + r.intervalSeconds;
        if (end > block.timestamp) revert ReadingNotComplete(end);

        (address owner, address signer) =
            registry.validateReading(r.meterId, r.amountWh, r.intervalSeconds);
        if (!SignatureChecker.isValidSignatureNow(signer, hashReading(r), deviceSig)) {
            revert InvalidDeviceSignature();
        }

        readingMinted[r.readingId] = true;
        _mint(owner, r.amountWh);
        emit ReadingMinted(
            r.readingId, r.meterId, owner, r.amountWh, r.intervalStart, r.intervalSeconds
        );
    }

    function pause() external onlyRole(PAUSER_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(PAUSER_ROLE) {
        _unpause();
    }

    function _update(address from, address to, uint256 value) internal override whenNotPaused {
        super._update(from, to, value);
    }
}
