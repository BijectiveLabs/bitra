"""Forecast: how likely an idea reaches its take before its stop, in two independent ways.

`paths` draws the instrument's own candles forward from the last closed one and reads the paths at
the levels a trade names, with Kronos (arXiv 2508.02739) as the reference for the candle as the
unit, the window normalised on itself and the next candle drawn on a coarse and a fine token. The
rest is fitted on settled outcomes: a regularised logistic model of the outcome on features
(`logistic`), a calibrator that maps its score onto the rate the outcomes actually showed
(`calibration`), and the expected value of a side net of the round trip (`decision`). Their arena
rows are `evaluation.paths` and `evaluation.forecast`. Plain numerics, with no market knowledge.
"""
