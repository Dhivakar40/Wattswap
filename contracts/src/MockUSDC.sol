// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @title MockUSDC (mUSDC)
/// @notice TESTNET ONLY. Worthless 6-decimal stand-in for a stablecoin with a rate-limited faucet.
///         Never deploy to a network where tokens carry real value.
contract MockUSDC is ERC20 {
    uint256 public constant FAUCET_AMOUNT = 1_000e6;
    uint256 public constant FAUCET_COOLDOWN = 1 days;

    mapping(address account => uint256) public nextFaucetAt;

    event FaucetClaimed(address indexed account, uint256 amount);

    error FaucetCooldown(uint256 nextAt);

    constructor() ERC20("Mock USDC (testnet)", "mUSDC") {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function faucet() external {
        uint256 next = nextFaucetAt[msg.sender];
        if (block.timestamp < next) revert FaucetCooldown(next);
        nextFaucetAt[msg.sender] = block.timestamp + FAUCET_COOLDOWN;
        _mint(msg.sender, FAUCET_AMOUNT);
        emit FaucetClaimed(msg.sender, FAUCET_AMOUNT);
    }
}
