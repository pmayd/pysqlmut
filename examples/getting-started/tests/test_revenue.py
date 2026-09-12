from pathlib import Path

import duckdb

SQL = Path(__file__).parent.parent / "sql" / "revenue.sql"


def revenue(*orders: tuple[str, int, str]) -> list[tuple]:
    """Run revenue.sql on the given orders (customer, amount, status)."""
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INTEGER, status VARCHAR)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", orders)
    return con.execute(SQL.read_text()).fetchall()


def test_revenue_per_customer_largest_first():
    rows = revenue(("alice", 30, "paid"), ("alice", 20, "paid"), ("bob", 10, "paid"))
    assert rows == [("alice", 50, 2), ("bob", 10, 1)]
