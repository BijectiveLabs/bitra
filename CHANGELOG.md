# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) once 1.0 is reached.

## [Unreleased]

### Added

- Agents with full access through the gates. Seven role packages under `agent/`
  (market screener, technical analyst, macro analyst, on-chain analyst, risk manager,
  executor, auditor), each a schema, a parser and a venue-neutral prompt. `tra agent
  session` runs them in a fixed order over recorded data in the sandbox and records every
  answer.
- The technical analyst as a decision core (`[strategy].agent = "technical_analyst"`),
  satisfying the `Strategy` protocol: asked on closed bars of a minute or longer, within a
  budget; a spent budget ends the session flat.
- `usage.jsonl` and `MeteredCompleter`: every model call written down with tokens, latency
  and the declared price; a budget that refuses before the call.
- Providers as plugins with verified presets (deepseek, groq, xai, mistral, openrouter, qwen,
  ollama, local) through the OpenAI-compatible adapter, and a Google adapter behind the
  `[google]` extra. `tra agent propose --provider` takes any registered name.
- `surface/`: the seven read tools shared by the MCP server and the roles, and the only
  actions a role may take: tighten limits, demote, arm the kill switch.
- `docs/agents.md`, `docs/providers.md`, `docs/liability.md`.
- Four commands from nothing to a backtest: `tra data get INSTRUMENT DAY` (fetch, audit,
  Parquet, manifest and funding into `data/`), `tra check` (what is ready and what is
  missing, in plain words), flagless `tra backtest` / `tra paper` / `tra live` / `tra agent
  session` inside a project (config, envelope, newest dataset, funding beside it, a run
  directory under `runs/`), and `tra init --agent PROVIDER --model M` for a model as the
  core. `[data].max_gap` and `[agents].magnitude_bps` in `strategy.toml`.
- The analyst's bars go to the model as compact rows: half the tokens per call, measured on
  a real day.
- Tools per role: `screen_figures` with a written regime rule, `features` on every closed
  bar, `macro_brief` and `onchain_brief` over external series (`data/series.py`,
  `examples/series.toml`: any source, aligned as-of with the publication lag honoured,
  correlations measured with n, thresholds declared), `risk_brief` from a run's own files,
  `size_for` / `rate_budget` / `export_signals` (`tra signals export`), `audit_brief`.
- `agent/react.py`: a bounded tool loop with a closed catalog, checked arguments, cut
  observations, a step cap and every step recorded. The auditor works it over the read
  surface before it answers.
- `ledger_search` and `runs_index` on the surface and the MCP server (nine tools).
- `tra agent session --resume`: recorded answers and finished runs are reused, and the
  reuse is written down.
- `tra agent eval --model PROVIDER:MODEL ...`: the analyst once per model over the same
  bars; net bps, round trips, calls, tokens, latency median and p90, tokens per bps, cost;
  `eval.json` and one run per model.
- `tra agent trace RUN|SESSION`: the role answers, the auditor's steps, every model call on
  the wall clock and every intent, fill and round trip on the bar clock, from the files a
  run wrote.
- Layer 1 of the chain, market observation: `chain/observation.py`, `tra chain observe`,
  `observation.toml` (an event on closed bars by feature or calendar, the response measured
  after it, counts per subperiod, the operator's minimums), the `observe_events` read tool
  (ten on the surface and the MCP server), and the example written by `tra init`.
- `tra check` reads `observation.toml` when present; a session hands the screener layer 1
  per candidate (`--observation`, or the file beside the config).
- The console face (`cli/ui.py`, on `rich`): `tra` alone is the cockpit (banner, what is
  ready, the next command); runs show a live panel over every closed bar and sessions show
  each role as it runs; check, session, eval, trace, ledger, ladder and observe render as
  tables. Off for `--json`, `--plain`, `TRA_PLAIN=1` and pipes. Two observation hooks carry
  it: `TradingConfig.observer` and `SessionConfig.on_step`; neither can decide anything.

### Changed

- A canary cap that is already met ends the session when flat.
- The console output escapes what the terminal's encoding cannot show instead of ending
  the command.
- `tra agent session` exits refused when the session never reached its audit verdict.
- `[agents].call_timeout_s` now reaches the vendor client as a per-request timeout, with
  `[agents].max_retries` (default 2) for the SDK's own backoff on 429s and 5xx; before, the
  declared timeout was recorded and not enforced. `CallOptions` carries all three to a
  provider's `build_with`.
- The runtime reads a `halt` from its core and ends the session flat.

## [0.5.0] - 2026-09-13

### Added

- The canary tier. `tra live` (`trade live`) runs the same loop as replay and shadow against
  a live venue plugin, with stops and take-profits routed through the venue as reduce-only
  orders, hard caps from `[canary]` (notional, round trips, session loss, minutes), and the
  ladder as the gate: the run refuses unless `ladder.json` stands at canary. Production
  stays refused.
- Live venues are plugins: `trading/venues/` with `LiveVenueFactory`, `LiveVenueSettings`,
  the `trading_research_agent.live_venues` entry point, and one bundled adapter (v5 REST,
  market IOC, idempotent `orderLinkId`, fills confirmed from order state, position
  reconciled after every fill, fees as charged). Its security review is
  `tests/test_live_venue.py`, run against a local double that verifies every signature.
- Preflight before the first print: clock skew against the venue, key permissions (trade
  only, no wallet, IP-bound, not expiring), no foreign position unless adopted deliberately.
  Keys come from the environment or `--env-file`; a key-like field in `[live]` is refused.
- Funding as charged: `FundingSource` on archive plugins, `tra data funding` writing
  `funding.json` with the measured settlement interval, `[run].funding` feeding the cost
  identity in every tier.
- The MCP server behind the `[mcp]` extra: seven read tools over the gates (`tra mcp serve`),
  none of which trades, promotes or records.
- Conventions: `tra init DIR` from bundled templates; `tra backtest`, `tra paper`, `tra
  live` as aliases of the tiers; `.env.example`.
- Release workflow: wheel and sdist built and attached on a version tag.

### Changed

- The `Venue` protocol carries `routes_exits`, `poll` and `pending`; the runtime is typed
  against the protocol, not the simulation, and a `VenueError` mid-session stops the loop
  and arms the kill switch.
- A partial closing fill leaves a smaller position and a second reduce-only request.

### Deliberately absent

- Production (T4), limit and maker orders, the private execution stream, a second live
  venue, Docker. Each is scheduled, none is a missing line of code.

## [0.4.1] - 2026-09-13

### Changed

- No strategy ships inside the package. The reference cores moved to
  `examples/strategies/` as examples; `strategy.toml` names a core by `file` and `class`
  (looked up next to the config first) or by `import`. The `builtin` key is gone.
- The walk-forward harness is generic: `walk_forward(panel, core, lookback=...)` takes a
  `FittedCore` and knows nothing about pairs or z-scores. The pair core is now the example.
- Venues are plugins. Archive layouts live in `data/venues/` behind `ArchiveVenue` and the
  `trading_research_agent.venues` entry point; live feeds in `trading/feeds/` behind
  `LiveFeedFactory` and `trading_research_agent.feeds`. `tra data fetch|audit --venue` and
  `tra trade shadow --feed` take any registered name; shadow defaults to the venue in
  `[run].venue_key`.
- `data.normalise` keeps only what every layout shares; the venue URL builders and
  normalisers moved into their plugins.

### Added

- Column maps: `tra data audit --mapping mapping.toml` takes any CSV through the same audit,
  manifest and Parquet contract. `examples/mapping.toml` documents every field.
- Architecture tests for the adoption rules: no decision core inside the package, no venue
  named outside the plugin directories, no example imported by the package.
- `docs/venues.md`: how to add a venue.

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
