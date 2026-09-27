# A trading-research-agents project

    tra                                        # what is ready, and the next command
    tra check                                  # the same, as a list
    tra data get BTCUSDT 2025-01-01            # fetch, audit, Parquet and funding into data/
    tra chain observe                          # layer 1: does the event in observation.toml recur?
    tra backtest                               # the core in strategy.toml over the newest dataset
    tra paper                                  # live public trades, simulated fills, zero orders

Everything a run needs is in `strategy.toml`, commented line by line. `strategies/
trailing_zscore.py` is an example core, not a recommendation; replace it with your own (a
class with `name` and `on_bar(bar) -> Signal | None`), or make a model the core with
`agent = "technical_analyst"` and an `[agents]` table (`tra init --agent PROVIDER --model M`
writes one). Keys go in `.env` (copy `.env.example`) and are read only with `--env-file .env`.

The live tier needs `[live]` and `[canary]` in `strategy.toml`, keys in `.env`, and a ladder
promoted to canary with evidence and your signature:

    tra ladder promote --evidence "layer 5 OPEN on paper-1" --signed-by you   # to T1
    tra ladder promote --evidence "..." --signed-by you                        # to T2
    tra ladder promote --evidence "..." --signed-by you                        # to T3
    tra live --env-file .env

Where the responsibility sits: docs/liability.md in the project's repository.
