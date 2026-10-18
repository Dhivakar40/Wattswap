# ADR-005: Mint pipeline: exactly-once minting from at-least-once delivery

Status: accepted · Date: 2026-10-18

## Context

Devices retry, networks drop, processes crash, and a chain can accept a transaction the sender never hears
about. A reading must still produce exactly one mint, in per-meter chronological order (ADR-003 amendment).

## Decision

**Identity.** `readingId = keccak256(abi.encode(meterId, intervalStart, intervalSeconds))`. One interval, one
id; the API rejects any other id (`reading_id_mismatch`). Re-sending is therefore a no-op by construction.

**Layers of idempotency** (each is independently sufficient to prevent a double mint):

1. Ingest locks the meter row (`SELECT … FOR UPDATE`), then checks `reading_id`. Duplicates return `200`.
   `UNIQUE(readings.reading_id)`, `UNIQUE(meter, interval_start)` and `UNIQUE(mints.reading_pk)` back it up.
2. The worker asks the contract `readingMinted(readingId)` before every send. If true it *adopts* the existing mint
   (looking up the `ReadingMinted` log for the tx hash) instead of sending.
3. The contract rejects a repeated `readingId` and any interval starting before `lastIntervalEnd`.

**Crash safety.** The worker signs the transaction, persists `{status: submitted, tx_hash, raw_tx, nonce}`, and
only then broadcasts. After a restart a `submitted` row is resolved by hash: receipt found → finish; node has
never seen it → re-broadcast the *same* signed transaction (never re-sign with a fresh nonce); known but unmined
for longer than `MINT_TX_TIMEOUT_SECONDS` → replace with a higher fee on the same nonce (×1.25 per attempt, up
to `MINT_MAX_ATTEMPTS`), after which an earlier attempt landing is detected via `readingMinted`.

**Ordering and single writer.** One mint worker is active at a time (Redis leader lock with renewal), and the
oracle key's nonce is serialised by a second Redis lock. Within a meter, mints go strictly by `interval_start`;
a transiently deferred reading blocks that meter's later ones for the pass, a *terminally failed* reading does
not (it becomes a gap, which the contract allows).

**What may mark a reading `failed`: only a decoded contract-level rejection.** The worker simulates
(`estimate_gas`) before spending gas, and decodes reverts from the contract ABIs into `failure_reason`
(`MeterInactive(meterId=…)`, `ReadingImplausible(…)`, `InvalidDeviceSignature`, …). That decoded error, whether seen
in simulation or in a mined, reverted transaction, is the *only* path to `failed`. Everything else is infrastructure
and retries indefinitely with exponential backoff (`MINT_BACKOFF_BASE_SECONDS` … `MINT_BACKOFF_MAX_SECONDS`), leaving
the mint `pending`/`submitted` with no failure recorded:

- RPC errors, timeouts, connection loss (per-mint backoff; the loop itself also backs off);
- nonce problems: a persisted transaction whose nonce was consumed elsewhere (`nonce too low`) is re-queued and
  re-signed; stuck transactions are fee-bumped;
- gas problems: a mined revert with no contract error (e.g. out of gas) is re-queued with a fresh estimate;
- reverts with no or undecodable data, and `ReadingNotComplete` (the chain clock is behind the interval end; it
  clears by itself);
- `ReadingAlreadyMinted` after a race: the existing mint is adopted, not failed.

Failed readings stay visible: `GET /meters/{id}/readings` and `GET /mints` return `mint.status = failed` with the
decoded `failure_reason`, so the owner can see the gap and why.

## Consequences

- Tested against real Anvil: crash before broadcast, crash after broadcast, adoption of an unrecorded mint, a
  stuck transaction (automine off) replaced by a higher fee, two concurrent workers, eight concurrent duplicate posts.
- Throughput is one transaction per confirmation round per oracle key. Ample for 15-minute readings; batching
  would need a contract change (not planned).
- Ingest also consults the on-chain `lastIntervalEnd`, so a database reset cannot let the API accept readings the
  chain will refuse.
- Only EOA device keys can be verified off-chain at ingest. EIP-1271 device wallets work on-chain but the API would
  reject their signatures; supporting them needs an `isValidSignature` call at ingest (deferred).
