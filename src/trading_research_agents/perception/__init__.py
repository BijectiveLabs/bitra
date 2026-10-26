"""Perception: archived text read by a typed judge into events the features can weigh.

This is the one place a judge meets market data. It sends a headline and its publisher, never a
position, a balance or a key, and turns each verdict into a `features.text.TextEvent`. Features
stay pure: they read events and never import a model.
"""
