"""The shelf: which declaration file feeds which reading, and what a project actually has.

One file per class of data, so a vendor is a line inside a file. These tests pin the three rules
that keep a desk portable: a class the operator has not filled in costs nothing, a file that is
there but broken is named rather than skipped, and a role is asked only when it has something to
read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_research_agents.agents.context import (
    ContextError,
    declared_in,
    inspect_shelf,
)

MACRO = """
[[series]]
name = "us10y"
file = "macro/us10y.csv"
ts = "date"
ts_unit = "iso8601"
value = "value"
unit = "percent"
"""

NEWS = """
[[news]]
name = "policy"
file = "news/policy.csv"
source = "central bank"
"""

ROWS = "date,value\n2026-09-20,4.10\n2026-09-21,4.05\n"
DATED = "when,what\n2026-09-20T12:30:00+00:00,no change\n"


def _shelf(project: Path, **files: str) -> Path:
    out = project / "sources"
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / f"{name}.toml").write_text(text, encoding="utf-8")
    return out


def test_a_class_on_the_shelf_names_the_role_that_reads_it(tmp_path: Path) -> None:
    _shelf(tmp_path, macro=MACRO, news=NEWS)
    declared = declared_in(tmp_path)

    assert declared.readings == ("macro",)
    assert declared.for_reading("macro") == (tmp_path / "sources" / "macro.toml",)
    assert declared.text == [tmp_path / "sources" / "news.toml"]
    assert set(declared.classes) == {"macro", "news"}
    assert declared.readings == ("macro",)


def test_chain_and_derivative_figures_both_feed_the_onchain_reading(tmp_path: Path) -> None:
    _shelf(tmp_path, onchain=MACRO, derivatives=MACRO)
    declared = declared_in(tmp_path)

    read = declared.for_reading("onchain")
    assert [p.name for p in read] == ["derivatives.toml", "onchain.toml"]
    assert declared.readings == ("onchain",)


def test_a_file_of_comments_declares_nothing_and_costs_nothing(tmp_path: Path) -> None:
    _shelf(tmp_path, macro="# uncomment an entry to switch this on\n", sentiment=MACRO)
    declared = declared_in(tmp_path)

    assert declared.empty == ("macro",)
    assert declared.readings == ("sentiment",)
    assert "macro" not in declared.classes
    assert declared.readings == ("sentiment",)


def test_the_older_filenames_are_still_read(tmp_path: Path) -> None:
    (tmp_path / "series.toml").write_text(MACRO, encoding="utf-8")
    (tmp_path / "onchain.toml").write_text(MACRO, encoding="utf-8")
    declared = declared_in(tmp_path)

    assert declared.for_reading("macro") == (tmp_path / "series.toml",)
    assert declared.for_reading("onchain") == (tmp_path / "onchain.toml",)


def test_the_shelf_wins_over_the_older_filename(tmp_path: Path) -> None:
    _shelf(tmp_path, macro=MACRO)
    (tmp_path / "series.toml").write_text(MACRO, encoding="utf-8")

    assert declared_in(tmp_path).for_reading("macro") == (tmp_path / "sources" / "macro.toml",)


def test_an_empty_shelf_file_does_not_fall_back_to_the_older_filename(tmp_path: Path) -> None:
    _shelf(tmp_path, macro="# nothing yet\n")
    (tmp_path / "series.toml").write_text(MACRO, encoding="utf-8")
    declared = declared_in(tmp_path)

    assert declared.empty == ("macro",)
    assert declared.for_reading("macro") == ()


def test_an_override_wins_over_the_shelf(tmp_path: Path) -> None:
    _shelf(tmp_path, macro=MACRO)
    other = tmp_path / "second-try.toml"
    other.write_text(MACRO, encoding="utf-8")

    declared = declared_in(tmp_path, overrides={"macro": other})
    assert declared.for_reading("macro") == (other,)


def test_an_override_switches_a_class_the_shelf_left_empty(tmp_path: Path) -> None:
    _shelf(tmp_path, sentiment="# nothing yet\n")
    mine = tmp_path / "mine.toml"
    mine.write_text(MACRO, encoding="utf-8")

    declared = declared_in(tmp_path, overrides={"sentiment": mine})
    assert declared.empty == ()
    assert declared.for_reading("sentiment") == (mine,)


def test_an_unknown_class_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(ContextError, match="orderflow is not a class of data"):
        declared_in(tmp_path, overrides={"orderflow": tmp_path / "x.toml"})


def test_inspect_reads_the_declarations_without_reading_the_data(tmp_path: Path) -> None:
    _shelf(tmp_path, macro=MACRO, news=NEWS)
    (tmp_path / "sources" / "macro").mkdir()
    (tmp_path / "sources" / "macro" / "us10y.csv").write_text(ROWS, encoding="utf-8")
    (tmp_path / "sources" / "news").mkdir()
    (tmp_path / "sources" / "news" / "policy.csv").write_text(DATED, encoding="utf-8")

    readings = {r.name: r for r in inspect_shelf(declared_in(tmp_path))}
    assert readings["macro"].entries == ("us10y",)
    assert readings["news"].entries == ("policy",)
    assert all(r.usable for r in readings.values())


def test_inspect_names_an_export_that_is_not_there(tmp_path: Path) -> None:
    _shelf(tmp_path, macro=MACRO)
    (reading,) = inspect_shelf(declared_in(tmp_path))

    assert reading.missing == ("macro/us10y.csv",)
    assert not reading.usable


def test_inspect_carries_the_loader_words_for_a_broken_file(tmp_path: Path) -> None:
    _shelf(tmp_path, macro='[[series]]\nname = "x"\n')
    (reading,) = inspect_shelf(declared_in(tmp_path))

    assert "names its file" in reading.error
    assert not reading.usable
