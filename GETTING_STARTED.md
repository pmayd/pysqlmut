# Getting started

This walkthrough takes a small SQL query with a test that passes, shows which rules of the query the test does
not check, and closes the gaps. It takes a few minutes. The files are in
[`examples/getting-started`](examples/getting-started).

## 1. The project

`sql/revenue.sql` computes revenue per customer from paid orders:

```sql
-- Revenue per customer from paid orders, largest first.
SELECT
    customer,
    SUM(amount) AS revenue,
    COUNT(*) AS orders
FROM orders
WHERE status = 'paid' AND amount > 0
GROUP BY customer
ORDER BY revenue DESC;
```

`tests/test_revenue.py` runs the query on DuckDB with a few orders and checks the rows it returns:

```python
def test_revenue_per_customer_largest_first():
    rows = revenue(("alice", 30, "paid"), ("alice", 20, "paid"), ("bob", 10, "paid"))
    assert rows == [("alice", 50, 2), ("bob", 10, 1)]
```

The test passes. The question is whether it would fail if the query were wrong.

`pyproject.toml` tells pysqlmut where the SQL is and how to run the tests:

```toml
[tool.pysqlmut]
dialect = "duckdb"
files = ["sql/*.sql"]

[tool.pysqlmut.pytest]
python = "uv run python"
tests = ["tests"]
args = ["-q"]
```

To follow along, copy the example and install pysqlmut:

```bash
cp -R examples/getting-started /tmp/getting-started && cd /tmp/getting-started
uv tool install pysqlmut
```

## 2. See the mutants

`pysqlmut generate --show` lists every change pysqlmut would make, without running a test:

```text
$ pysqlmut generate --show
sql/revenue.sql: 1 statements, 9 mutants
      4 aggregate       SUM -> MAX
        - SUM(amount) AS revenue,
        + MAX(amount) AS revenue,
      7 logical         AND -> OR
        - WHERE status = 'paid' AND amount > 0
        + WHERE status = 'paid' OR amount > 0
      7 drop-condition  drop the right condition
        - WHERE status = 'paid' AND amount > 0
        + WHERE status = 'paid'
      ...
```

Each mutant changes one expression. The list is the same every time you run it, as long as the SQL and the
settings stay the same.

## 3. Run the tests against every mutant

```text
$ pysqlmut run --report pysqlmut-report.json
9 mutants, 1 worker: pytest workers (uv run python)
  9/9

per operator: caught / survived / not covered / accepted / timeout / error
  aggregate           1 /     0 /     0 /     0 /     0 /     0
  comparison          1 /     1 /     0 /     0 /     0 /     0
  drop-condition      0 /     2 /     0 /     0 /     0 /     0
  literal             0 /     1 /     0 /     0 /     0 /     0
  logical             0 /     1 /     0 /     0 /     0 /     0
  order               1 /     0 /     0 /     0 /     0 /     0
  string-literal      1 /     0 /     0 /     0 /     0 /     0

5 of 9 mutants survived or were not covered, in 5 groups
    1x comparison (> -> >=): WHERE x = 'S' AND x > N
       e.g. sql/revenue.sql:7
         - WHERE status = 'paid' AND amount > 0
         + WHERE status = 'paid' AND amount >= 0
    1x drop-condition (drop the left condition): WHERE x = 'S' AND x > N
       e.g. sql/revenue.sql:7
         - WHERE status = 'paid' AND amount > 0
         + WHERE amount > 0
    1x drop-condition (drop the right condition): WHERE x = 'S' AND x > N
       e.g. sql/revenue.sql:7
         - WHERE status = 'paid' AND amount > 0
         + WHERE status = 'paid'
    1x literal (N -> N): WHERE x = 'S' AND x > N
       e.g. sql/revenue.sql:7
         - WHERE status = 'paid' AND amount > 0
         + WHERE status = 'paid' AND amount > 1
    1x logical (AND -> OR): WHERE x = 'S' AND x > N
       e.g. sql/revenue.sql:7
         - WHERE status = 'paid' AND amount > 0
         + WHERE status = 'paid' OR amount > 0
```

The exit code is 1 because mutants survived. A **caught** mutant made a test fail. A mutant that **survived**
changed the query, yet every test still passed.

The survivors say two things about the test:

- Removing `status = 'paid'` (drop the left condition), or turning `AND` into `OR`, changes nothing the test
  notices: all its orders are paid. **No test checks that open orders are left out.**
- `amount > 0` can become `amount >= 0`, `amount > 1`, or disappear. **No test has an order with an amount of
  0 or 1.**

`pysqlmut report pysqlmut-report.json` prints the same groups again later from the report file, and
`--status caught` shows what the tests did catch.

## 4. Close a gap with a test

Add a test for the status rule:

```python
def test_only_paid_orders_count():
    rows = revenue(("alice", 30, "paid"), ("alice", 99, "open"))
    assert rows == [("alice", 30, 1)]
```

Run again:

```text
$ pysqlmut run --report pysqlmut-report.json
...
3 of 9 mutants survived or were not covered, in 3 groups
    1x comparison (> -> >=): WHERE x = 'S' AND x > N
    1x drop-condition (drop the right condition): WHERE x = 'S' AND x > N
    1x literal (N -> N): WHERE x = 'S' AND x > N
```

The two status survivors are caught now. Three survivors about the amount rule remain.

## 5. Decide about the rest

A survivor means one of two things. Either a rule matters and needs a test, or the change cannot make a
difference for any data the query can see.

**If the rule matters,** add a test with the amounts that tell the versions apart:

```python
def test_orders_without_an_amount_are_left_out():
    rows = revenue(("bob", 0, "paid"), ("bob", 1, "paid"), ("bob", 10, "paid"))
    assert rows == [("bob", 11, 2)]
```

```text
$ pysqlmut run
...
0 of 9 mutants survived or were not covered, in 0 groups
```

Every mutant is caught, and the exit code is 0.

**If it cannot matter,** for example because the `orders` table already guarantees positive amounts, accept
the survivors after reviewing them:

```text
$ pysqlmut accept pysqlmut-report.json
accepted 3 more survivors; pysqlmut-accepted.json now holds 3

$ pysqlmut run
...
per operator: caught / survived / not covered / accepted / timeout / error
  comparison          1 /     0 /     0 /     1 /     0 /     0
  drop-condition      1 /     0 /     0 /     1 /     0 /     0
  literal             0 /     0 /     0 /     1 /     0 /     0
...
0 of 9 mutants survived or were not covered, in 0 groups
```

Accepted mutants are not run again and do not fail the run. `pysqlmut-accepted.json` records each one by its
file, operator and the line before and after the change, so commit it with the project. When that line changes,
the mutant is new and gets tested again.

## Next

- Run pysqlmut on your own project: set `dialect`, `files` and `[tool.pysqlmut.pytest]` in its
  `pyproject.toml`, then `pysqlmut run`. The [README](README.md) lists every setting and operator.
- Leave out operators that produce noise for a file with `[[tool.pysqlmut.file]]`, or a single line with
  `-- pysqlmut: skip`.
- Use `--workers` to test several mutants at once on larger projects.
