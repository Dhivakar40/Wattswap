// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Version} from "../src/Version.sol";

contract VersionTest is Test {
    function test_version() public {
        Version v = new Version();
        assertEq(v.VERSION(), "0.0.0");
    }
}
