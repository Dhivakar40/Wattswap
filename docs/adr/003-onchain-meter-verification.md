# ADR-003: Meter readings are verified on-chain

Status: accepted (amended in Phase 2: per-meter interval ordering) · Date: 2026-10-18 · Supersedes the Phase 1 plan item "meter signatures stay off-chain"

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

## Amendment (Phase 2): intervals are ordered and never overlap

**Problem.** `readingId` replay protection alone does not stop a compromised or buggy device key from signing
*overlapping* intervals under different ids, each individually plausible, and so minting more energy than the
meter could have produced over the covered time. An API-side check cannot cover a caller that bypasses the API.

**Decision.** `MeterRegistry` keeps `lastIntervalEnd[meterId]`. During `EnergyToken.mint` the token calls the
state-changing, `RECORDER_ROLE`-gated `registry.recordReading(meterId, amountWh, intervalStart, intervalSeconds)`
which, in addition to the active/interval-bounds/plausibility checks above, requires:

- `intervalStart >= lastIntervalEnd` (else `IntervalOverlap(meterId, intervalStart, lastIntervalEnd)`), and
- `intervalEnd > intervalStart`, guaranteed because `intervalSeconds >= minIntervalSeconds >= 1`
  (`InvalidInterval` otherwise), with the addition overflow-checked by Solidity,

then sets `lastIntervalEnd = intervalStart + intervalSeconds` and emits `IntervalRecorded`.

Plausibility uses the reading's *own* `intervalSeconds`; nothing assumes a 900 s interval.

- Equal boundaries (`start == lastIntervalEnd`) and gaps are allowed. Overlap and out-of-order are rejected.
- `RECORDER_ROLE` is held by `EnergyToken` only (granted in `Deploy.s.sol`).
- State changes are atomic with the mint: a reverted mint does not advance `lastIntervalEnd`.
- `EnergyToken.mint` now writes `readingMinted` before the registry call (CEI).

**Operational consequences.** Readings per meter must be minted oldest-first. An offline device that reconnects
must backfill in chronological order; a skipped interval can be left as a gap but can never be filled in later.
The backend mint worker therefore submits each meter's readings strictly in `intervalStart` order, one at a time.
A reading that permanently fails is marked `failed`; later readings may proceed, which turns it into a gap.

**Invariant (tested).** For any sequence of calls, minted Wh per meter is at most
`ratedCapacityW x coveredSeconds x safetyBps / (3600 x 10000)` and covered time never exceeds
`lastIntervalEnd - firstIntervalStart`, so no stretch of time is paid for twice
(`MeterIntervalInvariantTest`). Unit and fuzz tests cover overlap, gap, equal boundary and out-of-order.
