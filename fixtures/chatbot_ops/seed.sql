-- chatbot_workspace fixture：给 SQLite 场景一个确定的小库（agent 用 bash + python 读它）。
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    opened_at TEXT NOT NULL
);

INSERT INTO tickets (id, title, severity, opened_at) VALUES
    (1, 'export job stalled', 'high',   '2026-08-02'),
    (2, 'dashboard slow',     'medium', '2026-08-05'),
    (3, 'login 500',          'high',   '2026-08-09'),
    (4, 'typo on landing',    'low',    '2026-08-11');
