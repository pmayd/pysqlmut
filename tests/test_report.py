"""Survivors of the same repeated rule form one group; different rules stay apart."""

from pysqlmut.report import group, shape
from pysqlmut.runner import SURVIVED, Result


def result(operator: str, description: str, before: str, after: str = "", line: int = 1) -> Result:
    return Result("q.sql", line, operator, description, SURVIVED, 0.1, before, after, 2)


def test_lines_that_differ_only_in_names_and_literals_share_a_shape():
    assert shape("d_gender_text.text_field = 'gender_text'") == shape("d_org_text.text_field = 'org_class_text'")
    assert shape("WHEN age <= 44 THEN 6") == shape("WHEN age <= 59 THEN 9")
    assert shape("WHEN age <= 44 THEN 6") != shape("WHEN age < 44 THEN 6")


def test_a_repeated_rule_is_one_group_counted_once_per_place():
    results = [
        result("comparison", "= -> <>", "d_gender_text.text_field = 'gender_text'", line=10),
        result("comparison", "= -> <>", "d_org_class_text.text_field = 'org_class_text'", line=20),
        result("comparison", "= -> <>", "d_nationality_text.text_field = 'nationality_text'", line=30),
        result("coalesce", "keep only the first argument", "COALESCE(d_gender_text.text_value, inv.gender_text)"),
        result("comparison", "<= -> <", "WHEN age <= 44 THEN 6"),
    ]
    groups = group(results)
    assert [len(g.results) for g in groups] == [3, 1, 1]
    assert groups[0].operator == "comparison"
    assert [r.line for r in groups[0].results] == [10, 20, 30]
