"""Claims have to point at a figure that exists, and code is what checks they do.

Two roles argue from figures: the validators, about one proposal, and the auditors, about what the
desk and the package have actually been doing. Both are held to the same rule - name the figure by
the brief's own name and repeat its value - so the rule lives here once.

The comparison is fair to a model that rounds. "-1.63" against a brief's "-1.631" is the same
citation said with fewer digits, and it is kept; "-2.0" is a different number, and it is not. A
claim of NaN is not a citation of anything.

It is also fair about names. A brief carries sections and a row per series, so one number's full
name is `series.us10y.correlation_measured.value`, and no model writes that when it means "the
correlation". A citation therefore resolves when its words are the tail end of exactly one figure's
name, and when several match, the shallowest one - `series.us10y.value` before
`series.us10y.zscore_vs_recent.value`. None of that loosens the rule: whichever figure it resolves
to, the value still has to match, so a citation that lands on the wrong figure is discarded anyway.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

SKIP = ("mechanism", "basis")
# A list of rows is only worth flattening when each row says which thing it is about. Series rows do
# (`series = "us10y"`), so their figures become citable; a list of sixty bars does not, and stays
# out of the map rather than burying every other figure under three hundred of them.
NAMES = ("series", "name", "instrument", "kind", "symbol", "stance")

# Every role that is asked to say something about figures answers with these, so the rule reads the
# same in four schemas and is checked by one function.
FIGURES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"figure": {"type": "string"}, "value": {"type": "string"}},
        "required": ["figure", "value"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class Figure:
    """A figure a role says it read, by the brief's own name, with the value it read."""

    figure: str
    value: str

    def record(self) -> dict[str, str]:
        return {"figure": self.figure, "value": self.value}


def figures_used(value: Any, label: str = "figures_used") -> tuple[Figure, ...]:
    """Parse the citation list of an answer. Form only; whether it holds is checked separately."""
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    out: list[Figure] = []
    for number, row in enumerate(value, start=1):
        if not isinstance(row, Mapping):
            raise ValueError(f"{label} {number} must be an object")
        figure, read = row.get("figure"), row.get("value")
        if not isinstance(figure, str) or not figure.strip():
            raise ValueError(f"{label} {number} names the figure it read")
        if not isinstance(read, str) or not read.strip():
            raise ValueError(f"{label} {number} repeats the value it read")
        out.append(Figure(figure=figure.strip(), value=read.strip()))
    return tuple(out)


def survived(
    cited: tuple[Figure, ...], figures: Mapping[str, str]
) -> tuple[tuple[Figure, ...], tuple[str, ...]]:
    """Split a role's citations into the ones that hold and the reasons the others do not.

    A role that read its brief can point at the figures in it. A role that read nothing and wrote a
    confident sentence anyway cannot, and this is where that shows up - before the sentence is
    filed next to a decision as though it were evidence.
    """
    kept: list[Figure] = []
    lost: list[str] = []
    for claim in cited:
        wrong = cites(claim.figure, claim.value, figures)
        if wrong:
            lost.append(wrong)
            continue
        kept.append(claim)
    return tuple(kept), tuple(lost)


def figures_of(brief: Mapping[str, Any], *, prefix: str = "") -> dict[str, str]:
    """Every scalar figure in a brief, by the brief's own name, as a string.

    Nested tables are flattened with a dotted name, because an auditor reads a brief with sections
    in it and still has to be able to point at one number inside one section.
    """
    out: dict[str, str] = {}
    for key, value in brief.items():
        name = f"{prefix}{key}"
        if key in SKIP:
            continue
        if isinstance(value, Mapping):
            out.update(figures_of(value, prefix=f"{name}."))
        elif isinstance(value, list | tuple):
            for label, row in _rows(value):
                out.update(figures_of(row, prefix=f"{name}.{label}."))
        elif value is None:
            # A figure the record does not have is a fact about the record, and a reader that
            # says so should be able to point at it like any other.
            out[name] = "null"
        else:
            out[name] = str(value)
    return out


def _rows(value: list[Any] | tuple[Any, ...]) -> list[tuple[str, Mapping[str, Any]]]:
    """A list of rows that each name what they are about, or nothing at all."""
    out: list[tuple[str, Mapping[str, Any]]] = []
    for row in value:
        if not isinstance(row, Mapping):
            return []
        label = next(
            (
                str(row[key]).strip()
                for key in NAMES
                if isinstance(row.get(key), str) and str(row[key]).strip()
            ),
            "",
        )
        if not label:
            return []
        out.append((label, row))
    return out


def _words(figure: str) -> list[str]:
    """A citation's words, however the model separated them."""
    parts = re.split(r"[^0-9A-Za-z_]+", figure.strip())
    return [part for part in parts if part]


def _tail(name: str, words: list[str]) -> bool:
    segments = _words(name)
    return len(segments) >= len(words) and segments[-len(words) :] == words


def _flat(text: str) -> str:
    """One word, lower case, with every separator removed."""
    return re.sub(r"[^0-9a-z]", "", text.lower())


def _endings(name: str) -> set[str]:
    """Every tail of a figure's name as one word, and each of those without a trailing `value`.

    This is what lets `us10y_value` and `us10y value` mean `series.us10y.value`. It is a comparison
    of spellings, not of meanings, and it runs only after the exact and segment matches have failed.
    """
    segments = _words(name)
    out: set[str] = set()
    for k in range(1, len(segments) + 1):
        tail = segments[-k:]
        out.add(_flat("".join(tail)))
        if len(tail) > 1 and tail[-1] == "value":
            out.add(_flat("".join(tail[:-1])))
    return out


def same_figure(claimed: str, actual: str) -> bool:
    """Whether two values are the same figure, allowing a claim to carry fewer digits."""
    left, right = claimed.strip(), actual.strip()
    if left.lower() == right.lower():
        return True
    try:
        a, b = Decimal(left), Decimal(right)
    except InvalidOperation:
        return False
    if not a.is_finite() or not b.is_finite():
        return False
    exponent = a.as_tuple().exponent
    if not isinstance(exponent, int):
        return False
    return a == b.quantize(Decimal(1).scaleb(min(exponent, 0)))


def resolve(figure: str, figures: Mapping[str, str]) -> tuple[str, str]:
    """The full name of the figure being cited, and why it cannot be resolved when it cannot.

    A brief with sections in it names a figure `decisions.median_net_bps`, and a reader that cites
    `median_net_bps` means the same number as long as only one figure ends that way. The same holds
    for a row inside a series: `us10y value` is the tail of `series.us10y.value`, and so is
    `us10y_value` written as one word. A block measured with its own sample size resolves to the
    number in it, so `zscore_vs_recent` is that block's `value`. Where several figures share a tail,
    the shallowest wins, and where even that is a tie the citation is refused with the candidates
    named.
    """
    if figure in figures:
        return figure, ""
    words = _words(figure)
    if not words:
        return "", f"{figure!r} names no figure"
    ends = [name for name in figures if _tail(name, words)]
    if not ends:
        # A figure measured with its own sample size is a block - {value, n, window} - and a reader
        # that cites the block means the number in it. Its `n` and `window` stay separate figures.
        ends = [name for name in figures if _tail(name, [*words, "value"])]
    if not ends:
        # A name written as one word where the brief writes a path: `us10y_value` for
        # `series.us10y.value`, `us10y_zscore_vs_recent` for that block's own number. Compared with
        # every separator removed, and tried last, so a figure whose own name carries an underscore
        # has already resolved to itself and never reaches this.
        target = _flat(figure)
        ends = [name for name in figures if target in _endings(name)]
    if len(ends) == 1:
        return ends[0], ""
    if len(ends) > 1:
        depth = min(len(_words(name)) for name in ends)
        nearest = [name for name in ends if len(_words(name)) == depth]
        if len(nearest) == 1:
            return nearest[0], ""
        return "", f"{figure!r} could be {' or '.join(sorted(nearest))}; name it in full"
    return "", f"{figure!r} is not a figure in the brief"


def cites(figure: str, value: str, figures: Mapping[str, str]) -> str:
    """Empty when the citation holds; otherwise why it does not, in the words of the record."""
    name, unresolved = resolve(figure, figures)
    if unresolved:
        return unresolved
    if not same_figure(value, figures[name]):
        return f"{name} was {figures[name]}, not {value}"
    return ""
