# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) once 1.0 is reached.

## [Unreleased]

## [0.4.0] - 2026-09-13

### Added

- The trading agent. `strategy`: a pure decision core protocol, bars that are empty rather
  than filled, causal exits with adverse fill assumptions (touch is a fire, gap fills at the
  worse open, two-sided bar is a stop, take-profit at its limit), `ExitRule` that must name
  the mechanism it is bound to, and the walk-forward harness that fits every parameter on
  trailing data, freezes it at entry, evaluates the open position on every bar, and refuses a
  precomputed one.
- `trading`: recorded feed and bar builder; a simulated venue that fills after the measured
  latency with the cost identity; controls that fail closed (risk per trade, position, order
  rate, daily loss, kill switch; flattening always admitted); the promotion ladder, up with
  evidence and a signature, down on a reason; one runtime for replay and shadow writing
  `intents.jsonl`, `fills.jsonl`, `trades.jsonl` and `run.json`.
- The Bybit public trade feed as the `[shadow]` extra: no key, acknowledged subscribe,
  silence detection, reconnect, unrecognised frames counted.
- `strategy.toml` with a built-in reference core; `tra trade replay`, `tra trade shadow`,
  `tra ladder show|promote|demote`.
- No live venue and no real orders in this release, by design.

## [0.3.0] - 2026-09-13

### Added

- `execution`: the round-trip cost identity with one owner and four separate terms, signed
  funding accrual that refuses a spanned settlement without a rate, hedged-pair fees, walk the
  book with refusal instead of extrapolation and a staleness tolerance, passive fills with
  trade-through and both queue bounds, entries at the tick after the measured latency,
  effective cost across unfilled orders with their drift, and markout beside its half-spread.
- `chain` layer 4: coverage, resolution and power feasibility from the manifest; a failure is
  `NOT PROVEN` with a blocking axis, never a death.
- `chain` layer 5: net returns, collapse per timestamp, moving-block bootstrap (specification
  followed where the reference implementation clamped or dropped), costed sign-permutation and
  cross-sectional nulls with a turnover guard, subperiods, Benjamini-Hochberg, two-sided anchor
  robustness, declared power, and the verdict from the registration's own kill criteria.
- The era seal: `research_only` raises on one leaked timestamp; `Seal` opens once against the
  registration's hash with the gates fixed in advance, persisted in a file written once.
- `tra chain requirements`, `tra chain validate`; `trades.jsonl` and `validation.json`.
- `numpy` as the first required dependency. Money stays `Decimal`; statistics are float.

## [0.2.0] - 2026-09-13

### Added

- The agent: an idea through layers 0, 2 and 3. A language model bounds the magnitude
  (with its basis), writes the mechanism memo, critiques it, and drafts the pre-registration;
  the cost floor, the memo's form and the registration's validity are decided by the gates
  that already exist. Two bounded revision loops; defects of form go back to the model with
  the gate's own words, facts about the idea are final.
- Refusals at layers 0, 2 and 3 recorded in `refusals.jsonl`, apart from the kill ledger.
- Every run written to `runs/<run_id>/` with each exchange, the sealed registration and a
  summary.
- Provider adapters on the official SDKs as extras: `[anthropic]` (default model
  `claude-opus-5`, cached system prompt, structured output) and `[openai]` (strict JSON
  schema, model named by the operator). Tests use a scripted fake and never touch the network.
- `tra agent propose` and `tra agent refusals`.
- `validate_mechanism` exposed from `chain`.

## [0.1.0] - 2026-09-13

### Added

- Kill ledger: closed vocabulary for verdicts, axes and transferability; `LedgerEntry`
  validated on construction; append-only JSON Lines store with retraction by new row.
- Operating envelope: per-channel fee schedule with optional measured round trip, loaded
  from TOML into exact decimals.
- Falsification chain, layer 0: the cost floor, issuing `CLOSED` on the cost axis with a
  recorded reversal point.
- Falsification chain, layer 3: the pre-registration with its nine required elements, a
  content-hash seal, numeric kill criteria, and amendments that may tighten but never loosen.
- Data gate: canonical `Trade` with exact decimals and UTC nanoseconds; normalisers for the
  Bybit and Binance spot public trade archives that verify the layout on every file and
  reject bad rows with line and reason; a one-pass bounded-memory audit (duplicates,
  ordering, sequence resets, gaps, scale changes, concentration, rejected fraction); a
  manifest with source checksums and the audit result.
- Parquet dataset writer and reader (`[data]` extra): `decimal128(38, s)` price and quantity
  at the venue's scale, UTC nanosecond timestamps, exact round trip, refusal of any value
  that does not fit the declared scale.
- Archive download with `.part` staging, size check and, for Binance, verification against
  the venue's published checksum.
- The `tra` command: `data fetch`, `data audit`, `chain floor`, `chain check`, `ledger show`,
  with exit codes a pipeline can act on and `--json` output.
- Tests that enforce the dependency direction between packages and ASCII-only source.
- CI on Linux and Windows across Python 3.11, 3.12 and 3.13.
