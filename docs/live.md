# The live tier

Real orders exist in this release at one tier, canary (T3), through a venue plugin, behind a
security review that runs as tests rather than sitting in a document. Production (T4) is not
reachable; the ladder refuses it, and a canary session is what produces the evidence that will
one day argue for it.

```
tra live --config strategy.toml --envelope envelope.toml --out runs/live-1 --env-file .env
```

`tra live` is `tra trade live`. It runs the same loop as replay and shadow with two
differences: the venue is a live plugin that sends orders, and every stop, take-profit and
timeout is routed through it as a reduce-only order instead of being settled against the bar.
No language model is anywhere on this path.

## What has to be true before the first print

| Check | Refused when | Where |
|---|---|---|
| Tables | `[live]` or `[canary]` is missing from `strategy.toml` | config |
| Keys | `BYBIT_API_KEY` / `BYBIT_API_SECRET` are not in the environment or `--env-file`; or a key-like field appears in `[live]` | config, plugin |
| Ladder | `ladder.json` is not at CANARY; PRODUCTION is refused whatever the file says | `tra live` |
| Clock | the local clock is more than 1000 ms from the venue's (`GET /v5/market/time`) | plugin preflight |
| Key permissions | the key is read-only; carries any wallet permission; lacks ContractTrade Order and Position; is not bound to an IP (unless `key_ip_unbound_ok = true`); expires within 7 days | plugin preflight |
| Position | the venue already holds a position the run was not told to adopt (`adopt_open_position = true`) | plugin preflight |

Every refusal names what to fix. The preflight report is written to `preflight.json` in the run
directory before any order is sent.

## During the session

- **One order at a time.** A second intent while one is pending is refused, as in simulation.
- **Idempotent submission.** Every order carries `orderLinkId = tra-<run>-<seq>`. A lost
  response is retried with the same id; the venue answering "duplicate" means the first attempt
  landed, and the adapter polls that order rather than sending another.
- **Closes are reduce-only.** The venue answering "you may only reduce" on an entry means the
  model's position is not the venue's; the session stops.
- **Fills are confirmed, not assumed.** The adapter polls the order by its link id and takes
  `cumExecQty` and `avgPrice` as the fill. A partial IOC fill is a smaller position and a second
  reduce-only order for the remainder when it closes.
- **Reconciliation after every fill.** `GET /v5/position/list` must agree with the model to
  the last unit. A disagreement is a `VenueError`: the loop stops, the kill switch is armed,
  nothing more is sent. An order into an unknown position is the one thing worse than a stuck
  position, and the operator resolves it by hand.
- **Rate limits are waited out.** `retCode 10006` and HTTP 429 wait until the venue's reset
  timestamp, three times, then refuse.
- **Fees as charged.** The round trip in `trades.jsonl` uses the venue's `cumExecFee` of both
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
environment wins on conflict); nothing is ever read from a file the operator did not name. The
adapter's `repr` shows the last four characters of the key and never the secret; no header, no
signature and no secret appear in any log, in `intents.jsonl`, or in an error message. A trade
key on the venue should carry ContractTrade Order and Position and nothing else, bound to the
machine's IP, and rotated before the session if it expires within a week.

## Environments

`[live].environment` is `demo` (default), `testnet` or `mainnet`. The base URLs are a fixed
table in the plugin; a URL is never read from configuration. A first canary belongs on demo.

## What is not here

Limit orders and maker channels, the private WebSocket for execution reports, a second live
venue, and production. Each is a deliberate omission recorded in the changelog; none is a
missing line of code.
