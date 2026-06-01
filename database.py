import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "emporium.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                url          TEXT    NOT NULL,
                name         TEXT,
                selector     TEXT,
                in_stock     INTEGER DEFAULT NULL,
                last_checked TEXT,
                added_at     TEXT    DEFAULT (datetime('now')),
                UNIQUE(chat_id, url)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                chat_id  INTEGER PRIMARY KEY,
                email    TEXT NOT NULL,
                password TEXT NOT NULL
            )
        """)


def add_product(chat_id: int, url: str, name: str = None, selector: str = None) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO products (chat_id, url, name, selector) VALUES (?, ?, ?, ?)",
            (chat_id, url, name, selector),
        )
        if cur.rowcount == 0:
            raise ValueError("Este producto ya está en seguimiento.")
        return cur.lastrowid


def remove_product(chat_id: int, product_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM products WHERE id = ? AND chat_id = ?",
            (product_id, chat_id),
        )
        return cur.rowcount > 0


def get_products(chat_id: int):
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()


def get_all_products():
    with _conn() as conn:
        return conn.execute("SELECT * FROM products ORDER BY chat_id, id").fetchall()


def update_stock(product_id: int, in_stock: bool, name: str = None):
    with _conn() as conn:
        if name:
            conn.execute(
                "UPDATE products SET in_stock = ?, last_checked = ?, name = ? WHERE id = ?",
                (1 if in_stock else 0, datetime.now().isoformat(), name, product_id),
            )
        else:
            conn.execute(
                "UPDATE products SET in_stock = ?, last_checked = ? WHERE id = ?",
                (1 if in_stock else 0, datetime.now().isoformat(), product_id),
            )


def set_credentials(chat_id: int, email: str, password: str):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO credentials (chat_id, email, password) VALUES (?, ?, ?)",
            (chat_id, email, password),
        )


def get_credentials(chat_id: int):
    with _conn() as conn:
        row = conn.execute(
            "SELECT email, password FROM credentials WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return (row["email"], row["password"]) if row else None


def delete_credentials(chat_id: int):
    with _conn() as conn:
        conn.execute("DELETE FROM credentials WHERE chat_id = ?", (chat_id,))
