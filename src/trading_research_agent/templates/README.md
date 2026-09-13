# A trading-research-agent project

    tra data fetch --venue bybit --instrument BTCUSDT --day 2025-01-01 --out data/
    tra data audit data/BTCUSDT2025-01-01.csv.gz --venue bybit --instrument BTCUSDT         --max-gap 5m --parquet data/btc.parquet
    tra backtest --config strategy.toml --envelope envelope.toml         --parquet data/btc.parquet --out runs/backtest-1
    tra paper --config strategy.toml --envelope envelope.toml --out runs/paper-1

`strategies/trailing_zscore.py` is an example core, not a recommendation. Replace it with your
own: a class with `name` and `on_bar(bar) -> Signal | None`, pointed at by `[strategy]` in
`strategy.toml`. The gates do the rest.

The live tier needs `[live]` and `[canary]` in `strategy.toml`, keys in `.env` (see
`.env.example`), and a ladder promoted to canary with evidence:

    tra ladder promote --evidence "layer 5 OPEN on paper-1" --signed-by you   # to T1
    tra ladder promote --evidence "..." --signed-by you                        # to T2
    tra ladder promote --evidence "..." --signed-by you                        # to T3
    tra live --config strategy.toml --envelope envelope.toml --out runs/live-1 --env-file .env
