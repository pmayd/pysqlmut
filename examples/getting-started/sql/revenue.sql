-- Revenue per customer from paid orders, largest first.
SELECT
    customer,
    SUM(amount) AS revenue,
    COUNT(*) AS orders
FROM orders
WHERE status = 'paid' AND amount > 0
GROUP BY customer
ORDER BY revenue DESC;
