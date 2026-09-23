-- sales_v2 fixture seed（SQLite，SQLiteFixture 每 iteration 独立建库）
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount_cny REAL NOT NULL,
    ordered_at TEXT NOT NULL
);

INSERT INTO customers (id, name, region) VALUES
    (1, 'acmeCorp', 'north'),
    (2, 'globex',   'south'),
    (3, 'initech',  'north');

INSERT INTO orders (customer_id, amount_cny, ordered_at) VALUES
    (1, 12000.0, '2026-08-03'),
    (1,  8000.0, '2026-08-21'),
    (2,  9500.0, '2026-08-11'),
    (3,  4300.0, '2026-07-02');
