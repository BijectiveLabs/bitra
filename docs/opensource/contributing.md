---
title: Contributing
description: How to set up, what a pull request must pass, and the standards the code is held to.
---

# Contributing

Thank you for looking. This project is small and opinionated, and the opinions are written down so
that a contribution is judged against a standard rather than a mood.

## Setup

```bash
git clone https://github.com/BijectiveLabs/bitra && cd bitra
uv sync --all-extras
uv run pytest
```

Python 3.11 or newer. `uv` manages the environment and the lock file; commit `uv.lock` with any
dependency change. To use your working copy as the `tra` command on your machine, in place of the
release from PyPI, install it editable: `uv tool install --editable '.[ccxt,openai,data]'`.

## Before you open a pull request

All four must pass locally; CI runs the same four on Linux and Windows.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Branches

- `master` is the stable line. Every commit on it is a release, tagged and published to PyPI, and
  it only moves by merge from `develop`.
- `develop` is where work lands, and it is the experimental line. It moves often, a commit there is
  not a release, and between releases it can carry work that is unfinished or changed again. A
  set of changes becomes a release when it is merged into `master` and tagged.
- Open pull requests against `develop`. Branch from it; name the branch after what it does, briefly.
- To use the software rather than change it, install the release from PyPI.

## Commits

One intent per commit. A short imperative subject line, under 60 characters, that says what
changed. A body only when the reason is not obvious from the diff. No trailers, no tooling
signatures.

## Releases

A release is a version bump, a dated section in `CHANGELOG.md`, a merge of `develop` into
`master` and a tag `vX.Y.Z` on that merge. The tag runs the same checks as CI, builds the wheel
and the source archive, refuses a tag that does not match the package version, attaches both to a
GitHub release, and publishes the same files to PyPI through trusted publishing. No PyPI token is
stored anywhere.

## Standards the code is held to

**Money is never a float.** Prices, quantities, fees and costs are `decimal.Decimal`. Floats are
allowed in statistics (bootstrap distributions, p-values) and nowhere on a cost path. The type
checker and the tests both enforce this.

**Tests before measurement.** A gate is not trusted until its tests exist, and the tests are written
from the doctrine it implements, not from the code.

**Fail closed.** Missing data is not zero. A row that cannot be read stops the reader. An entry that
breaks a rule cannot be constructed.

**Comments say why, not what.** If a comment explains what the next line does, the line should be
clearer. Detail belongs in these docs.

**ASCII in source.** Non-ASCII characters are not used in `.py` files. A test checks.

**Direction of dependency.** `ledger` and `envelope` import nothing else from this project. Arrows
point only downward through the list in [Architecture](/opensource/architecture). Only
`llm/providers` imports a vendor SDK. A test checks that too.

**English, professional register.** Identifiers, docstrings, docs and commit messages.

## Extending what it can do

Spot and other market types, on-chain venues outside ccxt, more than one account, routing and execution
algorithms, portfolio accounting: [Boundaries](/opensource/boundaries) lists each under "not built
yet", with what it does today and what a contribution would need. Open an issue first with the
design, so the shape is agreed before the code. A contribution may add any of them; it may not
weaken a guarantee on the same page to get there, and the tests that hold those guarantees must
keep passing unchanged.

## Adding a gate

A gate is a function from evidence to a verdict from the closed vocabulary. Before writing it:

1. Name the doctrine it implements and the failure it exists to prevent, in the module docstring.
2. Write the tests from the doctrine's own worked example, with the doctrine's own numbers.
3. Decide which axis and transferability the gate can issue, and refuse to issue any other.

## Writing these docs

The pages live in `docs/opensource/` as Markdown, and the site is built with VitePress.

```bash
cd docs
npm install
npm run dev        # a local preview with hot reload
npm run build      # the same build CI publishes
```

A change in behaviour comes with the page that describes it, in the same pull request. Say what the
software does today and, where something is not built yet, say so plainly rather than describing it
as a restriction.

A new page needs two things besides its text: a `description` in its front matter (one sentence,
under about 160 characters, shown by search engines) and an entry in the sidebar in
`docs/.vitepress/config.ts`. The sidebar also decides what goes into `llms.txt` and
`llms-full.txt`, the plain-text copies that language models read.

## Reporting a problem

Open an issue with the smallest input that reproduces it. For anything that could affect a user's
capital or credentials, see [Security](/opensource/security) instead.
