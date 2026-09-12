"""Survivors of the same repeated rule form one group; different rules stay apart."""

import pytest

from pysqlmut.report import group, load, shape
from pysqlmut.runner import SURVIVED, Result


def result(operator: str, description: str, before: str, after: str = "", line: int = 1) -> Result:
    return Result("q.sql", line, operator, description, SURVIVED, 0.1, before, after, 2)


def test_lines_that_differ_only_in_names_and_literals_share_a_shape():
    assert shape("d_status_text.text_field = 'status_text'") == "x.x = 'S'"
    assert shape("WHEN age <= 44 THEN 6") == "WHEN x <= N THEN N"
    assert shape("d_status_text.text_field = 'status_text'") == shape("d_org_text.text_field = 'region_text'")
    assert shape("WHEN age <= 44 THEN 6") == shape("WHEN age <= 59 THEN 9")
    assert shape("WHEN age <= 44 THEN 6") != shape("WHEN age < 44 THEN 6")


def test_a_repeated_rule_is_one_group_counted_once_per_place():
    results = [
        result("comparison", "= -> <>", "d_status_text.text_field = 'status_text'", line=10),
        result("comparison", "= -> <>", "d_region_text.text_field = 'region_text'", line=20),
        result("comparison", "= -> <>", "d_currency_text.text_field = 'currency_text'", line=30),
        result("coalesce", "keep only the first argument", "COALESCE(d_status_text.text_value, o.status_text)"),
        result("comparison", "<= -> <", "WHEN age <= 44 THEN 6"),
    ]
    groups = group(results)
    assert [len(g.results) for g in groups] == [3, 1, 1]
    assert groups[0].operator == "comparison"
    assert [r.line for r in groups[0].results] == [10, 20, 30]


def test_two_different_changes_of_one_line_with_the_same_description_stay_apart():
    before = "WHEN is_refund AND is_paid AND NOT is_shipped"
    results = [
        result("drop-condition", "drop the right condition", before, "WHEN is_refund AND is_paid"),
        result("drop-condition", "drop the right condition", before, "WHEN is_refund AND NOT is_shipped"),
    ]
    assert [len(g.results) for g in group(results)] == [1, 1]


def test_a_report_of_another_format_is_refused(tmp_path):
    path = tmp_path / "old.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="not a pysqlmut report"):
        load(path)
