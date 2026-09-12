# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) once 1.0 is reached.

## [Unreleased]

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
- Archive download with `.part` staging, size check and, for Binance, verification against
  the venue's published checksum.
- The `tra` command: `data fetch`, `data audit`, `chain floor`, `chain check`, `ledger show`,
  with exit codes a pipeline can act on and `--json` output.
- Tests that enforce the dependency direction between packages and ASCII-only source.
- CI on Linux and Windows across Python 3.11, 3.12 and 3.13.
