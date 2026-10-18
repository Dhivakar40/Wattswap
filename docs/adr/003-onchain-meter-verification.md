# ADR-003: Meter readings are verified on-chain

Status: accepted · Date: 2026-10-18 · Supersedes the Phase 1 plan item "meter signatures stay off-chain"

## Context

The first plan had the API verify the meter's signature and the oracle account call `mint`. That makes the
minter key the sole root of trust: a compromised oracle could mint arbitrary (plausible-looking) energy.

## Decision

- `MeterRegistry` stores a `deviceSigner` per meter, set at registration and rotatable by `REGISTRAR_ROLE`
  (`rotateDeviceSigner`, event `DeviceSignerRotated`).
- `EnergyToken.mint(Reading r, bytes deviceSig)` requires **all** of:
  1. caller has `MINTER_ROLE` (the oracle);
  2. `deviceSig` is a valid EIP-712 `Reading` signature by the *current* `deviceSigner`
     (via `SignatureChecker`, so EOA or EIP-1271 device wallets);
  3. the meter exists and is active, the interval is within `[minInterval, maxInterval]`, and
     `amountWh <= ratedCapacityW * intervalSeconds * safetyBps / (3600 * 10000)`;
  4. the interval has ended (`intervalStart + intervalSeconds <= block.timestamp`);
  5. `readingId` has never been minted (replay protection, global).
- Tokens are minted to the meter's registered **owner**, not to the caller or a signed recipient.

## Consequences

- A compromised oracle key alone cannot mint: it also needs a device key, and the amount is bounded by the meter's
  rated capacity. Compromised device keys are contained per meter (rotate/deactivate).
- Higher mint gas (one signature verification). Acceptable for 15-minute+ intervals.
- The meter simulator (Phase 2) must hold device keys and sign EIP-712 readings; it is labeled as a simulator and
  isolated behind an interface so real hardware can replace it.
- Tests: wrong signer, rotated key (old rejected, new accepted), replayed `readingId` (even with a different
  payload), tampered fields, malformed signature, inactive/unknown meter, implausible amount, incomplete interval.
