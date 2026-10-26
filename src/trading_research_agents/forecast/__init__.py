"""Forecast: the probability an idea reaches its take before its stop, fitted on settled outcomes.

Three pieces, each plain numerics with no market knowledge: a regularised logistic model of the
outcome on features (`logistic`), a calibrator that maps its score onto the rate the outcomes
actually showed (`calibration`), and the expected value of a side net of the round trip
(`decision`). The arena row that fits them walk-forward on the archive is `evaluation.forecast`.
"""
