"""
Migration script — adds all new columns for the Google Drive-style sharing upgrade.
Run ONCE before restarting Flask:

    python migrate.py

Safe to run multiple times (checks before altering).
"""

import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")

MIGRATIONS = [
    # (table, column, definition)
    ("shared_link", "allowed_emails",  "TEXT"),
    ("shared_link", "download_count",  "INTEGER DEFAULT 0"),
    ("shared_link", "download_limit",  "INTEGER"),
    ("shared_link", "note",            "TEXT"),
]

CREATE_SHARE_TRANSACTION = """
CREATE TABLE IF NOT EXISTS share_transaction (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id         INTEGER NOT NULL REFERENCES shared_link(id),
    recipient_email TEXT    NOT NULL,
    sent_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    delivered       BOOLEAN DEFAULT 0,
    opened_at       DATETIME,
    downloaded_at   DATETIME,
    open_token      TEXT UNIQUE NOT NULL,
    error_msg       TEXT
);
"""

def migrate():
    print(f"Connecting to: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # 1. Column additions on existing tables
    for table, col, defn in MIGRATIONS:
        cur.execute(f"PRAGMA table_info({table})")
        existing = [r[1] for r in cur.fetchall()]
        if col in existing:
            print(f"  ✓ {table}.{col} already exists")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            conn.commit()
            print(f"  + Added {table}.{col} {defn}")

    # 2. Create share_transaction table
    cur.execute(CREATE_SHARE_TRANSACTION)
    conn.commit()
    print("  ✓ share_transaction table ready")

    conn.close()
    print("\nDone. Restart Flask.")

if __name__ == "__main__":
    migrate()