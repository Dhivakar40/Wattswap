// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Deploy} from "script/Deploy.s.sol";

contract DeployTest is Test {
    function test_deployWiresRolesAndMarket() public {
        uint256 pk = uint256(keccak256("deployer"));
        address deployer = vm.addr(pk);
        address relayer = makeAddr("relayer");
        vm.setEnv("PRIVATE_KEY", vm.toString(bytes32(pk)));
        vm.setEnv("RELAYER_ADDRESS", vm.toString(relayer));
        vm.chainId(31337);

        Deploy script = new Deploy();
        Deploy.Deployment memory d = script.run();

        assertTrue(d.exchange.hasRole(d.exchange.DEFAULT_ADMIN_ROLE(), deployer));
        assertTrue(d.exchange.hasRole(d.exchange.RELAYER_ROLE(), relayer));
        assertFalse(d.exchange.hasRole(d.exchange.RELAYER_ROLE(), deployer));
        assertTrue(d.watt.hasRole(d.watt.MINTER_ROLE(), deployer));
        assertTrue(d.oracle.hasRole(d.oracle.PRICER_ROLE(), deployer));
        assertTrue(d.registry.hasRole(d.registry.REGISTRAR_ROLE(), deployer));
        assertTrue(d.registry.hasRole(d.registry.RECORDER_ROLE(), address(d.watt)));
        assertFalse(d.registry.hasRole(d.registry.RECORDER_ROLE(), deployer));
        assertTrue(d.exchange.marketEnabled(script.WATT_MUSDC_MARKET()));
        assertEq(d.exchange.feeBps(), 100);
        assertEq(d.exchange.bandBps(), 500);
        assertEq(d.watt.decimals(), 3);
        assertEq(address(d.exchange.oracle()), address(d.oracle));
    }

    function test_deployRefusesUnsupportedChain() public {
        vm.setEnv("PRIVATE_KEY", vm.toString(bytes32(uint256(1))));
        vm.chainId(1);
        Deploy script = new Deploy();
        vm.expectRevert(abi.encodeWithSelector(Deploy.UnsupportedChain.selector, 1));
        script.run();
    }
}
