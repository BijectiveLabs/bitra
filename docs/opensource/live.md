---
description: "The live tier: real orders at the canary tier through ccxt, on the same client venue mode uses, with hard caps, reduce-only exits and the security checks that run as tests."
---

# The live tier

Real orders exist in this release at one tier, canary (T3), behind a security review that runs as tests rather than sitting in a document. Production (T4) is not
reachable; the ladder refuses it, and a canary session is what produces the evidence that will
one day argue for it.

```
tra live --config strategy.toml --envelope envelope.toml --out runs/live-1 --env-file .env
```

`tra live` is `tra trade live`. It runs the same loop as replay and shadow with two
differences: the venue sends orders, and every stop, take-profit and timeout is routed through
it as a reduce-only order instead of being settled against the bar. No language model is
anywhere on this path.

The live tier trades linear perpetuals. Spot pairs, stocks and prediction outcomes are traded by an
agent desk in venue mode (`tra run`), which has its own checks ([Venues](/opensource/venues)).

The venue is any exchange ccxt knows, named in `[live].venue`, and it is reached the same way
venue mode reaches it: the same client built from the environment, the same switch to the demo
or testnet, the same market type and contract size, the same position read. There is no second
adapter per exchange. A plugin registered under `trading_research_agents.live_venues` still
takes precedence for its name, for a venue ccxt does not cover.

## What has to be true before the first print

| Check | Refused when | Where |
|---|---|---|
| Tables | `[live]` or `[canary]` is missing from `strategy.toml` | config |
| Keys | the variables the venue signs with (`BYBIT_API_KEY` and `BYBIT_API_SECRET`; OKX adds `OKX_API_PASSPHRASE`) are not in the environment or `--env-file`; or a key-like field appears in `[live]` | config |
| Ladder | `ladder.json` is not at CANARY; PRODUCTION is refused whatever the file says | `tra live` |
| Clock | the local clock is more than 1000 ms from the venue's, judged on the quickest of three readings | preflight |
| Account | the account is in long/short position mode, or (OKX) in a mode that cannot trade perpetuals | preflight |
| Key permissions | where the venue reports them (Bybit today): the key is read-only; carries any wallet permission; lacks ContractTrade Order and Position; is not bound to an IP (unless `key_ip_unbound_ok = true`); expires within 7 days | preflight |
| Market | the instrument is not a linear perpetual on the venue, or the venue does not say how large a contract is | preflight |
| Position | the venue already holds a position the run was not told to adopt (`adopt_open_position = true`) | preflight |

Every refusal names what to fix. The preflight report is written to `preflight.json` in the run
directory before any order is sent.

## During the session

- **One order at a time.** A second intent while one is pending is refused, as in simulation.
- **Polling.** An order's state is asked for at most every `[live].poll_interval_ms` (250 by
  default), so a session does not spend the venue's rate limit on a fill it already knows.
- **Sized as priced.** The runtime sizes in the base and the venue counts contracts, so the size is
  divided by the market's contract size, and a size off the venue's step is refused rather than
  rounded into a different trade.
- **Idempotent submission.** Every order is a market order carrying a client id,
  `tra<run><seq>`, letters and digits only. A send that fails in transit is looked up by that id
  on the next poll before anything is sent again; only an order the venue does not know is sent
  again, under the same id, and the venue answering "duplicate" is read as the first attempt
  having landed. Three failed sends end the session.
- **Closes are reduce-only.** A reduce-only close the venue refuses means the model's position is
  not the venue's; the session stops.
- **Fills are confirmed, not assumed.** The order is read back from the venue, and its filled
  amount and average price are the fill, in the base. An order that has finished but does not yet
  say what it filled is asked again rather than read as nothing. A partial fill is a smaller
  position and a second reduce-only order for the remainder when it closes.
- **Reconciliation after every fill.** The venue's position must agree with the model to the last
  unit. A disagreement is a `VenueError`: the loop stops, the kill switch is armed, nothing more is
  sent. An order into an unknown position is the one thing worse than a stuck position, and the
  operator resolves it by hand.
- **Rate limits.** ccxt spaces requests by the venue's published limit, and a request refused for
  rate counts as a send that failed in transit.
- **Fees as charged.** The round trip in `trades.jsonl` uses the fee the venue reported on both
  fills, not the envelope's quote; spread and slippage are already inside the fill prices, and
  funding accrues from `[run].funding` when a settlement was spanned.
- **The kill switch.** Touching the `KILL` file flattens through the venue, waits for the fill,
  and stops.

## The canary caps

```toml
[canary]
max_notional = 200            # per order, in quote currency
max_round_trips = 5
session_loss_limit_bps = 100
max_session_minutes = 120
```

None has a default. Crossing any of them refuses further entries, flattens an open position
through the venue, and ends the run with `stopped_by` naming the cap. The caps are set by what
a defect would cost, not by what the strategy might earn: a canary is there to find out whether
the venue behaves the way the simulation assumed.

## Secrets

Keys come from the environment. `--env-file .env` reads `KEY=VALUE` lines explicitly (the
environment wins on conflict), and the `.env` beside `strategy.toml` is read the same way; keys are
never read from `strategy.toml` or any other config. They are read in one place, where the client
is built, and every error from the venue passes through a filter that replaces the key and the
secret before it is written anywhere: a log, `intents.jsonl`, `venue_events.txt` or the error
itself. A trade key should be able to trade and nothing else: no withdrawal or transfer
permission, bound to the machine's IP, and rotated before the session if it expires within a week.

## Environments

`[live].environment` is `demo` (default), `testnet` or `mainnet`. A first canary belongs on demo.

They are three different places with three different accounts, and a key for one is rejected by the
others, so the environment is not a label, it is where the order goes. `tra live` and venue mode
reach them the same way, through ccxt's own switch for each; where a venue has no switch for the
environment declared, the session refuses rather than sending somewhere close. Mainnet is the
client's own default and is never switched into.

`tra keys --probe` checks the keys against the place they are declared for.

## Not built yet

In `tra live`: limit orders and maker channels (venue mode in `tra run` has both), and execution
reports pushed by the venue rather than polled. Each is open to a contribution that keeps
the guarantees in [Boundaries](/opensource/boundaries). A production tier is not offered, and that one is a
guarantee rather than a gap: the ladder stops at canary.
