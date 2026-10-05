# Contributing

Thank you for looking. This project is small and opinionated, and the opinions are written
down so that a contribution is judged against a standard rather than a mood.

## Setup

```bash
uv sync
uv run pytest
```

Python 3.11 or newer. `uv` manages the environment and the lock file; commit `uv.lock` with any
dependency change.

## Before you open a pull request

All four must pass locally; CI runs the same four on Linux and Windows.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Branches

- `develop` is where work lands. Open pull requests against it.
- `master` is what has been released. It only moves by merge from `develop`.
- Branch from `develop`; name the branch after what it does, briefly.

## Commits

One intent per commit. A short imperative subject line, under 60 characters, that says what
changed. A body only when the reason is not obvious from the diff. No trailers, no tooling
signatures.

## Standards the code is held to

**Money is never a float.** Prices, quantities, fees and costs are `decimal.Decimal`. Floats
are allowed in statistics (bootstrap distributions, p-values) and nowhere on a cost path. The
type checker and the tests both enforce this.

**Tests before measurement.** A gate is not trusted until its tests exist, and the tests are
written from the doctrine it implements, not from the code.

**Fail closed.** Missing data is not zero. A row that cannot be read stops the reader. An
entry that breaks a rule cannot be constructed.

**Comments say why, not what.** If a comment explains what the next line does, the line
should be clearer. Detail belongs in `docs/`.

**ASCII in source.** Non-ASCII characters are not used in `.py` files. A test checks.

**Direction of dependency.** `ledger` and `envelope` import nothing else from this project.
Arrows point only downward through the list in `docs/architecture.md`. Only `llm/providers`
imports a vendor SDK. A test checks that too.

**English, professional register.** Identifiers, docstrings, docs and commit messages.

## Adding a gate

A gate is a function from evidence to a verdict from the closed vocabulary. Before writing it:

1. Name the doctrine it implements and the failure it exists to prevent, in the module docstring.
2. Write the tests from the doctrine's own worked example, with the doctrine's own numbers.
3. Decide which axis and transferability the gate can issue, and refuse to issue any other.

## Reporting a problem

Open an issue with the smallest input that reproduces it. For anything that could affect a
user's capital or credentials, see `SECURITY.md` instead.
