// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {OrderLib} from "./libraries/OrderLib.sol";
import {IPriceOracle} from "./interfaces/IPriceOracle.sol";

/// @title WattSwapExchange
/// @notice Atomic settlement of off-chain EIP-712 signed orders: WATT seller -> buyer and
///         mUSDC buyer -> seller (+ protocol fee to treasury) in ONE transaction. Any failed
///         check or transfer reverts the entire call, so either everything moves or nothing does.
///         Testnet/demo contract: payment is a mock stablecoin and no physical delivery occurs.
contract WattSwapExchange is AccessControl, Pausable, ReentrancyGuard, EIP712 {
    using SafeERC20 for IERC20;
    using OrderLib for OrderLib.Order;
    using OrderLib for OrderLib.PriceQuote;

    bytes32 public constant RELAYER_ROLE = keccak256("RELAYER_ROLE");

    uint256 public constant BPS = 10_000;
    uint256 public constant MAX_FEE_BPS = 500; // 5%
    uint256 public constant MAX_BAND_BPS = 2_000; // 20%
    /// @dev price is per kWh; amounts are in Wh.
    uint256 public constant WH_PER_KWH = 1_000;

    IERC20 public immutable watt;
    IERC20 public immutable usdc;
    IPriceOracle public immutable oracle;

    address public treasury;
    uint256 public feeBps;
    /// @notice Allowed deviation of execPrice from the signed quote, in basis points.
    uint256 public bandBps;

    mapping(bytes32 marketId => bool) public marketEnabled;
    mapping(bytes32 orderHash => uint256) public filled;
    mapping(bytes32 orderHash => bool) public cancelled;
    /// @notice Orders with nonce < minNonce[maker] are void (cancelUpTo).
    mapping(address maker => uint256) public minNonce;

    event TradeSettled(
        bytes32 indexed marketId,
        bytes32 indexed buyHash,
        bytes32 indexed sellHash,
        address buyer,
        address seller,
        uint256 fillWh,
        uint256 execPrice,
        uint256 cost,
        uint256 fee
    );
    event OrderCancelled(address indexed maker, bytes32 indexed orderHash);
    event NonceFloorRaised(address indexed maker, uint256 newMinNonce);
    event FeeUpdated(uint256 oldFeeBps, uint256 newFeeBps);
    event TreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);
    event BandUpdated(uint256 oldBandBps, uint256 newBandBps);
    event MarketUpdated(bytes32 indexed marketId, bool enabled);

    error ZeroAddress();
    error InvalidFee(uint256 feeBps);
    error InvalidBand(uint256 bandBps);
    error InvalidSide();
    error MarketMismatch();
    error MarketDisabled(bytes32 marketId);
    error SelfTrade();
    error ZeroFill();
    error DustFill();
    error OrderExpired(bytes32 orderHash, uint256 expiry);
    error OrderIsCancelled(bytes32 orderHash);
    error NonceTooLow(uint256 nonce, uint256 minNonce);
    error InvalidOrderSignature(bytes32 orderHash);
    error Overfill(bytes32 orderHash, uint256 remaining, uint256 requested);
    error PriceOutOfLimits(uint256 execPrice);
    error QuoteMarketMismatch();
    error QuoteFromFuture(uint256 timestamp);
    error StaleQuote(uint256 timestamp, uint256 maxStaleness);
    error InvalidQuoteSigner();
    error PriceOutsideBand(uint256 execPrice, uint256 quotePrice);
    error NotMaker();
    error AlreadyCancelled(bytes32 orderHash);
    error NonceNotIncreasing(uint256 given, uint256 current);

    constructor(
        address admin,
        IERC20 watt_,
        IERC20 usdc_,
        IPriceOracle oracle_,
        address treasury_,
        uint256 feeBps_,
        uint256 bandBps_
    ) EIP712("WattSwapExchange", "1") {
        if (
            admin == address(0) || address(watt_) == address(0) || address(usdc_) == address(0)
                || address(oracle_) == address(0) || treasury_ == address(0)
        ) revert ZeroAddress();
        if (feeBps_ > MAX_FEE_BPS) revert InvalidFee(feeBps_);
        if (bandBps_ == 0 || bandBps_ > MAX_BAND_BPS) revert InvalidBand(bandBps_);
        watt = watt_;
        usdc = usdc_;
        oracle = oracle_;
        treasury = treasury_;
        feeBps = feeBps_;
        bandBps = bandBps_;
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
    }

    // ------------------------------------------------------------------ settlement

    /// @notice Settles a matched buy/sell pair. Only callable by the relayer.
    function settle(
        OrderLib.Order calldata buy,
        bytes memory buySig,
        OrderLib.Order calldata sell,
        bytes memory sellSig,
        uint256 fillAmountWh,
        uint256 execPrice,
        OrderLib.PriceQuote calldata quote,
        bytes memory quoteSig
    ) external onlyRole(RELAYER_ROLE) whenNotPaused nonReentrant {
        _checkPair(buy, sell, fillAmountWh, execPrice);
        bytes32 buyHash = _verifyOrder(buy, buySig, fillAmountWh);
        bytes32 sellHash = _verifyOrder(sell, sellSig, fillAmountWh);
        _checkQuote(buy.marketId, execPrice, quote, quoteSig);
        _execute(buy.marketId, buyHash, sellHash, buy.maker, sell.maker, fillAmountWh, execPrice);
    }

    /// @dev Effects, then both token legs. Kept separate from settle() to bound stack depth.
    function _execute(
        bytes32 marketId,
        bytes32 buyHash,
        bytes32 sellHash,
        address buyer,
        address seller,
        uint256 fill,
        uint256 execPrice
    ) private {
        uint256 cost = fill * execPrice / WH_PER_KWH;
        if (cost == 0) revert DustFill();
        uint256 fee = cost * feeBps / BPS;

        // effects
        filled[buyHash] += fill;
        filled[sellHash] += fill;

        // interactions: both legs in this single transaction
        watt.safeTransferFrom(seller, buyer, fill);
        usdc.safeTransferFrom(buyer, seller, cost - fee);
        if (fee != 0) usdc.safeTransferFrom(buyer, treasury, fee);

        emit TradeSettled(marketId, buyHash, sellHash, buyer, seller, fill, execPrice, cost, fee);
    }

    function _checkPair(
        OrderLib.Order calldata buy,
        OrderLib.Order calldata sell,
        uint256 fill,
        uint256 execPrice
    ) private view {
        if (buy.side != OrderLib.BUY || sell.side != OrderLib.SELL) {
            revert InvalidSide();
        }
        if (buy.marketId != sell.marketId) revert MarketMismatch();
        if (!marketEnabled[buy.marketId]) revert MarketDisabled(buy.marketId);
        if (buy.maker == sell.maker) revert SelfTrade();
        if (fill == 0) revert ZeroFill();
        if (execPrice < sell.limitPrice || execPrice > buy.limitPrice) {
            revert PriceOutOfLimits(execPrice);
        }
    }

    function _verifyOrder(OrderLib.Order calldata o, bytes memory sig, uint256 fill)
        private
        view
        returns (bytes32 h)
    {
        h = _hashTypedDataV4(OrderLib.orderHash(o));
        if (block.timestamp > o.expiry) revert OrderExpired(h, o.expiry);
        if (cancelled[h]) revert OrderIsCancelled(h);
        uint256 floor = minNonce[o.maker];
        if (o.nonce < floor) revert NonceTooLow(o.nonce, floor);
        if (!SignatureChecker.isValidSignatureNow(o.maker, h, sig)) {
            revert InvalidOrderSignature(h);
        }
        uint256 remaining = o.amountWh - filled[h];
        if (fill > remaining) revert Overfill(h, remaining, fill);
    }

    function _checkQuote(
        bytes32 marketId,
        uint256 execPrice,
        OrderLib.PriceQuote calldata q,
        bytes memory sig
    ) private view {
        if (q.marketId != marketId) revert QuoteMarketMismatch();
        if (q.timestamp > block.timestamp) revert QuoteFromFuture(q.timestamp);
        uint256 maxStale = oracle.maxStaleness();
        if (block.timestamp - q.timestamp > maxStale) revert StaleQuote(q.timestamp, maxStale);

        (address signer, ECDSA.RecoverError err,) =
            ECDSA.tryRecover(_hashTypedDataV4(OrderLib.quoteHash(q)), sig);
        if (err != ECDSA.RecoverError.NoError || !oracle.hasRole(oracle.PRICER_ROLE(), signer)) {
            revert InvalidQuoteSigner();
        }

        uint256 diff = execPrice > q.price ? execPrice - q.price : q.price - execPrice;
        if (diff * BPS > q.price * bandBps) revert PriceOutsideBand(execPrice, q.price);
    }

    // ------------------------------------------------------------------ cancellation

    /// @notice Cancels one order. Only the maker can do this.
    function cancel(OrderLib.Order calldata o) external {
        if (o.maker != msg.sender) revert NotMaker();
        bytes32 h = _hashTypedDataV4(OrderLib.orderHash(o));
        if (cancelled[h]) revert AlreadyCancelled(h);
        cancelled[h] = true;
        emit OrderCancelled(msg.sender, h);
    }

    /// @notice Voids every order of the caller with nonce < `newMinNonce`.
    function cancelUpTo(uint256 newMinNonce) external {
        uint256 current = minNonce[msg.sender];
        if (newMinNonce <= current) revert NonceNotIncreasing(newMinNonce, current);
        minNonce[msg.sender] = newMinNonce;
        emit NonceFloorRaised(msg.sender, newMinNonce);
    }

    // ------------------------------------------------------------------ admin

    function setFee(uint256 newFeeBps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newFeeBps > MAX_FEE_BPS) revert InvalidFee(newFeeBps);
        emit FeeUpdated(feeBps, newFeeBps);
        feeBps = newFeeBps;
    }

    function setTreasury(address newTreasury) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newTreasury == address(0)) revert ZeroAddress();
        emit TreasuryUpdated(treasury, newTreasury);
        treasury = newTreasury;
    }

    /// @notice Sets the allowed |execPrice - quote.price| / quote.price band, in basis points.
    function setBand(uint256 newBandBps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newBandBps == 0 || newBandBps > MAX_BAND_BPS) revert InvalidBand(newBandBps);
        emit BandUpdated(bandBps, newBandBps);
        bandBps = newBandBps;
    }

    function setMarket(bytes32 marketId, bool enabled) external onlyRole(DEFAULT_ADMIN_ROLE) {
        marketEnabled[marketId] = enabled;
        emit MarketUpdated(marketId, enabled);
    }

    function pause() external onlyRole(DEFAULT_ADMIN_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(DEFAULT_ADMIN_ROLE) {
        _unpause();
    }

    // ------------------------------------------------------------------ views

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparatorV4();
    }

    function hashOrder(OrderLib.Order calldata o) external view returns (bytes32) {
        return _hashTypedDataV4(OrderLib.orderHash(o));
    }

    function hashQuote(OrderLib.PriceQuote calldata q) external view returns (bytes32) {
        return _hashTypedDataV4(OrderLib.quoteHash(q));
    }

    function remaining(OrderLib.Order calldata o) external view returns (uint256) {
        return o.amountWh - filled[_hashTypedDataV4(OrderLib.orderHash(o))];
    }
}
