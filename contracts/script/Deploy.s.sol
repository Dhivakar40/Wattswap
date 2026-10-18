// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {MeterRegistry} from "src/MeterRegistry.sol";
import {EnergyToken} from "src/EnergyToken.sol";
import {MockUSDC} from "src/MockUSDC.sol";
import {PriceOracle} from "src/PriceOracle.sol";
import {WattSwapExchange} from "src/WattSwapExchange.sol";
import {IMeterRegistry} from "src/interfaces/IMeterRegistry.sol";
import {IPriceOracle} from "src/interfaces/IPriceOracle.sol";

/// @notice Deploys the full WattSwap stack and wires roles. Works unchanged on Anvil and Sepolia.
///
///   Required env:   PRIVATE_KEY            deployer; becomes DEFAULT_ADMIN of every contract
///   Optional env (each defaults to the deployer address):
///                   RELAYER_ADDRESS        RELAYER_ROLE on the exchange
///                   MINTER_ADDRESS         MINTER_ROLE on EnergyToken (the meter oracle)
///                   PRICER_ADDRESS         PRICER_ROLE on PriceOracle (signs price quotes)
///                   REGISTRAR_ADDRESS      REGISTRAR_ROLE on MeterRegistry
///                   TREASURY_ADDRESS       protocol fee recipient
///   Optional:       FEE_BPS (default 100)  BAND_BPS (default 500)
///
///   MockUSDC is TESTNET ONLY. This script refuses to run on chain ids other than
///   31337 (Anvil) and 11155111 (Sepolia).
///
///   Local:   forge script script/Deploy.s.sol --rpc-url http://localhost:8545 --broadcast
///   Sepolia: forge script script/Deploy.s.sol --rpc-url $SEPOLIA_RPC_URL --broadcast --verify
contract Deploy is Script {
    bytes32 public constant WATT_MUSDC_MARKET = keccak256("WATT-mUSDC");

    struct Deployment {
        MeterRegistry registry;
        EnergyToken watt;
        MockUSDC usdc;
        PriceOracle oracle;
        WattSwapExchange exchange;
    }

    error UnsupportedChain(uint256 chainId);

    function run() external returns (Deployment memory d) {
        if (block.chainid != 31337 && block.chainid != 11155111) {
            revert UnsupportedChain(block.chainid);
        }

        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(deployerKey);
        address relayer = vm.envOr("RELAYER_ADDRESS", deployer);
        address minter = vm.envOr("MINTER_ADDRESS", deployer);
        address pricer = vm.envOr("PRICER_ADDRESS", deployer);
        address registrar = vm.envOr("REGISTRAR_ADDRESS", deployer);
        address treasury = vm.envOr("TREASURY_ADDRESS", deployer);
        uint256 feeBps = vm.envOr("FEE_BPS", uint256(100));
        uint256 bandBps = vm.envOr("BAND_BPS", uint256(500));

        vm.startBroadcast(deployerKey);

        d.registry = new MeterRegistry(deployer);
        d.watt = new EnergyToken(deployer, IMeterRegistry(address(d.registry)));
        d.usdc = new MockUSDC();
        d.oracle = new PriceOracle(deployer);
        d.exchange = new WattSwapExchange(
            deployer,
            IERC20(address(d.watt)),
            IERC20(address(d.usdc)),
            IPriceOracle(address(d.oracle)),
            treasury,
            feeBps,
            bandBps
        );

        d.registry.grantRole(d.registry.REGISTRAR_ROLE(), registrar);
        d.registry.grantRole(d.registry.RECORDER_ROLE(), address(d.watt));
        d.watt.grantRole(d.watt.MINTER_ROLE(), minter);
        d.oracle.grantRole(d.oracle.PRICER_ROLE(), pricer);
        d.exchange.grantRole(d.exchange.RELAYER_ROLE(), relayer);
        d.exchange.setMarket(WATT_MUSDC_MARKET, true);

        vm.stopBroadcast();

        console2.log("chainId           ", block.chainid);
        console2.log("MeterRegistry     ", address(d.registry));
        console2.log("EnergyToken (WATT)", address(d.watt));
        console2.log("MockUSDC          ", address(d.usdc));
        console2.log("PriceOracle       ", address(d.oracle));
        console2.log("WattSwapExchange  ", address(d.exchange));
        console2.log("admin/deployer    ", deployer);
    }
}
