# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) once 1.0 is reached.

## [Unreleased]

### Added

- `tra init --venue` writes a complete `[execution]` block for that exchange: its demo or testnet
  API, 3x isolated leverage, at most 200 per order, and `exits = "separate"` where the venue takes no
  stop with the entry. An agent desk made with `--venue` sizes orders in quote currency
  (`size_quote = 100`), which means the same on every instrument. The next steps it prints include
  what to set once at that exchange.
- `tra mandate --sign "Your Name" --venue-mode` signs and turns venue mode on in one step.
- `tra keys --prepare`: the probe, and on a demo or testnet API an account in long/short mode with
  nothing open is set to one-way. Never on mainnet.
- `tra keys --probe` reads the execution venue's account as soon as its keys are set, before venue
  mode is on, and fills blank fee lines first, so it works on a desk straight from `tra init`.

### Fixed

- With keys set, fees are read through the client orders go through, and the standard tier comes
  from the market list of the environment orders go to; a venue's slow public list no longer stops
  the read (Gate's timed out while its testnet answered). A key that cannot read fees but can trade
  (Gate keeps fees behind wallet read) is no longer reported as something to fix.

### Documentation

- Why it is built this way: the documented failures the design answers (language models trading
  real money, overtrading, wrong numbers in financial answers, instructions hidden in data, agents
  with authority over funds, automation without pre-trade controls, regulators' expectations), each
  with its source, and what we found building it.
- Validators and orchestration described as the judged case everywhere they appear.

### Changed

- Trade results are out of the source and the docs: the figures from our demo desks and from the
  research this package descends from (net per trade in bps, win rates) are replaced by what they
  showed in words, and report examples show their format, not a desk's numbers. Results will be
  published when there is a sample large enough to audit.

## [0.9.0] - 2026-10-20

### Added

- `[execution].exits = "separate"` for a venue that takes no stop with the entry, Binance futures
  first: the stop and take profit follow the fill as two reduce-only orders. A fill they cannot be
  placed for is closed at once; exits left resting by an earlier position are taken off before the
  next entry, and an entry is refused while one will not come off; a close by time takes them off
  too. Refused on spot and on a venue whose resting exits are not read yet. `tra venues --setup`
  suggests it where it applies. Run on Binance's demo API and Gate's testnet API on 2026-10-20.
- A venue's leverage is confirmed by its own reply where ccxt cannot read it back (Hyperliquid),
  and the record says so; a reply that is an error still refuses. Before, every derivative order
  there was refused for want of a read-back.
- An order's status is found in the venue's open and closed lists where ccxt has no single-order
  read (BloFin, Lighter).
- `tra venues` names what the order path needs beyond sending: order status, balance, leverage and
  fills. Gate joins the venues whose exits placed after the fill are read (price orders), and
  venues that sign for an account id (WOOFi Pro, ModeTrade) are asked for `<VENUE>_ACCOUNT_ID`.
- `[execution].ccxt_options`: a table set on ccxt's client for the execution venue, over the
  package's own defaults. Credentials are refused in it.
- Bitget's unified trading account. A key with only unified grants is recognised when the client is
  built and sent through Bitget's unified API; the leverage is set and read back in the declared
  margin mode, every order names that margin mode, and hedge mode is refused from the account's own
  settings. Run on Bitget's demo API on 2026-10-20: entries with exits attached, a refused second
  entry, a reduce-only close, a cancelled post-only order, a close by the venue's own stop, the kill
  path.
- Binance futures through `venue = "binance"`: a futures symbol sends the client to the futures
  API (ccxt's `binance` reads the spot wallet otherwise), the position is read from the position
  list, a balance ccxt did not parse is read from Binance's own rows, and a whole leverage is sent as
  an integer. Run on Binance's demo API on 2026-10-20 with exits placed after the fill.
- Gate and BingX on their practice APIs. Gate: a futures symbol uses the futures account and loads
  only the markets of its kind, the leverage is read from the position list, and client order ids
  carry Gate's `t-` prefix within its 28 characters. BingX: a futures symbol uses the swap account,
  its demo's VST counts as the USDT its contracts settle in, and the leverage is set for both sides
  of a one-way position. Both run end to end on 2026-10-20.
- When a venue key is missing and a similar name is set (`GATE_SECRET_KEY` for `GATE_API_SECRET`),
  `tra check` and `tra keys --probe` name both, never the value.
- Parquet files of your own: `tra data audit FILE.parquet --mapping mapping.toml` reads trade
  history through the same column map and audit as a CSV, and a `[[series]]` may name a `.parquet`
  file. A zoned timestamp is kept to the nanosecond; one without a zone is refused.
- The technical analyst sees its own last reading of the instrument inside the window it reads
  (`previous`: when, the target, setup, confidence and levels, the move since), so a reversal comes
  with its reason. What became of that reading is not shown. Validators do not receive it.

### Changed

- The analyst and the validators propose without the desk's rules. The technical analyst's prompt no
  longer describes what code accepts (the cost multiple, the reward-to-risk default, the stop bounds,
  the risk per trade, what the regime gate refuses); validators no longer receive the round trip or
  the claimed edge, and the claimed edge could be an operator's declared magnitude. A test keeps
  those settings out of both roles' briefs and prompts.
- The analyst's own target is recorded beside the one applied (`proposed_take_bps`, as
  `proposed_stop_bps` already was), and `tra hindsight` replays both sets of levels on the same path
  (`levels` in the report): what the operator's bounds did to the analyst's reading, measured.
- ccxt's Hyperliquid builder fee (1 bp on every order, on top of the venue's fee and outside the
  round trip the gates price) is off by default on every client that signs with the operator's
  keys. `ccxt_options = { builderFee = true }` turns it back on.
- Docs, site and messages say demo API and testnet API rather than account: the software reaches
  an exchange's practice environment through its API, not through an app.
- Every analyst reads only its own data. The technical analyst no longer receives the other
  analysts' declared states (`readings`) or the derivatives positioning series, which the on-chain
  analyst reads; the validators no longer receive the declared states. The readings are weighed once,
  by code, in confluence and conviction. A test holds each brief to its own data.
- The validators work as a judged case. With pro, contra and neutral declared, pro and contra build
  the strongest case for and against the proposal apart, neither seeing the other's; code checks
  every claim; neutral judges the two checked cases, without the advocates' verdicts, and its verdict
  is the only one confluence and conviction count. Code decides without asking neutral when only one
  side has a case. Measured before: pro supported 63 times in 64, contra opposed 61 in 63, and a
  neutral asked alone abstained in 33 of 37 cycles.
- An answer that does not fit its schema is asked for once more with the reason after the brief,
  rather than with the same brief again; the event analyst gets the same second call.
- The console's separator lines take the home screen's colour, so they no longer read as text.

### Fixed

- The margin mode is read back with the leverage: a symbol the venue reports in cross margin while
  `[execution].margin_mode` is isolated refuses the order, as a wrong leverage already did. Both
  refusals now quote what the venue answered when it would not change the setting (on the Bitget
  demo API: a key that could not reach its futures settings).
- A fee ccxt reports as negative on a taker fill is counted as paid, since no venue pays a taker;
  on Bitget's unified account the fee is read from Bitget's own fill. Before, a round trip there
  would have shown its fees as a rebate.
- An order is refused when the venue's book, read at the moment of sending, is stamped more than
  30 seconds ago; `tra keys --probe` says so. Binance's demo API served a book 3.2 hours old on
  2026-10-20, 35 bps from where its own trades printed.
- A close carries a client order id of its own, as entries always did; without one, ccxt put its
  own broker id on a Binance close.
- A filled order the venue reports without an average price is priced from its own fills, so the
  entry's slippage is recorded (Bitget's unified account).
- In venue mode, a book or funding source from a plugin is now held to the execution venue too. It
  is asked which venue it serves, and one that does not say is refused, where before only `ccxt:`
  sources were checked.

### Documentation

- Venues: a "Broker tags" section with what ccxt adds to orders on each venue, and the default
  this package sets on Hyperliquid's builder fee.
- Your own data: a page on bringing trade history, series and vendor feeds in CSV, JSON or Parquet,
  the rules every file is held to, and how each is stored (datasets as Parquet that DuckDB, Polars
  or pandas open directly).
- Working with language models: what a role is given, measured; how an answer that does not fit or
  cites nothing is handled, with the counts from a demo desk; the analyst's last reading.
- Agents: what goes in and what comes out of every role in a cycle, in one table.
- Exchange accounts and keys: making a key on each venue, for the practice environment and for live
  trading, with the settings each one needs and what has been run end to end.
- For exchanges: how the software reaches a venue, what "run end to end" checks, and how orders
  identify themselves.

## [0.8.0] - 2026-10-18

### Added

- Lines a client draws for a day, each counted from the records on disk so a restart does not reset
  it, and each holding new entries while open positions keep their exits:
  `[agents].daily_token_budget` and `[agents].daily_cost_budget` (every role and both auditors,
  across runs), `[controls].max_trades_per_day` (every instrument), `[controls].max_losing_streak`,
  and a pause switch (`PAUSE` by default, `[controls].pause_switch`). `tra pause` and `tra kill`
  set the switches and `--off` lifts them; `tra check` prints every line in force on one row.

- Spot, on the same order path as perpetuals. A desk lists it in `[execution].markets` (the default
  stays perpetuals only). Read from the market's own facts: long only, since a short on spot needs a
  borrow; paid in full against the free quote balance, with no leverage set; the position is the
  coin the account holds, with residue under the venue's minimum counted as nothing; exits attached
  with the entry; a close cancels the resting exits wherever the venue keeps them (OKX keeps them as
  algo orders) and sells what is held, rounded down to the venue's step; a fee charged in the coin
  bought is counted in the quote currency. In the cycle, a short on spot is refused by code before
  it is priced, and funding and open interest are not asked for. Run end to end on the Bybit and
  OKX demos on SOL/USDT, and as a full desk cycle in sandbox and venue mode on the Bybit demo.
- A channel can carry a spot pair's fees beside the perpetual's, as a `[channel.spot]` table, and a
  spot cycle is priced at them; the base fees stay the perpetual's. `tra check` compares the base
  fees with the perpetual and the spot fees with the pair, and names the market when one is priced
  below what the venue charges.
- Stock perpetuals and tokenized stocks, through the same order path as crypto: a stock perpetual
  is a linear perpetual and a tokenized stock is a spot pair. What the underlying is comes from the
  venue's own fields (Bybit `stock` and `xstocks`, OKX `instCategory` 3) or from
  `[execution].equities`. With `[execution].equity_session = "regular"`, the default, a cycle on an
  equity waits while the US regular session is closed and refuses a hold past its close; holidays
  are declared in `[execution].equity_closed`; the on-chain analyst is not asked. Run on the Bybit
  demo (AAPLX/USDT) and the OKX demo (TSLA/USDT:USDT).
- Prediction market outcomes, as a third market type a desk lists in `[execution].markets`. ccxt's
  prediction venues (Kalshi, Polymarket and others) are reached like any exchange: their async
  clients run behind a synchronous bridge, so one client type serves the whole package. An outcome
  is decided by a new role, the event analyst, which answers with a probability; code prices the
  share to resolution (`probability - ask - fee`) against the margin, sizes it so its whole cost
  fits the risk per trade, rests the take profit at the venue after the fill, sells at the venue's
  floor to close, and writes a share held to resolution up at its payout. The channel declares the
  fee as `[channel.prediction]`, a coefficient on `price x (1 - price)` or a flat rate.
- A taker price is held inside a venue's published price band (OKX), where an order priced past it
  is refused whole.
- `venue` as a source in `data.toml`: the venue the desk trades on, from `[run].venue_key`, with
  the book read from the environment orders go to. The shipped chains use it, so a desk reads one
  venue by default and changes venue by changing `venue_key`. `params` may be keyed by venue,
  `{ bybit = { intervalTime = "5min" } }`, so one venue's spelling never reaches another.
- Data the analysts are handed, from more places, in one `fetch` line each:
  - the venues' public positioning, free and without a key: `binance-futures`, `bybit` and `okx`
    presets for long/short account ratios, taker buy and sell volume and open interest history,
    refreshed every five minutes;
  - paid vendors by their published references: `coinglass` (liquidations, open interest,
    long/short, taker volume across venues), `glassnode`, `cryptoquant` and `arkham`, each with
    its key read from the environment and sent where the vendor asks for it;
  - `params` for any fetch, and `tra sources add --param FIELD=VALUE`; rows that arrive as arrays
    are read by position.
- News and public sentiment: a `[[news]]` entry fetches an RSS or Atom feed or a JSON news API into
  its file, as plain dated lines kept for `keep` and refreshed every `refresh`; `tra run` and
  `tra sources pull` refresh them. A feed that declares a document type is refused.
- `positioning` in the technical brief: the derivatives series declared for the traded asset, with
  each one's change over the last hour, age and declared state.
- `[conviction].reader_labels`: how strongly a reading analyst's own label counts where its series
  declare nothing (headlines, say), provided it cited what it read. 0, the default, keeps labels
  out.
- `tra hindsight`: every directional decision a desk recorded, replayed on the candles that followed
  it with its own stop, take and hold, grouped by the gate that stopped it. Each gate's refusals
  are set beside what it let through, with a verdict that says when the sample is too small, and
  the deciding model's side is set beside its reverse, resampled by day. Paths are kept under
  `runs/cycles/hindsight/`, the report in `runs/cycles/hindsight.json`, and the quant auditor
  reads it. A ccxt source takes `since_ms` for a window that starts at a given time.

### Changed

- The MCP tools follow one verb-noun pattern: `check_cost_floor`, `show_ledger`, `show_ladder`,
  `read_run_report`, `search_ledger` and `list_runs` replace `cost_floor_check`, `ledger_show`,
  `ladder_show`, `run_report`, `ledger_search` and `runs_index`; the other four keep their names.
- The welcome screen has no frame: TRADING RESEARCH above AGENTS from 124 columns, TRA below that,
  the ASCII name on a console without block characters. The one-line wordmark is gone.
- `[agents]` refuses a key it does not know, as `[controls]` already did, so a misspelt budget is
  an error rather than a budget that is silently not there.
- Market structure, read by the system on the decision timeframe and every context timeframe:
  alternating swings labelled HH, HL, LH, LL, the trend they make (uptrend, downtrend, range), the
  last break of structure or change of character (closes only), the level whose loss changes the
  trend, and the nearest unbroken support and resistance. The technical brief carries it as
  `market_structure`, `tra run` prints it beside the regime, and a trend setup that points against
  the structure is refused unless the structure has just changed character. The analyst's
  instructions told it to place a stop beyond the swing from "the bars themselves", which it was
  never given; they now name the figures it has.
- A range fade is refused where the window's moves have been carrying on: the technical brief
  carries `variance_ratio` (four-bar against one-bar return variance), and `range_reversion` needs
  it below `[regime].fade_below_variance_ratio`, 1 by default, 0 to turn it off. The analyst's
  instructions name `range_breakout` or flat for a range whose moves carry on.
- Every MCP tool says what it is for, when to use it against its siblings, what it returns and
  that it only reads, and every argument carries a description in the input schema. The server's
  instructions give the order of work an idea follows through the tools.
- Glama builds the MCP server from the spec on its admin page, not from a file in the repository;
  `glama.json` no longer names one, and `glama/Dockerfile` records that spec
  (`uv sync --locked --no-dev --extra mcp`, `uv run --no-sync trading-research-agents mcp serve`).
  CI checks that both start and list their tools.
- The welcome panel spells the name: TRADING RESEARCH AGENTS on one line from 185 columns,
  TRADING RESEARCH above AGENTS from 129, and TRA below that, each block centred, with the package
  name, the tagline and the version centred beneath.

### Fixed

- In venue mode, a `data.toml` that priced orders on another venue's book or funding was accepted.
  It is refused before anything is pulled: the book and the funding an order is priced on are the
  execution venue's own. Candles may still fall back to another venue deliberately.
- A close the venue accepted but did not fill was recorded as closed. It is now confirmed against
  the account: when the account still holds the position the unfilled close is cancelled, the
  record says it did not fill, the exits at the venue keep standing, and the next pass tries again.
- The quant auditor never saw a venue desk's results: it read closed round trips only from session
  runs, while a venue desk records them in `runs/cycles/round_trips.jsonl`, so every scheduled look
  reported none closed. It now reads both; rows written before the net was kept in bps are stated
  from their net, entry and size.

## [0.7.1] - 2026-10-17

### Added

- `glama/Dockerfile`, named in `glama.json`, which Glama builds the MCP server from: the package
  with its `[mcp]` extra, served over stdio by a non-root user. CI builds the image on every change
  and checks that it starts and lists its tools.

### Changed

- The console's welcome header is the TRA wordmark in block letters, which fits a terminal of 31
  columns. A console whose code page has no block characters gets the ASCII wordmark instead.

## [0.7.0] - 2026-10-16

### Added

- `glama.json`, which lets the maintainers claim the server's listing on Glama.

### Changed

- `tra live`, the canary tier, sends through ccxt on the same client and `VenueHand` as venue
  mode, so every exchange ccxt knows is a live venue and there is one order path whichever command
  sends. What it checks before the first print is the same or stricter: the clock, the account's
  position mode, OKX's account mode, Bybit's key permissions (now read through ccxt), the market
  type and contract size, and a position the run was not told to adopt. A size off the venue's
  step is refused rather than rounded, and a send that failed in transit is looked up by its client
  id before anything is sent again. Client ids are now `tra<run><seq>`, letters and digits only.
  Run end to end on the Bybit demo (ETHUSDT) and the OKX demo (BTCUSDT, one contract of 0.01 BTC):
  entry, reduce-only close, flat, fees as charged.
- The public trade stream for `tra paper` and `tra live` is ccxt's streaming side, so every venue
  ccxt streams is a feed, with no key. Trades on a contract market are turned into the base, a
  trade sent twice is passed on once, and one older than the last is counted and not passed on.
  The `[shadow]` extra now installs ccxt.
- `tra venues --setup` no longer prints a `[live]` table; venue mode is what it sets up, and
  [The live tier](https://docs.bijectivelabs.dev/opensource/live) covers `tra live`.

### Removed

- The direct Bybit REST adapter, `trading_research_agents.trading.venues.bybit`. Bybit runs through
  ccxt like every other venue. A plugin registered under `trading_research_agents.live_venues`
  still takes precedence for its name.
- The Bybit websocket feed, `trading_research_agents.trading.feeds.bybit`, and with it the
  `websockets` dependency. Bybit streams through ccxt like every other venue.

## [0.6.8] - 2026-10-16

### Added

- The package also installs its command as `trading-research-agents`, next to `tra`, so
  `uvx --with "mcp>=2,<3" trading-research-agents mcp serve` starts the MCP server without an
  install. `server.json` describes the server for the MCP Registry, and the MCP page has a section
  on using it from your own agent. Each release now also publishes that entry to the MCP Registry,
  after PyPI, through GitHub's OIDC with a pinned and checksum-verified publisher.

### Changed

- One headline everywhere, naming no market: "Autonomous trading agents with deterministic
  execution. The agents propose; the system decides." It replaces "An autonomous trading system
  for crypto" in the README, the docs, the PyPI summary, the citation and the console. What it
  trades today is unchanged and still stated where it is described: linear perpetuals, run end to
  end on Bybit and OKX.
- Every market is read once into one type, `trading.market.Market`, from ccxt's unified market
  structure: type, settlement currency, contract size, fees and minimum. The order path asks it
  what applies instead of reading raw venue fields, which is the ground spot, tokenized stocks and
  prediction markets are built on. Nothing about what is sent changes; a new test pins the
  perpetual order byte for byte and checks that the same decision sends the same order twice.
- What kind of market a desk may send to is one setting, `[execution].markets`, defaulting to
  `["linear perpetual"]`, the one type built today. A type that is not built yet is refused when
  the config is read, by name. The round-trip record reads its currency from the market's
  settlement instead of splitting the symbol.
- The architecture page describes the desk as one system: data, markets, orders, execution, order
  state, positions and the account, all through ccxt and the package's own types, with no adapter
  per exchange.

### Fixed

- Order records wrote some quantities in scientific notation: ten contracts of 0.001 were recorded
  as `1E+1`, and a fill on a venue with a large contract could read `2E+7`. Contracts, contract
  size and filled amounts are now written in plain decimals. Orders themselves were never affected.
- The MCP server reported an empty version to the hosts that connect to it; it now reports the
  package version and the documentation link.

## [0.6.7] - 2026-10-16

### Changed

- The README and the docs say where the project is going, in a new "Where it is going" section:
  linear perpetuals are what it trades today, with spot and the other market types on the way;
  every venue ccxt supports is integrated and ready for its end-to-end run, with Bybit and OKX run
  so far and a report wanted for the others.
- Tagline: "An autonomous trading system for crypto. The agents propose; tested rules decide."
- The branches are spelled out: `master` is the stable line and every commit on it is a release;
  `develop` is the experimental line and a commit there is not a release. Users are pointed at the
  release on PyPI; links to the examples and the changelog, and the README's CI badge, follow
  `master`.
- A market type that is not built yet is refused with "venue mode trades linear perpetual futures
  today, and other market types are not built yet".

### Fixed

- Boundaries said a decentralised venue could be priced but not executed, while Hyperliquid goes
  through the same venue hand as any ccxt venue. The row now covers on-chain venues outside ccxt.
- Liability described Boundaries as what the software will not do by design; it now says what that
  page holds: the guarantees, and what is not built yet.

## [0.6.6] - 2026-10-15

### Added

- The docs site can be found and read by search engines and language models: a sitemap and
  `robots.txt`, a description, canonical link and social card on every page, structured data
  naming the package, its version and its repository, `llms.txt` and `llms-full.txt`, and a plain
  Markdown copy beside each page. Each deploy tells IndexNow engines which pages changed.
- The landing page links to the docs with a real link and says in a few lines what the package
  is, so a crawler that does not run scripts still finds both.
- `CITATION.cff`, so GitHub offers a citation; more PyPI keywords and classifiers.

### Changed

- One address per role: the product page `www.bijectivelabs.dev/open-source` is the homepage on
  PyPI, in the citation and in the README's web badge; the docs stay at `docs.bijectivelabs.dev`;
  company links go to `www.bijectivelabs.dev` without a redirect.
- Docs: the venues page says Bybit and OKX have both been run end to end on the demo API, matching its own
  table; the providers page names all three providers with a default model.
- Locked dependencies move to newer minor and patch releases: anthropic 1.13, openai 3.28,
  google-genai 2.29 and mcp 2.3; ruff 0.17 and mypy 2.4 for development.

### Security

- The docs site's build tools move to vite 6.4 and katex 0.18, closing five advisories against the
  local docs server. They affect writing the docs only; the package and the published site are
  unchanged.

## [0.6.5] - 2026-10-13

### Fixed

- The pass over the universe measured each instrument on data.toml's own candles (one-minute bars,
  four hours) while the cycle decided on fifteen-minute bars, so the screener and the analyst could
  read two different regimes for the same instrument in the same pass (ETH "trending up" to the
  screener, "ranging" to the analyst). The pass now measures on the cycle's bars and window.
- The console's tagline matches the README's.

### Changed

- `tra init --agent groq` and `--agent google` no longer ask for `--model`: they use the models
  that have run live cycles here (`openai/gpt-oss-120b`, `gemini-flash-latest`), as `anthropic`
  already did. Other providers still ask for one.
- The README and the quick start install every provider in one command
  (`[ccxt,openai,anthropic,google,data]`), say how to upgrade, and end with a table of what a new
  desk does before anything is changed. Missing-extra messages suggest the same install.
- The README's PyPI and Python badges refresh within half an hour of a release.
- The screenshots in the README and the docs are taken from the current console.

### Added

- Issue forms for bugs, features and venues that ask for the version, the command and its output;
  blank issues are off, and questions go to Discussions (`SUPPORT.md`).
- Repository housekeeping: pull requests are labelled by what they touch, issues and pull requests
  with no activity for 30 days are marked stale and closed 14 days later, Dependabot proposes
  grouped updates monthly, at most two open at a time, and its patch and minor ones merge once CI passes, and CodeQL scans the
  Python and the workflows.

## [0.6.4] - 2026-10-13

### Changed

- In `tra run` the technical analyst chooses the setup from the measured regime and the higher
  timeframes, with its own desk prompt. `[agents].mechanism` is the operator's thesis, the setups
  they favour when the market offers them, and no longer the only thing the analyst may trade. The
  research core in `tra backtest` and `tra agent session` keeps its registered mechanism.
- The market screener picks where a setup has the best chance given each instrument's regime, not
  where one mechanism might work.
- `range_reversion` needs a measured range, as the trend setups need a measured trend: it is now
  refused in a mixed market with a measured trend strength. A regime that could not be measured
  still refuses nothing.
- `tra init --agent` writes a mechanism that names no setup: "trade the setup the measured regime
  supports: with a trend, inside a range, or flat".

## [0.6.3] - 2026-10-13

### Fixed

- `tra keys --probe` stopped with `No module named 'google'` when a fallback named a provider whose
  SDK was not installed: asking whether `google.genai` is installed imports `google` first, and
  that raised instead of answering no. The probe now names the missing SDK and the extra that
  adds it, and goes on to the next key.
- `tra check` says when a provider in a fallback chain cannot load, as a note: the chain skips that
  link when it is reached, so the desk still runs.
- Every message about a missing optional dependency gives the same advice, for a uv tool install
  and for pip alike, instead of a pip command that does not fit an install made with uv.
- `tra models` with a provider whose SDK is missing names the SDK and the extra, instead of
  `ModuleNotFoundError`.
- `tra venues` with nothing named and no project lists the venues and what each can do, instead of
  refusing; `tra venues NAME` exits 1 when it cannot answer for NAME.
- A command run outside a desk says the folder is not a desk and how to make one, instead of
  `[Errno 2] No such file or directory`; `tra` alone there suggests
  `tra init my-desk --profile autonomous`.
- The auditors read candle age with the bar it belongs to, so a fifteen-minute bar ten minutes
  after its close is no longer reported as stale data.
- The technical analyst is told that a stop inside one ATR is moved out to it and that the target
  is measured against the stop that results, so it stops naming targets the bounds will refuse.

### Added

- `scripts/client_smoke.py` runs the commands a new client types first against an installed
  `tra` and fails on any crash; CI runs it on a bare install and on the quick start's extras.

## [0.6.2] - 2026-10-13

### Fixed

- The console listed and completed commands by their internal names, so `help` showed `run run`,
  `check check` and `backtest replay`, and tab offered words that were not commands. It now lists
  and completes every command as it is typed: `run`, `check`, `backtest`, `data get`.

## [0.6.1] - 2026-10-12

### Changed

- A stop the analyst names inside one average true range of the decision timeframe is widened to
  it (`[exit].min_stop_atr`, 1 by default, 0 turns it off). On 15-minute bars a stop held only to
  the 20 bps floor sat inside one bar's ordinary range and was hit by noise; the wider stop makes
  the position smaller, not the loss bigger.
- The README and the quick start have one way in: install from PyPI, `tra init --agent --model`,
  the key in `.env`, `tra check`, `tra run`. Installing from a clone is described under
  Development and in CONTRIBUTING. The path to real orders is three steps for an agent desk, with
  the research path for a scripted strategy described on its own.
- Docs brought in line with the code: `--every 15m` throughout, no `--env-file .env` where the
  `.env` beside the config is read anyway, the ladder at CANARY only on mainnet, `max_notional`
  among what venue mode needs, the risk manager's part in `tra run`, OKX run end to end on its
  demo API, and fees filled in from the account rather than left blank.
- The screen `tra` opens on is called the console everywhere; the word cockpit is gone from the
  docs, the code and the screenshot names.
- The README published to PyPI points its images at this release's tag, so each version's page
  keeps its own pictures.

### Fixed

- The risk manager in `tra run` compared a day's net in money with the daily loss limit in basis
  points and said the limit was exceeded when it was not. Its brief now names the unit of every
  figure and gives today's result in basis points of equity beside the money.
- `tra live` read keys only from `--env-file`; it now reads the `.env` beside `strategy.toml`
  like every other command, and `tra check` no longer asks for `--env-file .env`.
- A desk made by `tra init --profile autonomous` opens its README with what is left to do before
  the first cycle, and no longer asks for a dataset that only a backtest needs.
- The README's images are read from the repository again, so they show on GitHub while it is
  private; the copy published to PyPI points them at GitHub, rewritten at build time.
- The CI badge is GitHub's own, which shows to anyone who can see the repository; the docs build
  badge, which had no runs to report, is gone, and the Python badge reads the versions from PyPI.

## [0.6.0] - 2026-10-11

### Changed

- When the analyst names a target that survives the `[exit]` bounds, that target is the move the
  cost gate and the targets weigh, recorded as `edge_source`; the break-even bound prices the
  stops and the round trip against it. Without a named target, `[agents].magnitude_bps` stands as
  before. A constant written for one-minute bars had refused most trades on a 15-minute desk.
- The stop and the target are placed right after the round trip is priced, before the validators,
  so a trade the exits refuse is never paid for with votes.
- The default `[exit].timeout_bars` on an agent desk is four hours of bars on its own timeframe
  (240 at 1m, 16 at 15m), and `tra init --agent` writes 16. A measured stop is sized over the
  same span, so 240 fifteen-minute bars no longer push every stop to `max_stop_frac`.
- The documentation, the README, the commented templates and the console say "the system"
  where they used to say "code" for what the program itself does, and "scripted strategy" for
  what was called a code core.
- Stops sized by volatility: `[exit].mode = "volatility"` measures each trade's stop as
  `stop_atr` average true ranges on the decision timeframe, grown with the square root of the
  horizon and held between `min_stop_frac` and `max_stop_frac`; the take profit is
  `reward_to_risk` times the stop. `tra init --agent` writes it. Fixed fractions stay as
  `mode = "fixed"`, the default. The record carries the ATR, the horizon and any bound that held.
- A take profit inside the round trip cost refuses the trade.
- `[controls].max_drawdown_bps`: no new entry once equity is that far below its highest reading,
  across days; `on_drawdown = "flatten"` also arms the kill switch. Wakes the quant auditor once a
  day. `tra init` writes 1000.
- Simpler setup. `[controls]` figures have standard defaults (risk per trade 50 bps, daily loss
  300 bps, 6 orders a minute; `max_position` optional). An agent desk can leave out `[exit]`
  (volatility stops, 240-bar timeout) and the research cost figures in `[run]`. Venue mode on a
  demo or testnet API no longer waits on the ladder, and no longer needs a `[canary]` table,
  which only `tra live` reads. `[execution].max_orders_per_cycle` is out of the template.
- Unknown keys in `[controls]` are refused.
- `tra init --agent` writes a 15-minute decision timeframe with 1h and 4h as context.
- The risk manager acts in `tra run`: asked with the quant auditor, it may `reduce` size, `pause`
  instruments or `halt` through the kill switch, each for `hold_hours`, never loosen. State in
  `runs/cycles/risk.json`, decisions in `risk_decisions.jsonl`; `[audit].risk_manager = false`
  turns it off.
- `[exit].mode = "analyst"`, the agent-desk default: the technical analyst names `stop_price` and
  `target_price`; the system holds the stop inside `min_stop_frac`/`max_stop_frac`, refuses a target
  closer than `min_reward_to_risk`, brings in one past `max_reward_to_risk`, and falls back to the
  measured stop. The brief now carries `range_high`, `range_low`, `atr` and `atr_bps`.
- `[targets].max_break_even_win_rate` (default 0.5) refuses a trade whose stop, target and round
  trip would need more than that share of winners.
- Every intent records `break_even_win_rate`, the share of trades that must reach the take profit
  to cover the stops and the round trip; the cycle output prints it.
- `tra mandate --sign "Your Name"`: writes `MANDATE.md` when the project has none and signs
  `[execution]` with the name and today's date (`--on` for another). Never overwrites a
  `MANDATE.md` someone wrote, puts the config back if the result does not load, and does not
  switch venue mode on.
- `tra check` no longer counts a stale series as something to fix: it is shown, not counted, and
  never stops the desk.
- The quant auditor wakes on a clock (`[audit].quant_every = "24h"`, kept across restarts),
  when `[controls].daily_loss_limit_bps` is reached (once per UTC day, no longer a technical
  alarm), and after `[audit].losing_streak` losing round trips in a row (3 by default).
- Docs: why the exits are built the way they are, and a table of when each auditor is asked.
- Unknown keys in `[exit]` are refused.
- A ccxt data source can read a venue's practice environment: `ccxt:okx@testnet`, `ccxt:bybit@demo`.
  `tra init --venue` points the order book there, so a desk trading a demo or testnet API prices,
  checks drift and measures slippage on the book its orders meet. Seen on OKX's demo: 9.22 bps of
  "drift" that was the gap between the live and the demo book.
- The sentiment prompt asks for figures named in full, as the macro and on-chain ones do.
- `[execution].max_orders_per_cycle` was accepted and never enforced; the venue hand always sends at
  most one order per decision. Any value other than 1 is now refused instead of implying more.
- Removed code nothing called: a parallel helper, a refusal property, a bootstrap property, an
  excursion property, a ledger id list, a mandate wrapper used only by its own test, and a contract
  conversion helper superseded by the round-trip path.
- README: banner, then the title and line centred, then the badges with an insights badge, then
  the text.
- A decision older than `[execution].max_decision_age_s` (default 120 s, from the start of its cycle)
  is not sent. Each sent order records `decision_age_s`, `send_ms` and `slippage_bps`, measured
  live, and the cycle output prints them.
- Account settings ccxt does not unify are checked before the first order and in `tra keys --probe`
  (`trading/venues/accounts.py`). The first: an OKX account in Spot mode, which refuses every
  perpetual order with sCode 51010, is named with the setting to change.
- Everything an order is sized, stepped or resolved by is read from the market list of the
  environment orders go to, not the live one. A demo or testnet API is not a copy of live: OKX's demo
  listed 155 linear perpetuals against 485 live, and 50 of the 138 on both had another contract
  size or lot (MET: 1 coin a contract live, 10 on the demo). Discovery still ranks on the live
  market and keeps only what the environment lists, with its steps.
- `[exit].stop_sigma_frac` and `take_profit_sigma_frac` are `stop_frac` and `take_profit_frac`:
  they were always a fraction of price, not of a sigma. The old names still load.
- A setup has to point the way its own figures point. A trend setup needs a measured trend, so it is
  refused in a mixed market as in a ranging one; a range fade must fade the stretch; an exhaustion
  trade must go against the move; a breakout trades the side the close left from. Seen live: a
  pullback-in-trend short in a mixed market rising 53 bps.
- A venue that answers "no position" by raising (OKX through ccxt raises `NullResponse`) is read as a
  flat account, not as an account that cannot be read.
- The clock check takes three readings and believes the quickest. The first request on a new
  connection carries the TLS handshake and made a clock 0.6 s behind look 1.2 s behind.
- `tra keys --probe` reads the account before the mandate is signed; a key that cannot reach the
  account is worth knowing first. A refused key shows in `tra check`.
- Fees fill themselves. `tra check` and `tra run` read the maker and taker fee from the account
  and write them on blank `envelope.toml` lines, labelled. Without keys, or on a demo or testnet API
  that does not report fees, the venue's standard tier from its market list is written instead,
  labelled, and replaced by the account's own once it reports one. A fee the operator wrote is kept
  and checked: one below what the account pays is reported.
- `tra models <provider>` lists the models the key can use, from the vendor's own list; no
  prompt is sent. `tra init --agent` takes a provider's default model when it has one.
- `tra sources add <class> <name> --from <preset> --high bullish|bearish` puts a fetched series on
  the shelf in one step: pulled, declared with the source's own timing, and with lines drawn from
  its own history (quartiles by default), labelled as such.
- `tra init --venue <ccxt id>` writes a desk for that exchange: data chains, venue key, execution
  venue and practice environment, the keys that venue signs with, and a fee file of its own.
  Orders stay in sandbox mode.
- `tra venues <venue> --setup` reads your maker and taker fees from the account when the keys are
  set, in basis points, and no longer prints a `[live]` table for a venue that has no live-session
  plugin or a size increment to copy. `--environment` picks the account it reads.
- Sizes use the venue's own step when `[run].size_step` is not declared, converted to the base.
- A blank fee in `envelope.toml` is reported with its line and what to write, not as a TOML error.
- `[execution].environment` (`demo`, `testnet` or `mainnet`) says where orders go. It falls back to
  `[live].environment`, then `demo`. A `[live]` table for a venue with no live-session plugin no
  longer breaks `tra check` and `tra keys`.
- The order book reads levels that carry more than a price and a size (OKX sends four values); before,
  it refused the venue's book outright.
- A funding rate with no timestamp is dated when it was read. It was dated at the venue's next
  settlement, in the future, so it read as fresh with a negative age.
- Discovery reports the 24-hour change to four places.
- Venue mode now declares its margin mode to the client, so a venue that defaults to cross margin
  (OKX) trades and reads leverage as isolated when isolated is what the operator declared.
- The client order id is letters and digits, 32 at most, which every venue accepts; the old form
  carried hyphens that OKX rejects.
- An account in long/short position mode is refused before any order, with the way to fix it.
- OKX stock and commodity perpetuals are not crypto in discovery (190 and 9 of its 485 perpetuals).
- An instrument is named by base and settlement currency (`BTCUSDT`, `ZECUSDC`) and resolved on the
  venue orders go to, as a linear perpetual. On OKX, Gate, KuCoin and Hyperliquid, whose ids are
  spelled differently, the same name now reaches the right market, and a name that is also spot on
  Binance or Bitget is no longer read as spot. Discovery names markets the same way.
- Orders and positions are converted between the base and the venue's contracts. Before, a size in
  the base went out as an amount in contracts, which is wrong on every venue whose contract is not
  one unit of the base (OKX Bitcoin is 0.01, Gate 0.0001, OKX PEPE ten million). Discovery reports
  steps and minimums in the base. A venue that does not report its contract size is refused.
- The correlation reference is Bitcoin on whatever venue is traded, not the name `BTCUSDT`.
- The documentation is a VitePress site (`docs/`), published to docs.bijectivelabs.dev from
  `master` through GitHub Pages (`.github/workflows/docs.yml`). The pages that lived in `docs/` as
  plain Markdown moved to `docs/opensource/`, with a new introduction, quick start, command
  reference, security page and contributing page. References in the code and templates point at the
  site.
- `SECURITY.md` describes what the package does today: venue keys in memory, orders in venue mode.
- A series can name the asset it is about (`asset`, with `elsewhere` for how much it counts on other
  instruments). Bitcoin's chain no longer weighs the same on a Zcash trade as on a Bitcoin one.
- `[trigger].look_valid_for` (default 1h): a last look older than this counts as a first look, and
  "moved since the last look" is measured against the latest pass over the universe, not only the
  last cycle, which for most instruments was hours ago.
- A position the venue closed is reported with its currency and its return in bps of the entry
  notional: `net 1.9327 USDT (193.3 bps of the entry notional)`.
- The scheduler says when the next pass ends on the clock (`next pass at 14:30:00 UTC, in 10m 57s`)
  and why it is shorter than the interval: passes land on multiples of `--every`, so a desk started
  at 14:16 on `--every 30m` waits until 14:30, not until 14:46.
- When the screener takes none of what code admitted, the pass says so, instead of blaming the
  trigger for the one instrument that did not move.
- The macro and on-chain prompts ask for a figure named in full when two series carry the same field;
  a bare `state` was thrown away as ambiguous.
- README follows the order a person meets the tool: the console, the screen pass, one cycle, then
  the result.
- A pass holds every candidate before any model is asked when the desk already carries
  `[desk].max_open_positions`; those cycles could only be refused at the desk step, and their calls
  counted against the provider's rate limit (seen live: a validator rate-limited on a cycle that
  was refused anyway).
- In venue mode an instrument with a position already open at the venue is skipped by the pass and
  held by the cycle before any model is asked, unless `[execution].adopt_open_position` is set; the
  venue hand would refuse the entry anyway, so asking the roster only cost tokens.
- The default channel stays `taker-taker`, and the templates and README now say why and how to
  switch to `maker-taker`.
- **Conviction weighs each voice by how strong it is, not by its word.** A reading analyst's
  score is now graded from its own figures: how far each series sits past the operator's line, in
  widths of the neutral band between the bullish and the bearish line, signed and between -1 and 1.
  The score is the net of every voice over the weight it expressed, so weak opposition costs a
  little size, strong opposition costs a lot, and only opposition that outweighs the case refuses
  the trade; silence neither lifts nor dilutes the deciding analyst's confidence. Before this, a
  macro reading barely past its line counted as a full voice against and could zero a clear case
  (seen live on 2026-10-04). The report and the panel show each voice against its weight.
- A reading analyst's declared state is recorded even when its model did not answer: it is code
  over the operator's thresholds and does not depend on the model.
- **Nothing caps what a role may spend any more.** `token_budget`, `max_calls_per_hour`,
  `min_interval_s` and `max_output_tokens` are all optional, and undeclared means counted rather
  than capped: an answer cut off halfway is a wasted call and a worse decision, not a saving. Every
  call is still a line in `usage.jsonl` with its tokens, latency and price, every cycle reports what
  it spent, and the auditors read those records and propose where to spend less. An operator who
  wants a hard ceiling declares one and `tra check` says what it will do. The Anthropic API requires
  `max_tokens` on every request, so an undeclared cap becomes a generous default there and nothing
  at all for the other vendors.
- `tra init` no longer writes a token budget or a rate limit into a new project, and no longer
  writes a zero price that reads as free. `effort` is written commented with the vocabulary beside
  it, so a model with no reasoning to configure is left alone until an operator asks.
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
- The README is rewritten around what a newcomer looks for first: what it solves, a quick start,
  what the terminal shows, the venue and provider tables, and the path to real orders.
- The Anthropic adapter defaults to `claude-opus-5-5`.
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

### Added

- **Fewer tokens for the same decisions** (measured: the reading analysts were 37 % of all tokens,
  validators 27 %, output 37 %). A reading analyst's answer is reused while its brief's figures are
  unchanged (`[agents].reuse_reading_for`, default 1 hour; any new value asks again at once). The
  validators are not asked when code is certain to refuse regardless of their votes (desk, round
  trips, reward to risk, an edge that fails even without impact). Table-reading roles take
  `effort = "low"` where an effort is declared. Briefs are sent without JSON spacing, and cached
  prompt tokens are read from OpenAI-compatible servers that report them.
- `[universe].type_field` and `exclude_types` set which market types discovery leaves out on any
  venue (Bybit's `symbolType` is the default there), and `quote` the settlement currency; every
  part of the code side of the screen is now the client's to configure.
- `docs/boundaries.md` now separates guarantees (enforced by code and tests, kept by every change)
  from what is not built yet (spot and other market types, DEX execution, more than one account,
  transfers, routing, portfolio accounting), with what a contribution in each area would need; the
  README and `CONTRIBUTING.md` say the same. Before, the scope read as a list of things the project
  refused.
- **Discovery and a screener that chooses.** `[universe].discover` adds the venue's most traded
  linear perpetuals each pass (one market list and one ticker call), filtered by code for volume,
  spread, listing age, crypto only and the desk's leverage; each carries the venue's own symbol and
  size step. Every candidate is measured by code (move and pressure, regime, z-score, trend strength,
  volume, 24-hour volume and change, correlation with BTC). With `screener = true` the screener
  now picks and orders the candidates, naming the setup it expects and citing figures from each
  instrument's own row; code drops a pick it was not given or that cites nothing that holds.
  Code's own order is kept beside it for comparison. `tra universe` previews discovery. Positions
  on instruments no longer listed are still counted by the desk and looked after by the keeper.
- A model link whose quota is spent, or that asks for a longer wait than `max_retry_wait_s`,
  rests (15 minutes for a spent quota) and the chain goes straight to the next link; seen live, a
  spent Gemini quota put 15 to 45 seconds in front of every validator and the book moved past the
  drift limit before the order went out. Usage now names the provider that actually answered.
- The cycle panel says when the venue did not take the order (`venue  NOT SENT: ...`), and the
  refusal is on the cycle's record; before, the panel showed the intent and nothing else.
- The universe table marks an admitted instrument that did not run as `waiting`, says the pass
  took `[universe].max_per_cycle` by pressure, no longer cuts a reason at 60 characters, and no
  longer claims no tokens were spent when the screener model read every candidate.
- Conviction shows each voice with its sign and whether it pulled for the trade or against it.
- The targets panel reads the trade as a trader would: each exit as a price, the move in price and
  what it does to the position (a short's take profit is a fall in price and a gain), reward to
  risk as a ratio, the expected edge with where it came from, the round trip, what is left after
  cost against the multiple the desk asks for, model spend with any unpriced calls named, and
  trades today before this one. Unasked analysts carry their reason in the agents table.
- Venue mode checks the market type on every order and refuses anything that is not a linear
  perpetual (spot, dated futures, inverse contracts, options), and the README says what is traded.
- `tra check` flags validators that are asked every cycle while neither `[confluence]` nor
  `[conviction].validator_weight` counts their votes; docs say how each stance's vote is used and
  that no stance, neutral included, decides.
- The console takes the whole terminal when it opens, as `cls` or `clear` would (screen and
  scrollback), and turns on escape sequences on the classic Windows console first; the shell's
  earlier output no longer sits above the cockpit.
- The macro template lists daily series that reach crypto (dollar, real and nominal yields, VIX).
- Two free on-chain feeds for `fetch`: `coinmetrics` (Coin Metrics community API; `minus` turns
  exchange inflow less outflow into one net-flow series) and `blockchain.info` (Blockchain.com
  charts). The on-chain template shows both.
- A reading analyst that is not asked now has its own row in the live table and the result,
  marked `not asked` with the reason (nothing declared under its `sources/` files, not in
  `[agents].analysts`, every series stale or unreadable). Before this, an on-chain analyst with no
  data simply did not appear, which read as broken.
- Every cycle keeps its raw inputs in `inputs.json.gz` (candles on every timeframe, book levels,
  funding, open interest, the settings used), and `cycle.json` carries a sha256 of every file in
  the cycle. `tra runs verify` checks the hashes and recomputes the technical figures from the
  inputs. Before this, a snapshot recorded where values came from but not the values, so a brief
  could not be checked.
- A per-day cycle index (`runs/cycles/index/<day>.jsonl`). The trigger, the targets, the desk
  limits, the position keeper and the open interest change read it instead of every cycle
  directory: measured on a month of three instruments every five minutes (26,000 cycles), a read
  went from 2.3 to 3.6 s to under 0.6 s. Built from the directories on a desk's first cycle; a cycle
  that never finished is still read from its directory.
- `tra runs prune --keep 90d [--archive] [--yes]`: removes old cycle directories and screens, keeps
  the index and the logs.
- Every series a reading analyst reads now has a `max_age` (default 4 days). A value older than
  that is shown as `stale` and never counted in the declared state, confluence or conviction; a role
  whose every series is stale is not asked. Seen on the demo desk: macro and sentiment files that
  had not been updated for nine days were still being weighed as current.
- `fetch` on a series refreshes its file from FRED (`from = "fred"`, free key), the Crypto Fear &
  Greed Index (`from = "alternative.me"`) or any https JSON endpoint (`from = "http"`, for paid
  on-chain and derivatives vendors), merging into the history already there. `tra run` refreshes due
  series before each pass; `tra sources pull` does it now; `tra sources show` lists every series with
  where it came from, its last value and its age. `tra check` names the stale ones.
- The cycle shows a `series read` table (analyst, series, value, as of, age, state, origin), and
  `cycle.json` keeps it under `series`.
- The targets panel shows the entry, the take profit and the stop loss as prices with their distance
  from the entry in bps, and spells out net as the claimed edge less the round-trip cost. The plain
  output prints an `exits:` line.
- The technical analyst now sees the market around its decision: the regime on its own timeframe
  (`regime`, `trend_strength`, `window_move_bps`), the same on higher timeframes declared in
  `[run].context_intervals`, the order book imbalance near the touch, the open interest change
  since the previous snapshot, and what the operator's thresholds say on the macro and other series
  (`readings`), all computed by code before any model is asked.
- `[regime]`: a setup that does not fit the regime measured on the decision timeframe (a trend
  setup in a range, a range fade or a pullback against a trend) is refused by name;
  `setups_must_fit = false` records it and lets the trade go on. `tra check` states the lines.
- `[agents].on_missing_validator` (default `refuse`): a declared validator that no model answered
  refuses the trade instead of letting it go on one opinion; `continue` keeps the old behaviour.
- `[agents].max_retry_wait_s` (default 15): a provider that says "try again in N seconds" within
  that bound is waited for once before the next link in the chain, read from the vendor's
  retry-after header or its message.
- `tra check` says how many calls a cycle makes within a minute and on which providers, suggests
  giving validators their own provider when every role is on one, and states the validator policy.
- One desk per project in venue mode. `tra run` takes `runs/cycles/desk.lock` and refuses to start
  while another live process holds it, so two processes never send orders for one account from the
  same project; a lock left by a process that died is taken over.
- **Capital, orders and positions in venue mode.** Before every order: the account's equity and
  free margin are read, sizing uses the smaller of the account equity and `[run].equity`, an order
  may use at most `max_margin_share` of the free margin, and `[controls].daily_loss_limit_bps`
  stops new entries for the day. At send time: `[execution].leverage` (now required in venue mode)
  and `margin_mode` are applied at the venue and read back, the estimated liquidation must sit
  beyond the stop plus `liquidation_buffer_bps`, a taker entry is a limit IOC within
  `max_slippage_bps` with its fill read back, and `[controls].max_orders_per_minute` is enforced
  across the desk. At the start of every pass: positions are matched to the orders that filled
  them, closed with a reduce-only order past `[execution].hold_limit`, recorded when the venue's
  exits closed them, and flattened when the kill switch is armed. New records: `positions.jsonl`,
  `round_trips.jsonl`, `equity.jsonl`. All through ccxt's unified calls, nothing venue-specific.
- A partial fill now carries only its share of the risk in the desk's book.

- The venue hand reads the book again just before sending. A maker order rests at the touch as it
  is then, not at the snapshot's (seen live: a post-only order rested at a half-minute-old price was
  on the wrong side of the market and rejected at once), and the record carries the priced mid, the
  drift and where it rested. `[execution].max_drift_bps` refuses an order whose mid has moved that
  far against the trade since it was priced.
- The live view shows every configured step from the start, a spinner and a running clock on each
  step in progress, and a line saying who is working right now; the pass over a universe gets the
  same view, the wait between scheduled cycles is a countdown, and an auditor woken by an alarm has
  a spinner.
- `tra venues NAME --setup`: the blocks to paste into `envelope.toml`, `data.toml`, `strategy.toml`
  and `.env`, shaped by what the venue can do. The fees are left blank so the file refuses to load
  until the operator writes their own.
- The venue hand refuses to send when this machine's clock is more than a second from the venue's,
  the bound `tra live` already held.
- `docs/venues.md` lists which venues work today and what has been run end to end, and adding a
  venue is four steps.
- `[run].size_quote`: the base size in quote currency, converted at each cycle's own mid and rounded
  down to the instrument's step, so one figure means the same money on every instrument a desk
  watches. It is converted before any model is asked, recorded in `cycle.json`, and exactly one of
  `size` and `size_quote` may be declared. Replay, paper and live sessions trade a fixed size and
  refuse it by name.
- `[run].size_step` may be a table with one increment per instrument; it has to name every
  instrument the desk can trade.
- The venue hand refuses a size off the venue's step or under its minimum instead of letting ccxt
  round it on the way out, which would send a different size from the one priced and recorded.
- `tra run` treats SIGTERM, SIGHUP and SIGBREAK like Ctrl-C. A maker order resting when the stop
  arrives is cancelled first and the record says so; stopping inside a cycle exits as a refusal.
- `runs/cycles/scheduler.jsonl`: one line per start and stop of `tra run`, with what stopped it.
- `tra check` reads the size against `[execution].max_notional` in quote terms, including the top
  rung of the conviction ladder, and says what one unit of size means across a universe.
- `tra watch`: a read-only view of a desk from the records it writes. It opens no socket, builds no
  client, holds no key and takes no lock, so opening it and closing it does nothing at all to a
  running desk; a record still being written is skipped rather than shown wrong. It shows the last
  decisions and what became of them, the last pass over the universe, the book as the newest cycle
  recorded it, anything that broke, and what the models cost across the cycles in view.
- `[desk]`: what the whole book may carry at once, as opposed to what any one trade may risk.
  `max_open_risk_bps` sums what every open position loses at its own stop and refuses the trade that
  would take the total over; `max_open_positions` is a plain count; `correlation_above` refuses a
  candidate that has been moving with something already open, because one trade in two names is not
  two trades. What counts as open comes from the venue in venue mode and from the intents on disk
  everywhere else, and the cycle records which. A position whose risk is not on the record refuses
  the next trade rather than being left out of the sum.
- `tra paper --minutes N` ends a shadow session after that long. Without it a session runs until
  somebody stops it, which is right for a desk and wrong for the first one anybody runs.
- `tra -c` opens no history file and builds no line editor: a one-shot in a script has nothing to
  arrow back through. The history in `.tra-history` is a convenience for the prompt, not an audit
  trail - what the desk did is in `runs/`, where nothing is rewritten.
- The console prompt is `>` with a line above and below it, so one command's output never runs into
  the next, and an empty prompt says what it is waiting for.
- `tra keys`: whether every credential this project would actually reach is there and works, without
  any of them being shown. Every provider in play - the declared one, whatever a role was given
  instead, and every link of every fallback chain - plus the venue an order would go through.
  `--probe` proves each one with a free authenticated call (a model list, an account read); `--all`
  shows every provider this machine holds a variable for, including the ones set to nothing. Keys
  are shown as eight hex characters of their own SHA-256: enough to tell two apart, impossible to
  turn back into either, and anything a vendor says back is scrubbed of every secret this process
  knows before it is printed.
- `[universe]`: the instruments a desk watches, and a pass that chooses between them before any of
  them costs a roster of models. One data pull per instrument and no tokens, the same `[trigger]`
  applied to each, and what survives ordered by how far the mid has moved against that instrument's
  own spread. The order is code, because it decides where capital gets to be considered at all; an
  instrument nobody has looked at before keeps the place the operator gave it, which is how a new
  instrument enters a running desk. `max_per_cycle` says how many a pass runs in full and
  `max_intents_per_day` counts the whole desk's day rather than one instrument's.
- `[universe].screener` puts the market screener over what code admitted, as a second opinion from
  the same figures. It may only take an instrument out - not add one, not reorder, not mention a
  direction or a size - and a screener that cannot answer leaves the pass as code left it. It reads
  its own doctrine (`market_screener_live.md`), because the same role answers a different question
  over recorded trades in a session than it does over one live snapshot in a cycle.
- A cycle directory now carries the instrument in its name (`2026-09-29T07-44-43Z-ETHUSDT`), so a
  desk running several instruments in the same second keeps them apart on disk. The moment is still
  the first twenty characters of a cycle id, wherever one is read back. The pass itself is written
  to `runs/screens/<time>.json` whether or not it chose anything.
- `tra` on its own now opens a console instead of printing one screen and handing the shell back.
  The cockpit first, then a prompt that takes every command this CLI has and draws it where the
  operator is looking, with history in `.tra-history`, tab completion, `help` and `exit`. It is a
  place to stand rather than a second way of doing anything: each line goes through the same parser
  and the same handler a shell would reach, so a command typed at the prompt and the same command
  typed in a terminal are one code path leaving one record. `tra -c "run --now"` runs one command
  and leaves, for scripts and for a server, and `--no-history` keeps nothing.
- `[audit]`: the auditors wake without anyone typing the command, and they wake differently because
  they answer different questions. `on_alarm` asks the technical auditor when a cycle records
  something broken - the cycle did not finish, a model call never came back, a data chain ran out,
  a venue refused - at most once per kind per `alarm_cooldown_s`. `quant_every_cycles` runs the
  quant auditor on a count of cycles. A gate refusing is not an alarm. Neither auditor can act on
  what it finds, an auditor that fails is a line in the run's output rather than the end of the run,
  and their calls are counted in their own ledger so what the desk spent deciding stays separable
  from what it spent looking at itself.
- `[trigger]`: what has to have happened before a cycle pays the roster for an opinion. The mid's
  move since the last cycle, the width of the book, a cooldown after an intent, and a widening rule
  that asks for more movement after a run of refusals with nothing changing, capped at eight times
  the baseline so a desk can never be talked into going blind. Every figure is free - the snapshot's
  own touch, the cycle summaries on disk, the clock - so a cycle that holds costs one data pull and
  no tokens, and the hold is recorded with its arithmetic. A cycle the trigger held does not count
  towards the streak. `tra run --now` overrides it, and the cycle records the override.
- `[controls].max_risk_per_trade_bps` is now applied inside the cycle, between the conviction ladder
  and the cost gate. The loss at the stop against equity, in basis points; over the allowance the
  size is cut to fit, never the stop moved; under one step of the venue the cycle refuses. The
  figure travels in the intent as `risk_bps`. It had only ever been enforced by the session runtime,
  so a cycle in venue mode could send a size nobody had measured against the account.
- `[targets].min_edge_multiple`: the claimed edge as a multiple of the round trip. A cost of 18 bps
  against an edge of 20 is not a two basis point trade - the fees are certain, the edge is a
  probability, and slippage moves only one of them. `tra init --profile autonomous` writes 2.0.
- `[targets].max_research_bps`: every model call since the last intent, against the notional of the
  trade those calls produced, so a day of refusals is paid for by whatever the day trades rather
  than disappearing from the track record. `tra check` refuses the target when no model price is
  declared, or when the declared price is zero.
- The technical analyst answers a `setup` named from a closed list (`trend_continuation`,
  `pullback_in_trend`, `range_reversion`, `range_breakout`, `momentum_exhaustion`,
  `volatility_expansion`, `no_setup`). A direction with `no_setup` is refused, and the name travels
  into the intent, so which setups a desk has been paid for is a question the records answer.
- Every role that speaks about numbers answers `figures_used` as `{figure, value}` pairs, checked
  against its own brief by the same code the validators and auditors are held to. A reading
  analyst's uncited label is recorded and supports nothing; the analyst that decides is refused
  outright when not one citation survives, because a direction that cannot point at a figure in its
  brief came from somewhere the cycle cannot see.
- `effort` per role: one vocabulary (`minimal` to `max`) declared in `[agents]` and overridden in
  `[agents.roles.<role>]`, mapped onto Anthropic's `output_config.effort`, Gemini's
  `thinking_config.thinking_level` and OpenAI's `reasoning_effort`, each verified against the
  installed SDK. Undeclared, the field is left out of the request entirely.
- A live cycle draws its own steps as they happen - the whole configured pipeline, pending first,
  then running, done, refused, failed or held. Progress travels on its own wire and can change
  nothing: a watcher that throws is swallowed, because a broken progress bar is not a reason to lose
  a decision.
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
  entry crosses the book (a limit IOC within `max_slippage_bps`), and a channel that says neither is refused rather than guessed
  at - sending a taker order under a maker-priced channel pays a fee the gate never priced.
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

### Fixed

- Candles on a timeframe longer than a minute were refused as stale once a cycle started more than
  90 seconds after the bar closed, which stopped four in ten cycles of a 15-minute desk before the
  analyst was asked. The candle `ttl` is now written for one-minute bars and a longer bar adds its
  own wait: a 15-minute bar with `ttl = "90s"` is fresh for 15m30s after it closed.
- The technical analyst's brief lost `range_high`, `range_low`, `atr` and `atr_bps` to the scope
  filter, so it could never name a stop or a target from them. They are now part of what the role
  reads, and `stop_price` and `target_price` part of what it answers.
- The screen line counted instruments after the screener as "admitted"; it now counts what the
  system admitted, so it agrees with the reason given when the screener takes none.
- The sentiment analyst's prompt names the exact path of a plain figure, so a citation such as
  `series.fear_greed.change_over_window` is no longer thrown away for a `.value` it never had.
- Conviction counted every validator verdict, so a pro that supported and a contra that opposed,
  as they do by assignment, added weight to the denominator and diluted every score (measured: 13
  of 13 cycles in a session fell below the floor; with the fix, the five that neutral supported
  clear it). A vote now counts for what it says beyond its stance.
- A retry-after written in minutes or hours ("try again in 8m55s", a tokens-per-day limit) was not
  read, so the spent link was asked again every cycle instead of resting.
- The cycle panel says which steps a cycle never reached and where it ended, so a missing risk or
  targets panel no longer looks like a broken one.
- `[controls].max_position` counts units, so on a desk that discovers coins priced in cents it
  capped a 400 USDT order at a few cents' worth. `[controls].max_position_quote` caps every
  instrument in quote currency at its own mid, and discovery requires it; the unit cap still holds
  `[run].instrument`.
- `tra check` now says what the desk does today: the screener's call in the per-minute count, reuse
  of unchanged readings, effort per role as each role is asked, prices declared free or not at
  all, discovered instruments in the universe and size lines, and how validator votes count.
- A model answer that does not fit its schema is asked for once more before it counts as missing;
  seen live, one validator's malformed JSON refused a whole trade under `on_missing_validator =
  "refuse"`. Groq now gets an enforced JSON schema (checked on `openai/gpt-oss-120b` for every role),
  and any OpenAI-compatible server that cannot enforce one falls back to JSON mode by itself. The
  agents table marks an answer that came back unreadable instead of showing it as ok.
- Validators and the screener are told that `ret_*_bps` and `window_move_bps` are moves already
  made, not forecasts, and that a `trend_strength` below 2 is not a trend; both were misread live.
- The technical brief's window move was named `net_bps`, and validators read it as the trade's net
  return (seen live: "a modest net return (22.07 bps) below the claimed edge"). It is now
  `window_move_bps`.
- Funding was priced over the analyst's horizon even when the desk holds to `[exit].timeout_bars`;
  it is now priced over the hold the desk actually keeps.
- A daily series held flat across a window of minute bars was reported as "correlation 0.0 on
  n=60" (float noise from a constant); it is now reported as not measurable, with the reason.
- One missing or broken file in a `sources/` declaration (such as a fetched series not pulled yet)
  made the whole declaration unreadable and silenced the analyst; each series is now read on its
  own, and the missing one is named.
- Appends to order, position, equity, scheduler, usage and ledger records now guard against a torn
  last line (a crash mid-write no longer glues the next record onto it) and wait for the disk where
  money is involved; whole JSON files are written atomically; every lines reader skips a torn line
  instead of failing (the auditor and `tra agent trace` used to).
- A pass reported instruments with an open position, or held by a full desk, as "not moved enough
  for [trigger]" whenever the trigger had also held them. The book is now checked first, and every
  instrument is named once, under the first cause that holds it.
- A pass that ran no cycle said "nothing was worth a cycle", which read as a quiet market even when
  the desk's own book was the reason. It now names each cause: an instrument open at the venue, the
  others moving with it above `[desk].correlation_above`, or too little movement for `[trigger]`,
  and says when a correlated universe leaves the desk one position at a time.
- A screen held by correlation printed its reason twice.
- **A position carried past midnight UTC froze the desk.** In venue mode, what an open position
  risks was looked up among the intents written today only, so after the day rolled over the desk
  could no longer state its own book and refused every new trade (seen live on 2026-10-05). It is
  now matched to the newest order this desk sent that the venue filled, on the same side, whatever
  the day; a later intent that was refused or cancelled no longer stands in for it.
- **A maker order the venue had already ended was reported as possibly still resting** (seen live
  on the Bybit demo, 2026-10-04). One venue's `fetch_order` through ccxt refuses unless told the
  order is among its last 500, and the refusal was read as "no answer" for the whole wait. The hand
  now asks per state (open, then closed) where the venue supports it, stops waiting as soon as the
  venue reports the order cancelled, rejected or expired, and records how much of it filled.
- The plain report of a sent order now carries an `outcome:` line (filled, ended by the venue,
  cancelled unfilled), and `tra watch` shows the same; "placed" alone read as a position.
- `--cycles N` counts scheduled passes. It counted cycles, so on a quiet market where the trigger
  held every pass the run never ended.
- `tra watch` rows carry the date as well as the time; a view spanning two days read out of order.
- The conviction refusal no longer reads "conviction: conviction 0.000".
- A reading analyst could read a declared `bullish` on a yield series as yields rising and call it
  risk-off (seen live). Each declared state in a brief now carries `state_for` (the traded
  instrument) and the operator's `threshold` in words, and the prompts say how to read them.
- The Gemini SDK printed an automatic-function-calling warning into the terminal on every call; it
  is switched off, since no role is ever given tools.
- Plain output carries models' typographic quotes, dashes and ellipses as ASCII, so a log or an
  old console no longer shows escapes; the records keep the original text.
- Plain and JSON reports are flushed as they are written, so a desk logging to a file can be read
  while it runs.
- **A cycle in venue mode ignored `[live].environment` and sent orders to the wrong place.** It
  switched the client into the venue's testnet for both `demo` and `testnet`, so a demo key was
  rejected by testnet - and, worse for anyone whose key is accepted there, an operator who declared
  `demo` had their orders routed somewhere they had not declared. Each environment is now reached
  through its own switch, and a venue with no switch for the environment declared is refused rather
  than sent somewhere close. Found by sending a real order on a demo venue and reading what came
  back; nothing in the test suite could have caught it, because the suite never reached a venue.
- **A desk watching several instruments built one hand and sent every instrument's orders through
  it.** The symbol came from `[run].instrument`, so an intent for one instrument would have gone out
  under another's symbol. A hand is now built per instrument, and refuses an intent about any other
  instrument even if one reaches it.
- `tra keys --probe` checks the venue through both clients that can reach it - the live session's
  own REST client and the cycle's ccxt client - because a readiness check that proves one path works
  proves nothing about the other, and it is the second one a cycle uses.
- `[live].key_ip_unbound_ok` could never be declared: the guard that keeps credentials out of config
  files matched the word "key" anywhere in a setting's name, and refused the whole file. The guard
  now runs over settings the table does not declare, so a flag about a key is not mistaken for a
  key. `[live]` gained the key allowlist every other table already had, so a typo there is named
  rather than ignored.
- `tra check` reported two different ladder tiers in the same run: one line read the ladder, the
  other invented the string "REPLAY", which is not a tier this ladder has. Both now read the ladder.
- The move since the last cycle is measured whenever there is something to measure against, not
  only when `[trigger].min_move_bps` is declared. A pass over several instruments is ordered by that
  figure, and it was disappearing exactly when the trigger was waived with `--run --now`.
- `tra run --now` overrides the trigger for the pass as well as for the cycle. An operator who asked
  anyway was being told nothing was worth looking at.
- A resting order whose cancel the venue refused was recorded as "cancelled" anyway, beside a flag
  that said otherwise. The cancel now reports what the venue said, and the record asks the venue
  what became of the order: either it had already ended it, which is ordinary for a post-only
  limit, or it is still resting there - which is said loudly, because an order nobody knows about
  is the one thing a record cannot make up for afterwards.
- A placement record carried the string "None" where a venue reported no status. It now carries
  nothing, which is what it means.
- A validator was held to a narrower set of figures than the brief it was handed: it could see the
  proposal's claimed edge and the round trip's cost, and citing either was discarded as an invented
  figure. What it may cite is now what it was shown.
- A citation written as one word where the brief writes a path - `us10y_value` for
  `series.us10y.value` - now resolves, by comparing the two with every separator removed and only
  after the exact and segment matches have failed. A name the brief does not carry is still refused,
  however it is spelled, and the value still has to match whatever it resolved to.
- Citations were being thrown away that the models had read correctly. `figures_of` skipped lists,
  and a reading brief carries a row per series, so `us10y value = 4.02` was a real number from the
  brief that no reader could point at. A list of rows that each name what they are about is now
  flattened into citable figures (a list of sixty bars names nothing and stays out), and a citation
  resolves when its words are the tail of exactly one figure's name, the shallowest winning a tie,
  with a block measured by its own sample size resolving to the number in it. The value still has to
  match whichever figure the name resolved to, so nothing about this loosens the rule.
- The intent carried no stop when `[exit].take_profit_sigma_frac` was absent, because both halves
  were computed together. Each half now stands on its own: a desk that declared a stop and no take
  profit gets its stop.
- A cycle now hands each source what the operator declared for that kind in `data.toml`. It was
  passing only the candle window it needed, so `levels`, `params` and anything else in a
  `[data.<kind>]` table were silently dropped on the `tra run` path while `tra data pull` honoured
  them.
- `tra venues` reported a test environment for every venue, because ccxt's base class defines the
  switch everywhere and only refuses it underneath. It now reports `demo` and `testnet` separately,
  from what each venue implements and the URLs it carries.
- A venue that signs with a passphrase (OKX, Bitget, KuCoin) or a wallet (Hyperliquid) could not
  authenticate: the venue hand only ever read a key and a secret. It now reads whatever ccxt says the
  venue requires, and `tra check` and `tra keys` name each variable.
- Demo or testnet requested on a venue without one now fails with a sentence naming `tra venues`,
  instead of ccxt's NotSupported.
- The console dropped any text in square brackets from tables and panels, so a refusal naming
  `[run].slippage_bps` read `.slippage_bps`. Rich markup is off for every rendered line.
- Unrounded decimals in the live view and the targets panel (`net 14.83045252144942739852674872`).
- A venue refusal was shown as a finished `venue` step; it is now shown as refused, with the reason.
- The scheduler printed how long it was going to wait after it had waited.
- `tra data pull` wrote its snapshot among the cycles, where the trigger, the desk and the auditors
  read; it now writes to `runs/pulls/`.
- `tra venues` verdicts name which account read is missing, and a venue that cannot report the
  position is listed as data only, since the hand refuses to send without it.

### Security

- The lock moves to ccxt 4.5.85, urllib3 2.8.0 and pyjwt 2.15.1, and overrides ccxt's exact pin
  on multidict to 6.9.1, clearing the advisories `uv audit` reported against 0.6.0's first lock.
- The read tools only open paths inside a declared root, and never a hidden file. This covers the
  auditor's tool loop in a session (project, session and ladder directories) and the MCP server
  (the working directory, or `tra mcp serve --root DIR`, repeatable). Before this a model could pass
  any path on the machine to a reader.
- A vendor's or a venue's error text is scrubbed of any credential the process holds before it is
  recorded or put into a brief.
- `max_calls_per_hour` and `min_interval_s` count across every role in a `tra run`, as the setting
  reads; each role used to keep its own window, which allowed the declared figure once per role.
- `usage.jsonl` appends are serialised. Parallel analysts on Windows could interleave lines.
- The reading analysts, validators and auditors are told that text in their brief is material to
  read, never an instruction.

### Removed

- `tra run --resume`. The cycle id comes from the clock, so it never found anything to reuse, and
  reusing answers against a newer snapshot would be wrong anyway. `tra agent session --resume`
  stays.

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
