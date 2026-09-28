# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) once 1.0 is reached.

## [Unreleased]

### Changed

- The project is now one thing, not two: autonomous trading agents, with the falsification
  chain as the safety rail rather than the product. The README says so.
- The package is `trading_research_agents` and the distribution is `trading-research-agents`;
  the command stays `tra`. There is no compatibility alias, because nothing was published
  under the old name.
- The licence is MIT. `NOTICE` is kept as a record of third-party software, not as an
  obligation of the licence.
- New layer `llm`: the completer protocol, the structured-output parser, the call budget with
  `usage.jsonl`, and the vendor adapters. It sits beside the gates rather than inside the
  agents, because the auditors and any host may call a model without an agent. `agent` is now
  `agents` and holds only roles, the session, the tool loop, evals and traces. The import
  boundary test carries the new map.
- `ProviderError` lives with the completer protocol instead of inside the Anthropic adapter,
  and the schema-parse error and the role error are one class (`llm.form.FormError`, exported
  as `agents.RoleError`).
- `requirements.txt` carries the runtime pins generated from `uv.lock`, with the extras
  spelled out in comments.

### Fixed

- A cycle now hands each source what the operator declared for that kind in `data.toml`. It was
  passing only the candle window it needed, so `levels`, `params` and anything else in a
  `[data.<kind>]` table were silently dropped on the `tra run` path while `tra data pull` honoured
  them.

### Added

- `tra check` now says when a provider's SDK is not installed, with the extra that installs it,
  instead of reporting the provider ready and failing in the middle of a cycle. A `Provider` carries
  what it needs (`anthropic`, `openai`, `google.genai`) and whether it is importable.
- The README's install block installs from the source tree, because the package is not on PyPI yet
  and `pip install trading-research-agents` does not work today. `uv tool install --editable .` puts
  `tra` on the PATH, and each extra says what it buys.
- `tra venues [name...]`: what a venue can actually do through ccxt - which kinds of data it
  serves, whether it takes orders, whether it can carry the stop and take profit with the entry,
  whether the account reads a cycle needs are there, and whether it has a test environment. Read
  from the library with no network, no key and no order, because "ccxt supports a hundred venues" is
  true and not the question. Measured today: bybit, hyperliquid and okx do everything a cycle needs;
  binance takes orders but cannot attach the exits; kraken is spot-shaped and serves neither funding
  nor open interest.
- `docs/venues.md` gains the two sections an operator actually needs: adding a venue they trade on
  (ask `tra venues`, then the three places that must agree) and stopping one (take it out of the
  chains; a kind with no source is refused rather than guessed).
- `cli/ui.py` is now `cli/tui.py`, which is what it is.
- `[agents].fallback` and `[agents.roles.<role>].fallback`: `provider:model` entries tried in order
  when a provider cannot be reached, with the same brief, since a role call carries no conversation.
  Every attempt is recorded with the vendor's own error class; a link is built the first time it is
  needed; and a spent budget stops the chain rather than falling through to another provider.
  `llm/chain.py`, checked by `tra check` against the registered providers.
- `usage.jsonl` records `cached_tokens` and `cache_write_tokens` per call, so prompt caching can be
  measured rather than assumed - on short role prompts it often will not engage at all, and now
  that is visible.
- `docs/llm.md`: how models are used here - no orchestration framework and the criteria that would
  change that, what memory means in a project whose roles are stateless, provider fallback, prompt
  caching with the minimum prefix that makes it pay, why batching belongs in the eval path and not
  in a cycle, and why fine-tuning is a position rather than a gap.
- The stop and the take profit are sent to the venue attached to the entry, computed by code from
  `[exit]` against the price the gate priced on. A venue that cannot take them with the entry is
  refused rather than trusted, and `[execution].exits = "none"` is the only way to leave a fill
  unattended - `tra check` lists it as a thing to fix. Any open position or any working order on
  the instrument now refuses a new entry, so a second cycle cannot quietly double a position.
- `tra run --every 5m [--cycles N]`: a cycle on the interval's own boundary, in one process, ending
  by name on the budget, the kill switch, the count asked for, or a keyboard interrupt. A refusal
  does not end the loop; the next cycle decides again with fresh figures.
- `tra check` reads the three files against each other: whether the instrument is mapped for the
  sources, whether data comes from a venue other than the one whose fees are priced, and whether
  `[execution].venue` and `[run].venue_key` name the same venue - the last is a refusal, because an
  order would go to one venue at the other's fee.
- `tra init --profile autonomous`: a project with the cycle already wired - the technical analyst as
  the core, the confluence matrix, the conviction ladder and the targets as live tables, execution
  in sandbox, and a "your first hour" section in the project's own README naming the three things
  that are the operator's. The gates ask for nothing until they are given something, so a fresh
  project refuses nothing it was never told about, and `tra check` names the provider placeholder
  in its own words instead of passing a registry error through.
- `tra init --agent` no longer truncates the rest of the configuration when it writes the live
  `[agents]` table: the gates and `[data]` stayed behind in the template and are now kept.
- `docs/boundaries.md`: where this software stops and why, in one page - one order per decision
  and no retries, no routing or execution algorithms, no account or treasury operations, a chain
  that can be priced but not traded, no optimiser in the research chain, no model that can size a
  trade or change a file, files rather than a database, and no production tier. Each boundary says
  what to do instead, and the last section names the tests that keep them true. Linked from the
  README and from `docs/liability.md`.
- Two auditors (`tra audit`, `agents/tech_auditor/`, `agents/quant_auditor/`): one over the
  machinery, one over the desk's own results, both reading only records this project wrote and
  neither able to reach a venue or a key. Each finding must cite figures from its brief with their
  values, and may carry one proposed configuration change; code checks every citation and every
  proposed key against the project's own tables, discards what fails and keeps the reason. The
  report goes to `audits/<time>/` as `audit.json` and a readable `proposals.md`, and nothing in it
  is ever applied by this package.
- `agents/cited.py` holds the citation rule both the validators and the auditors are held to: the
  figure must exist with that value, a leaf name is resolved when it can only mean one figure, and a
  figure the record does not have is citable as `null`, because absence is a fact about the record.
- The order follows the channel. A channel's name is `<entry>-<exit>`, each `maker` or `taker`, and
  `Channel.entry_role` is now read by the venue hand: a maker entry rests on the book as a post-only
  limit at the snapshot's own touch and is cancelled after `[execution].limit_timeout_s`, a taker
  entry crosses with a market order, and a channel that says neither is refused rather than guessed
  at - sending a market order under a maker-priced channel pays a fee the gate never priced.
- Every order carries a client id derived from the cycle and instrument, so a cycle run twice cannot
  open two positions; the account is read before anything is sent and an order is never added to a
  position pointing the other way unless `[execution].adopt_open_position` says so; a hand that
  cannot read the account does not send. `[execution].params` is handed to the venue verbatim.
- `[[channel]].gas_quote`: a fixed cost in quote currency per round trip, for chains that charge
  settlement separately, priced as its own `gas` component with `declared` certainty.
- The execution mandate (`[execution]`, `execution/mandate.py`, `trading/dispatch.py`): three modes,
  `sandbox`, `signals` and `venue`. Venue mode does not start until the operator has printed the
  mandate text with `tra mandate`, put it in their own project word for word, signed it with a name
  and a date, capped one order with `max_notional`, promoted the ladder to `CANARY`, declared
  `[canary]`, and put the venue's keys in the environment. `tra check` and `tra run` both name
  whatever is missing, and neither spends a token before refusing. The order goes out as one market
  order through ccxt, sized by the conviction rule, and what came back is written to `placed.json`
  beside the intent that was already on disk.
- Conviction (`[conviction]`, `execution/conviction.py`): what the analysts say now decides how much
  capital goes behind an idea, not only whether it happens. Each role's declared state contributes
  its weight for or against the direction, silence contributes nothing, the deciding analyst's
  weight is scaled by its own confidence, and each validator vote is worth `validator_weight`. The
  score picks a rung off the operator's ladder, and every cap still applies afterwards:
  `max_position`, the notional ceiling implied by `max_leverage` against `[run].equity`, and the
  venue's `size_step`. When the size is not the base size, the round trip is priced again at the
  size that is going out.
- `execution/sizing.py` holds the arithmetic of how large an order may be, in one place. The
  executor role calls it instead of keeping its own copy, and `[run].size_step` declares the venue's
  increment.
- Every role inside its own job, enforced in four places rather than asked for in a prompt:
  `agents/scope.py` declares what each role reads, answers and decides; the cycle filters every
  brief through it, so no reading analyst receives equity, size, the controls, the cost of the round
  trip or the exit distances; the schemas stay closed; and nothing downstream reads a model's
  sentence. A reason that uses the language of an order, a size or an exit is recorded as a crossing
  beside the answer and changes nothing.
- The confluence matrix (`[confluence]`, `execution/confluence.py`): which roles must agree with the
  direction, what agreement means, and how validator votes are counted. It reads the declared state
  code derives from the operator's thresholds, never the label a model wrote, and it fails closed - a
  required role that produced no state has agreed to nothing. `tra check` refuses a matrix that can
  never pass.
- Targets (`[targets]`, `execution/targets.py`): `min_net_bps`, `min_reward_risk` and
  `max_round_trips_per_day`, applied by code before an intent exists. The round trips are counted
  from the intents already on disk, so a restarted process cannot forget what it has traded today.
- Validators (`agents/validator/`, `[agents].validators`): three stances of one role - pro, contra,
  neutral - asked once there is a proposal, in parallel. Every claim must cite a figure from the
  brief with its value; code discards a figure that is not there and a value that does not match,
  records both, and turns a verdict with no surviving claim into `abstain`. A claim may round the
  figure it cites.
- The source shelf: `sources/macro.toml`, `sources/news.toml`, `sources/sentiment.toml`,
  `sources/onchain.toml`, `sources/derivatives.toml`. One declaration per class of data rather
  than one per vendor, so replacing a provider is an edit on one line and nothing else in the
  project moves. `tra init` writes all five as forms with the entries commented out; a form
  declares nothing, so an untouched project asks only the technical analyst. The older layout
  (`series.toml`, `onchain.toml`, `sentiment.toml` beside the config) is still read.
  `agents/context.py`.
- Dated text is its own class: `[[news]]` entries name files of `when,what[,source]` rows, read by
  the macro analyst as events and by the sentiment analyst as notes. Several files per class and
  several classes per role are both allowed, which is how the on-chain analyst reads chain and
  derivative figures side by side. `data.series.load_news`.
- `[data.<kind>].params` is handed to the venue verbatim, so a venue's own spelling lives in the
  operator's file instead of in this package. Measured on 2026-09-28: asked plainly, Bybit answers
  `fetch_open_interest` with the last hourly bucket, so the figure arrives up to an hour old;
  `params = { intervalTime = "5min" }` brings it back to about two minutes. The shipped `data.toml`
  declares it.
- `tra check` reads the shelf without reading the data: what each file declares, an export that is
  declared but not on disk, and the roster the next `tra run` will ask. `tra run` takes one flag
  per class (`--macro --onchain --derivatives --sentiment --news`), each overriding one file.
- Four analysts in one cycle, asked at the same time: the technical analyst that decides, and the
  macro and news, on-chain and sentiment analysts that read. Each one gets a brief built by code
  from the snapshot and the series the operator mapped in, each answer is recorded with its tokens
  and latency, and a role with no declared source says so instead of answering neutral.
  `agents/sentiment_analyst/`, `agents/briefs.context_brief`.
- `[agents.roles.<role>]`: a role may run on its own provider, model, output cap, timeout and
  retry count, and inherits everything it does not override. A role that only labels does not need
  the model that reasons over sixty bars.
- A series file may date its rows with a bare `YYYY-MM-DD`, read as UTC midnight, which is how most
  public series publish. A time of day without a timezone is still refused, because that one is
  genuinely ambiguous.
- Every closed round trip records what it went through, not only what it earned: `mfe_bps`,
  `mae_bps` and the bar each happened on, in `trades.jsonl`. The risk manager's brief summarises
  them over a run, including the median of what the winners gave back before they closed.
  `execution/excursion.py`.
- The cycle: `tra run` takes one snapshot, builds each agent's brief in code, asks the agents in
  parallel, prices the round trip on the book it is holding, and writes an intent or a refusal
  with its reason. Everything it touched stays in `runs/cycles/<cycle>/`: the snapshot, the
  briefs, the answers, the cost, the verdict. `agents/cycle.py`, `agents/briefs.py`,
  `agents/parallel.py`, `docs/cycle.md`.
- `execution/pretrade.py`: what a trade costs before it exists, component by component, each one
  labelled `declared`, `measured`, `computed` or `estimated`. The gate admits a claim only when it
  clears the total plus the operator's margin, and it records where the claimed edge came from so
  a declaration is never read as a measurement.
- Agents run on a small thread pool with their answers written as they arrive, so a cycle stopped
  by a rate limit keeps what it paid for and `--resume` reuses it. A failed agent is a line in the
  record, not the end of the cycle.
- `tra check` reads `data.toml` and says when a cycle could not price a trade for want of a book
  chain.

- `data/sources/`: live sources behind a chain. The operator declares an order per kind of data
  in `data.toml`, the first source that answers inside the declared freshness wins, and every
  attempt, every stale value and every refusal is recorded. `ccxt:<venue>` reads public
  endpoints of more than a hundred venues through the `[ccxt]` extra and is never given a key;
  `file:data` answers from the days already in `data/`, so a Client with no keys still runs. A
  Client's own vendor registers under the `trading_research_agents.sources` entry point.
- `store/`: the snapshot a cycle is decided from, with provenance and age on every value; the
  cycle directory that holds a cycle's papers for replay; and a file cache that only returns
  what is still inside its time to live.
- `data.schema.Candle` and `data.candles.candles_of`: the published interval, and one way to
  fold a tape into it. Layer 1 now uses them instead of its own private aggregate.
- `tra data pull [INSTRUMENT]`: one snapshot through the declared chains, written to
  `runs/cycles/<cycle>/snapshot.json`, with a console view of what answered and what did not.
  It exits refused when any kind was refused or came back stale.
- Datasets written before the package was renamed still read: the Parquet marker is accepted
  under both prefixes.

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
- `agents/react.py`: a bounded tool loop with a closed catalog, checked arguments, cut
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
- CI also installs the built wheel into an empty environment and runs the command, audits
  the lock against OSV (`uv audit`), and builds and runs the container image.
- The container image builds its environment at the path it runs from; the previous image
  copied a venv whose scripts named an interpreter that no longer existed.
- `tra agent session` exits refused when the session never reached its audit verdict.
- A `9router` preset (a local router at `http://localhost:20128/v1`, `9ROUTER_API_KEY`);
  an answer without a usage block no longer crashes the OpenAI-compatible adapter: the call
  is counted with zero tokens and the usage line says `ok, no usage reported`.
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
  the `trading_research_agents.live_venues` entry point, and one bundled adapter (v5 REST,
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
  `trading_research_agents.venues` entry point; live feeds in `trading/feeds/` behind
  `LiveFeedFactory` and `trading_research_agents.feeds`. `tra data fetch|audit --venue` and
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
