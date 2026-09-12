"""A sample query and a test for it, shared by the tests that run pysqlmut on a project."""

PAID = """-- Paid orders per customer.
SELECT customer, SUM(amount) AS total
FROM orders
WHERE status = 'paid' AND amount > 0
GROUP BY customer
ORDER BY customer;
"""

# The test checks the totals of paid orders but has no order with amount 0, so it cannot tell
# amount > 0 from amount >= 0.
TEST_PAID = """
from pathlib import Path

import duckdb


def test_paid_totals():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('a', 7, 'open'), ('b', 2, 'paid')")
    assert con.execute((Path(__file__).parent / "paid.sql").read_text()).fetchall() == [("a", 8), ("b", 2)]
"""
