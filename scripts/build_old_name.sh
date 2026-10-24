#!/usr/bin/env bash
# The package under the name it had before 0.10.1, built from the same source into dist/.
# A tool or environment installed as trading-research-agents upgrades in place to the same release,
# with its commands, instead of to a package that only depends on bitra.
set -euo pipefail
cp pyproject.toml pyproject.toml.bitra
trap 'mv pyproject.toml.bitra pyproject.toml' EXIT
sed -i 's/^name = "bitra"$/name = "trading-research-agents"/' pyproject.toml
grep -q '^name = "trading-research-agents"$' pyproject.toml
uv build --out-dir dist
