// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice EIP-712 struct definitions and struct hashes for orders and price quotes.
library OrderLib {
    uint8 internal constant BUY = 0;
    uint8 internal constant SELL = 1;

    /// @dev `limitPrice` is mUSDC (6dp) per kWh; `amountWh` is WATT base units (1 = 1 Wh).
    struct Order {
        address maker;
        bytes32 marketId;
        uint8 side;
        uint256 amountWh;
        uint256 limitPrice;
        uint256 expiry;
        uint256 nonce;
        bytes32 salt;
    }

    /// @dev Signed by an account holding PRICER_ROLE on the PriceOracle.
    struct PriceQuote {
        bytes32 marketId;
        uint256 price;
        uint256 timestamp;
    }

    bytes32 internal constant ORDER_TYPEHASH = keccak256(
        "Order(address maker,bytes32 marketId,uint8 side,uint256 amountWh,uint256 limitPrice,uint256 expiry,uint256 nonce,bytes32 salt)"
    );
    bytes32 internal constant QUOTE_TYPEHASH =
        keccak256("PriceQuote(bytes32 marketId,uint256 price,uint256 timestamp)");

    function orderHash(Order memory o) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(
                ORDER_TYPEHASH,
                o.maker,
                o.marketId,
                o.side,
                o.amountWh,
                o.limitPrice,
                o.expiry,
                o.nonce,
                o.salt
            )
        );
    }

    function quoteHash(PriceQuote memory q) internal pure returns (bytes32) {
        return keccak256(abi.encode(QUOTE_TYPEHASH, q.marketId, q.price, q.timestamp));
    }
}
