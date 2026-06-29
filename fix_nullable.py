"""
One-time fix: makes shared_link.document_id nullable so deleting
a file no longer crashes with IntegrityError.

SQLite doesn't support ALTER COLUMN, so we recreate the table.
Run once:  python fix_nullable.py
"""
import sqlite3, os

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")
print(f"Database: {DB}")

conn = sqlite3.connect(DB)
cur  = conn.cursor()
cur.execute("PRAGMA foreign_keys = OFF")

# Check current definition
cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='shared_link'")
row = cur.fetchone()
if row:
    print("Current shared_link schema found.")
else:
    print("Table shared_link not found — nothing to do.")
    conn.close()
    exit()

print("Recreating shared_link with nullable document_id...")

cur.executescript("""
-- Step 1: rename old table
ALTER TABLE shared_link RENAME TO shared_link_old;

-- Step 2: create new table with document_id nullable
CREATE TABLE shared_link (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    token            VARCHAR(64)  UNIQUE NOT NULL,
    document_id      INTEGER      REFERENCES document(id),
    created_by       INTEGER      NOT NULL REFERENCES user(id),
    visibility       VARCHAR(10)  DEFAULT 'public',
    password         VARCHAR(255),
    allowed_emails   TEXT,
    created_at       DATETIME     DEFAULT CURRENT_TIMESTAMP,
    expires_at       DATETIME,
    view_count       INTEGER      DEFAULT 0,
    download_count   INTEGER      DEFAULT 0,
    download_limit   INTEGER,
    is_active        BOOLEAN      DEFAULT 1,
    note             TEXT
);

-- Step 3: copy data
INSERT INTO shared_link
    SELECT id, token, document_id, created_by, visibility, password,
           allowed_emails, created_at, expires_at, view_count,
           download_count, download_limit, is_active, note
    FROM shared_link_old;

-- Step 4: drop old table
DROP TABLE shared_link_old;
""")

conn.commit()
cur.execute("PRAGMA foreign_keys = ON")
conn.close()
print("Done. Restart Flask.")