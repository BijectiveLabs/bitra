# trading-research-agents is now bitra

TRA, the open-source autonomous trading agents with deterministic execution by Bijective Labs, is
published as [bitra](https://pypi.org/project/bitra/) from 0.10.1. This package installs bitra at
the same version and nothing else. The command is still `tra`.

New installs:

```
uv tool install 'bitra[ccxt,openai,anthropic,google,data]'
```

An existing tool install moves over with `uv tool uninstall trading-research-agents` followed by
the line above. Documentation: https://docs.bijectivelabs.dev/opensource/
