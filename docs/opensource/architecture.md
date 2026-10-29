---
description: Package layout, dependency direction and the file formats that form the contract of TRA.
---

# Architecture

The product is one chain of gates. A claim about a strategy passes the data gate, the
falsification chain and the execution gate, in that order, before the agent may say it. This
page fixes the package boundaries, the direction of dependency, and the files that cross
those boundaries. The Python API is allowed to change before 1.0; the file formats are the
contract.

## Packages

```
trading_research_agents/
  records/     how every record reaches the disk: crash-safe appends, atomic
               writes, hashes, the per-day cycle index                          depends on nothing
  extras.py    how to add a missing optional dependency, worded once            depends on nothing
  ledger/      verdict vocabulary, axes, transferability, kill-ledger rows      depends on nothing
  envelope/    operator parameters: fees per channel, latency, venue access     depends on nothing
  forecast/    the instrument's own candles drawn forward and read at a trade's
               levels; a logistic fit, Platt and isotonic calibration, the
               expected value of a side; numerics only                          depends on nothing
  data/        any trade data to Parquet, dataset audit, manifest; venue
               archive plugins in data/venues, column maps for the rest      -> ledger, envelope
  chain/       falsification chain, layers 0 to 5                               -> ledger, envelope, data
  execution/   cost identity, fills after latency, trade-through matching,
               fill probability, markout, orders whose send went unanswered,
               stops measured by ATR or by the paths                            -> ledger, envelope, data, chain, forecast
  risk/        the gate every new order passes: hard caps, the loss at the stop
               recomputed, switches, unanswered sends, today's trades and losses;
               signs the intent it approves                                     -> execution
  strategy/    the core protocol, exits bound to the mechanism, the walk-forward
               harness; no strategy of its own                                 -> everything above
  trading/     the live feed through ccxt in trading/feeds, simulated venue, the
               live venue through ccxt in trading/venues, controls, canary caps,
               ladder, the runtime                                                     -> everything above
  llm/         language-model providers, fallback chains, usage ledger, response
               cache; the typed judge port and its adapter                      depends on nothing
  store/       snapshots and cycle directories                                  -> everything above, llm
  surface/     the gates as callable tools, read tools and tightening actions
               (listed on the MCP page); shared by mcp and agents                -> everything above
  readings/    macro, on-chain and sentiment: the operator's series against the
               operator's thresholds, a declared state and a graded score; no model -> data
  agents/      the research roles at layers 0, 2 and 3; the session roles; the
               cycle and its seats and reviewers; the session's written rules     -> everything above, llm, store, surface, readings
  mcp/         the gates as MCP tools                                            -> everything except agent
  market_data/ one-minute flow and book bars, derivatives and liquidations, dated
               text, gap marks; the verified archive in day files and hour parts,
               compaction, the recorder; ccxt only in market_data/venues        -> data
  features/    figures computed at a bar's close from what was known then:
               price, path, order flow, derivatives, cross-asset                -> market_data, execution
  perception/  archived headlines read by the typed judge into text events     -> llm, market_data, features
  evaluation/  views, triple-barrier settlement with the desk's costs,
               baselines, the forecast and paths rows, the arena and its records -> features, market_data, data, envelope, execution, forecast
  cli/         the `tra` command                                                 -> everything
```

Rules, each enforced by a test in `tests/test_boundaries.py`:

1. `ledger` and `envelope` import nothing else from this project. They are vocabulary.
2. Arrows point only downward through the list. `chain` does not know `execution` exists; layer 5
   takes the cost of a trade as a function argument, and `execution` supplies one.
3. Only `llm/providers` imports a vendor SDK. Layers 0, 1, 4, 5 and 7 are arithmetic and
   statistics and have no path to a model.
4. Source files are ASCII.
5. No class in the package implements `Strategy` or `FittedCore`. Decision cores live in
   [`examples/strategies/`](https://github.com/BijectiveLabs/bitra/blob/master/examples/strategies/) or in the operator's own package, and the package never imports
   an example.
6. No module outside `data/venues/`, `data/sources/`, `market_data/venues/`, `trading/feeds/` and
   `trading/venues/` names a venue.
7. `mcp` reaches everything except `agent`, and its tools only read.
8. The only actions on `surface` tighten a limit, step the ladder down, or arm the kill
   switch. A role holding the surface holds nothing that loosens.

One more rule is held by behaviour rather than by imports: every new order passes
`risk.evaluate`, and the order hand refuses an intent that does not carry the gate's signature for
that very intent, made in this run and still valid. Closing or flattening a position needs no
approval. `tests/test_mandate.py` and `tests/test_cycle.py` break if either lock is removed.

## Adoption rules

Five rules decide what goes into the package and what stays outside it. They exist so the
software can be adopted without inheriting the particular strategies, venues or vendors of
the research it descends from.

1. **No strategy in the core.** The package ships the mechanism (the protocol, the exits, the
   walk-forward harness) and no strategy. What [`examples/strategies/`](https://github.com/BijectiveLabs/bitra/blob/master/examples/strategies/) holds is an example of
   the mechanism, not a recommendation, and a run points at it by file like it would at any
   other core. Rule 5 above enforces it.
2. **Venues are plugins.** Archive layouts and live feeds are found through a registry and
   an entry point group; the two that ship are plugins like any other. The gates, the
   chain, the runtime and the agent do not know which venue a run is on. Rule 6 enforces it;
   [Venues](/opensource/venues) says how to add one.
3. **The data gate takes any data.** A venue archive through its plugin, or any CSV through a
   declared column map ([`examples/mapping.toml`](https://github.com/BijectiveLabs/bitra/blob/master/examples/mapping.toml)), ends in the same audit, the same manifest
   and the same Parquet. Nothing about where a row came from changes what the gate demands
   of it.
4. **Agents are a surface, with full access.** The roles and the MCP tools call the gates;
   they never are the gates. A model may decide (the structure lead is a decision core);
   the system executes and refuses (controls, caps, ladder, kill switch). Rules 3 and 8 enforce it.
5. **Every market is one shape.** A market is read once from ccxt's unified market structure
   into `trading.market.Market`: its type, what it settles in, whether it trades in contracts
   and how large one is, its fees and its minimum. A spot pair, a perpetual, a dated future, an
   option and a prediction outcome are the same type with different facts, so the order path
   asks the market what applies (contracts, margin, leverage) rather than branching per venue or
   per market type. Which types venue mode sends to today is one list in `trading/dispatch.py`;
   [Boundaries](/opensource/boundaries) says why the rest wait.

Not every package exists yet. The list is the plan; a package is added when its first gate
lands with tests. Today: `ledger`, `envelope`, `data`, `chain`, `execution`, `strategy`,
`trading`, `llm`, `store`, `agents`, `mcp`, `market_data`, `features`, `evaluation`, `readings`,
`perception`, `forecast`, `records`, `surface` and `cli` (one module per command family; the console
face on `rich` is `cli/tui.py` with `cli/tui_cycle.py`, `cli/tui_panels.py` and `cli/tui_desk.py`,
which read reports and decide nothing). Outside the package: `examples/` (an envelope, a run
config, a column map, two example cores), never imported by it; `templates/` inside the
package is a copy of those examples that `tra init` writes into a new project, and is never
imported either.

## One system, every venue

The desk does not carry an adapter per exchange. It speaks to every venue through ccxt, and
everything ccxt reports is turned into the package's own types once, at the edge. Past that edge
a venue is a name in the settings, and the parts below work the same for all of them.

| Part | What it does | Where |
|---|---|---|
| Data | candles, books, funding and open interest from any venue ccxt supports, chained with fallbacks | `data/sources/` |
| Client | one synchronous client for every venue, whichever ccxt namespace it lives in; prediction venues' async clients run on a loop of their own | `data/sources/ccxt_client.py` |
| Market | one shape for every market type, read from ccxt's unified market, a prediction outcome included | `trading/market.py` |
| Order | the entry, its exits attached, one per decision, under a client id from the cycle | `trading/dispatch.py` |
| Execution | a taker entry as a limit IOC inside a slippage band, a maker entry post-only with a timeout | `trading/dispatch.py` |
| Risk | the last gate before an order: hard caps, the loss at the stop, switches, unanswered sends, today's trades and losses; it signs what it approves and the order hand checks the signature | `risk/` |
| Order state | waits for the fill, cancels what is left, and records what the venue said in its own words; a create that went unanswered is looked for by its client id, and one not found is UNKNOWN until reconciled | `trading/dispatch.py`, `execution/unknown.py` |
| Position | reconciled with the venue on every pass; closed by time or by the kill switch, reduce-only | `trading/positions.py` |
| Account | equity, free margin, the margin an order ties up, the day's loss and the drawdown | `trading/capital.py` |

It is deliberately small. It holds what an autonomous desk needs to send an order safely and know
afterwards what happened, and no more; a full order and portfolio platform is a different product.

`tra live`, the research ladder's canary tier, sends through the same client and hand as venue
mode, so the order path is one path whichever command sends, and the public trade stream that
`tra trade shadow` and `tra live` read is ccxt's too. What is left venue by venue is what ccxt does
not unify: the historical trade archives in `data/venues/`, and the account checks in
`trading/venues/accounts.py`.

## The market data plane

Everything a decision or a replay reads about a market comes through one plane, and the plane is
code: no model touches it. The same bar builder makes a minute whether it is fed by the venue's
daily archive or by its live stream, so a replay and a live decision read the same object.

```mermaid
flowchart TB
    subgraph venue["one venue, through ccxt (market_data/venues only)"]
        ws["streams<br/>watch_trades, watch_order_book, watch_liquidations"]
        hist["REST history<br/>funding, open interest, mark, index, long/short"]
        daily["daily trade archive"]
        news["news feeds declared in sources/news.toml"]
    end
    subgraph recorder["tra record: one process, the only writer of what it records"]
        flow["flow track<br/>FlowBarBuilder: venue time, a grace for reordering,<br/>duplicates dropped, late trades counted"]
        book["book track<br/>one sample a second into BookBar"]
        points["points track<br/>levels by five-minute history backfill,<br/>liquidations as they stream"]
        text["text track<br/>each headline once, at first sight"]
        hours["hour parts<br/>written when the hour can no longer change"]
    end
    subgraph backfill["tra data bars"]
        audit["the data gate audits the archive"]
        builder["the same FlowBarBuilder"]
    end
    archive[("data/market<br/>day files and hour parts,<br/>a manifest with sha256 beside each")]
    compact["compaction<br/>a closed day joined, verified, parts removed;<br/>unseen hours written as missing"]
    readers["readers and the BarFeed port<br/>a file whose bytes changed is refused"]
    judge["typed judge, optional<br/>headlines into typed events"]
    features["features<br/>41 figures known at the bar's close"]
    arena["the arena<br/>replay, settlement, statistics"]
    ws --> flow
    ws --> book
    ws --> points
    hist --> points
    news --> text
    flow --> hours
    book --> hours
    points --> hours
    text --> hours
    hours --> archive
    daily --> audit --> builder --> archive
    hist --> archive
    archive --> compact --> archive
    archive --> readers --> features --> arena
    readers --> judge --> features
```

What each failure means, and what the plane does about it:

| Failure | What it means | What the plane does |
|---|---|---|
| a stream drops | coverage is lost from the last message | the minute it dropped in is partial, the minutes after it are missing until a message arrives, the span goes to `gaps.jsonl`, and the stream reconnects after 1 s doubling to 60 s, with jitter |
| a connection goes silent | its keepalive notices it, within about 15 s at the recorder's 5 s ping | the stream is unseen from its last message; hours are held three minutes so minutes already closed are marked too |
| a trade arrives after its minute closed | the venue or the network was late | counted on the minute while its hour is open; counted in the events log once the hour is written; never added to a closed minute |
| the same trade arrives twice | a reconnect replayed it | dropped by its trade id |
| a trade carries a venue timestamp before the bar being built | the stream reordered it | placed by venue time inside the grace, so the archive and the stream build the same bar |
| a level is published after its hour was written | an open-interest bucket closes late | the hour is written again, merged, until its day is compacted |
| the recorder stops or starts mid-hour | nobody watched part of the hour | the unwatched minutes are missing, marked in `gaps.jsonl`; a later run in the same hour fills only the minutes the earlier one missed |
| a file is edited after it was written | its bytes no longer match its manifest | every reader refuses it and `tra runs verify --market` names it; it is never repaired |
| a venue publishes no history for a series | nothing to keep | recorded as unsupported, and every feature that needs it is absent with that reason |

State lives in files, not in a shared store: one writer per file, hour parts read only once
written, and the rate limit split by a declared `requests_per_minute`. The `BudgetStore` and
`BarFeed` protocols in `market_data/ports.py` are where a platform running many desks would put a
shared store; nothing in the package needs one.

## How a decision is made, now and next

```mermaid
flowchart LR
    subgraph now["the cycle today"]
        direction TB
        f1["features and briefs, by code"] --> r1["readings, by code"]
        r1 --> m1["deciding team<br/>lead, blind confirmer, flow; code combines"]
        m1 --> g1["gates: cost, exits,<br/>confluence, conviction"]
    end
    subgraph next["the decision being built: in the arena today, live in 0.13"]
        direction TB
        f2["features, by code"] --> fc["forecast and calibrator<br/>a probability fitted on outcomes"]
        j2["typed judge<br/>text events today,<br/>claims checked from 0.13"] -.-> fc
        s2["reasoning model<br/>woken only when worth it"] -.-> fc
        fc --> ev["one expected-value decision<br/>p x take - (1 - p) x stop - cost"]
    end
    g1 --> gate["risk gate, by code"]
    ev --> gate
    gate --> ex["execution: one order, exits attached"]
```

Both paths end in the same risk controls and the same order path, and the arena replays both over
the same archive before the new one replaces the old.

## The session, as it runs

```mermaid
sequenceDiagram
    participant C as system
    participant A as structure lead (core)
    participant U as auditor
    C->>C: figures per dataset, cost floor, layer 1 when declared
    C->>C: screen by rule: kept / excluded, a reason each
    loop every closed bar, per kept instrument
        C->>A: compact bars, features
        A-->>C: target, confidence, horizon
        C->>C: controls admit or refuse, the venue fills, exits run
    end
    C->>C: risk by rule: continue / demote / halt, on the measured figures
    C->>C: allocation by rule: an equal share each (nothing is sent)
    C->>U: run reports, ledger, ladder, usage (a bounded tool loop)
    U-->>C: verdict, findings
```

The screen, the risk ruling and the allocation are written rules (`agents/session_rules.py`):
the same answer every time, at no cost, each naming the figure that decided it. The macro and
on-chain readings, when series are declared, are read between the screen and the runs: their briefs are measured by the system, read against the operator's thresholds by code, and
ask no model.

## The chain

| Layer | Test | Dies here when | Cost to run | Status |
|---|---|---|---|---|
| 0 | Cost floor | plausible magnitude below the cheapest round trip | minutes | implemented |
| 1 | Market observation | the phenomenon does not recur | minutes | implemented (`tra chain observe`) |
| 2 | Mechanism memo | nobody can be named who is structurally forced to lose | minutes | implemented (model writes, the system checks the form, a critic objects once) |
| 3 | Pre-registration | the claim cannot be stated so data could refute it | hours | implemented |
| 4 | Data requirements | no available instrument can measure the premise | hours to days | implemented |
| 5 | Statistical validation | interval spans zero, loses to a matched null, or lives in one subperiod | days | implemented |
| 6 | Strategy design | the exit is not bound to the mechanism | weeks | implemented |
| 7 | Shadow | net expectation negative in the live market, zero orders placed | weeks of calendar | implemented (`tra paper`, then layer 5 on its trades) |
| 8 | Live capital | the venue does not behave as the simulation assumed | capital | canary tier only (`tra live`), hard caps, behind the ladder |

The chain is strictly ordered by cost. A hypothesis does not reach layer 5 because somebody is
confident about it.

## Files at the boundaries

| Artefact | Format | Minimum content |
|---|---|---|
| Dataset | Parquet; price and quantity as `DECIMAL(38, s)` with `s` the instrument's scale; timestamps UTC nanoseconds | trades: `ts`, `venue`, `instrument`, `price`, `qty`, `side`, `trade_id`, `seq` |
| `manifest.json` | JSON | schema version, venue, instrument, UTC range, source files with SHA-256, normaliser version, audit verdict, creation time |
| `preregistration.json` | JSON, sealed by content hash | mechanism, claim, metric, horizon, universe, era, cost model, power, kill criteria; amendments are separate dated files referencing the original hash and may never loosen a threshold or move a horizon |
| `ledger.jsonl` | JSON Lines, append-only | one row per entry; nothing is rewritten; a retraction is a new `WITHDRAWN` row naming the row it retracts |
| `envelope.toml` | TOML | one `[[channel]]` per venue and execution channel; see [`examples/envelope.toml`](https://github.com/BijectiveLabs/bitra/blob/master/examples/envelope.toml) |
| `trades.jsonl` | JSON Lines | layer 5 input: `instrument`, `entry_ts_ns`, `exit_ts_ns`, `direction`, `weight`, `gross_return_bps`, numbers as decimal strings |
| `validation.json` | JSON | layer 5 output: interval, null report, subperiods, power, metrics, the decision with every fired criterion, the registration seal and the trades hash |
| `seal.json` | JSON, written once | era, opened_at, specification hash, gates |
| `strategy.toml` | TOML | a run of the trading agent: the core by file or import, exit rule, run settings, controls; see [`examples/strategy.toml`](https://github.com/BijectiveLabs/bitra/blob/master/examples/strategy.toml) |
| `mapping.toml` | TOML | a column map that takes any CSV through the data gate; see [`examples/mapping.toml`](https://github.com/BijectiveLabs/bitra/blob/master/examples/mapping.toml) |
| `intents.jsonl`, `fills.jsonl` | JSON Lines | every intent with its admission decision; every simulated fill |
| `ladder.json` | JSON | current tier and the history of promotions (with evidence and signature) and demotions (with reason) |
| `funding.json` | JSON | settlement timestamps (epoch ns) to rates as fractions, the measured interval, the venue and the range; written by `tra data funding`, read by `[run].funding` |
| `preflight.json` | JSON | what a live venue checked before the first print: clock skew, key permissions, position adopted |
| `usage.jsonl` | JSON Lines | one model call per line: role, provider, model, tokens, latency, declared cost, outcome |
| `cycle.json` | JSON | one cycle: data, agents, gates, intent, placement, refusals, and `files`, a sha256 per file in the cycle directory |
| `inputs.json.gz` | gzip JSON, schema 1 | a cycle's raw inputs as fetched (candles per timeframe, book levels, funding, open interest) and the settings its briefs used |
| `index/<day>.jsonl` | JSON Lines | one line per cycle: id, instrument, mid, held, intent, placement, open interest, and the sha256 of `cycle.json`; the last line for a cycle wins |
| `session.json`, `audit.json` | JSON | a session of the seven roles: what was kept and why, the runs, the risk decision and the limits after it, the allocation, the auditor's verdict |
| `market/<venue>/<instrument>/<stream>/<day>.parquet` | Parquet, zstd; prices and sizes `DECIMAL(38, s)` | one stream of one market for one UTC day: `flow_1m`, `book_1m`, `deriv`; dated text under `market/text/<feed>/text/` |
| `.../<stream>/<day>/part-<HH>.parquet` | the same schema | one hour of an open day, until compaction |
| `<file>.manifest.json` | JSON | stream, venue, instrument, day and hour, rows, sha256, partial and missing minutes, sources, scales, schema and builder versions |
| `market/<venue>/<instrument>/gaps.jsonl` | JSON Lines | every span a stream or an archive did not cover, with the reason |
| `market/events.jsonl` | JSON Lines | the recorder's events: started, connected, disconnected, gap_closed, latency, skew, late_trades, late_points, compacted, stopped |
| `runs/arena/<id>/arena.json`, `ideas.jsonl`, `run.json` | JSON, JSON Lines | an arena run: configuration, archive hashes, rows and coverage, byte-stable; every idea with its settlement; when it ran |
| `cache/llm/<key[:2]>/<key>.json` | JSON | one model call keyed by role, model, prompt version, brief, sampling and sample index |

## Numbers

- Basis points to one decimal place, leading minus for negatives, never parentheses.
- Times in UTC, always stated as such. A naive datetime is refused.
- Prices, quantities, fees and costs are `Decimal`. Floats appear only in statistics.
- Every quantity that carries a verdict is reported as a triple: point estimate, interval,
  sample size.

## What the platform boundary looks like

This project is consumed by other systems through the files above and nothing else. It does
not import from, link to, or know about any proprietary trading platform. A system that wants
its research tier gated by these rules reads the Parquet, the manifest and the ledger.
