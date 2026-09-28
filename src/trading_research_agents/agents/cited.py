"""Claims have to point at a figure that exists, and code is what checks they do.

Two roles argue from figures: the validators, about one proposal, and the auditors, about what the
desk and the package have actually been doing. Both are held to the same rule - name the figure by
the brief's own name and repeat its value - so the rule lives here once.

The comparison is fair to a model that rounds. "-1.63" against a brief's "-1.631" is the same
citation said with fewer digits, and it is kept; "-2.0" is a different number, and it is not. A
claim of NaN is not a citation of anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

SKIP = ("mechanism", "basis")


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
            continue
        elif value is None:
            # A figure the record does not have is a fact about the record, and a reader that
            # says so should be able to point at it like any other.
            out[name] = "null"
        else:
            out[name] = str(value)
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
    `median_net_bps` means the same number as long as only one figure ends that way. Resolving that
    is not a loosening of the rule: the value is still checked, and a leaf that could mean two
    figures is refused with both names in the message.
    """
    if figure in figures:
        return figure, ""
    ends = [name for name in figures if name.rsplit(".", 1)[-1] == figure]
    if len(ends) == 1:
        return ends[0], ""
    if len(ends) > 1:
        return "", f"{figure!r} could be {' or '.join(sorted(ends))}; name it in full"
    return "", f"{figure!r} is not a figure in the brief"


def cites(figure: str, value: str, figures: Mapping[str, str]) -> str:
    """Empty when the citation holds; otherwise why it does not, in the words of the record."""
    name, unresolved = resolve(figure, figures)
    if unresolved:
        return unresolved
    if not same_figure(value, figures[name]):
        return f"{name} was {figures[name]}, not {value}"
    return ""
