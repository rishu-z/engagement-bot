"""
server.py — Tracking Server with SQLite

Use with the Telegram bot by setting:
  SERVER_URL=<this service URL>

Optional query args accepted by /visit:
  uid, post, sess, target, uname, xuser
"""

from datetime import datetime
from urllib.parse import urlparse
import os
import sqlite3

from flask import Flask, jsonify, redirect, request

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "clicks.db")

# Optional in-memory refs (if injected when running bot + server in same process)
session_links_ref = {}  # post_num -> {url, poster_id, x_username, ...}
user_cache_ref = {}     # tg_id -> telegram.User


# ──────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────
def get_db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = get_db_conn()
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS clicks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_num   INTEGER NOT NULL,
            post_num      INTEGER NOT NULL,
            tg_id         INTEGER NOT NULL,
            tg_username   TEXT,
            x_username    TEXT,
            x_link        TEXT,
            clicked_at    TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_click_unique
        ON clicks(session_num, post_num, tg_id)
        """
    )
    con.commit()
    con.close()


init_db()


def _extract_x_username(url: str) -> str:
    if not url:
        return "Unknown"
    try:
        parsed = urlparse(url)
        if "x.com" not in parsed.netloc.lower():
            return "Unknown"
        path_parts = [p for p in parsed.path.split("/") if p]
        if not path_parts:
            return "Unknown"
        return path_parts[0].replace("@", "")
    except Exception:
        return "Unknown"


def save_click(session_num, post_num, tg_id, tg_username, x_username, x_link):
    """Save one click to SQLite; ignore duplicates for same (session, post, user)."""
    con = get_db_conn()
    con.execute(
        """
        INSERT OR IGNORE INTO clicks
          (session_num, post_num, tg_id, tg_username, x_username, x_link, clicked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_num,
            post_num,
            tg_id,
            tg_username,
            x_username,
            x_link,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    con.commit()
    con.close()


# ──────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────
@app.route("/visit")
def visit():
    """
    Track a click and redirect.

    Supported URL format:
      /visit?uid=<tg_id>&post=<post_num>&sess=<session_num>&target=<url>
    """
    try:
        uid = int(request.args.get("uid", 0))
        post_num = int(request.args.get("post", 0))
        sess_num = int(request.args.get("sess", 0))
    except (ValueError, TypeError):
        return "Bad request", 400

    link_info = session_links_ref.get(post_num)

    target_url = (request.args.get("target") or "").strip()
    if not target_url and link_info:
        target_url = (link_info.get("url") or "").strip()
    if not target_url:
        target_url = "https://x.com"

    # Avoid logging own link click when poster_id is known.
    if link_info and uid == link_info.get("poster_id"):
        return redirect(target_url)

    # tg_username from query -> cache -> fallback
    tg_username = (request.args.get("uname") or "").strip()
    if not tg_username:
        cached = user_cache_ref.get(uid)
        if cached and getattr(cached, "username", None):
            tg_username = f"@{cached.username}"
        elif cached:
            tg_username = getattr(cached, "full_name", f"User {uid}")
        else:
            tg_username = f"User {uid}"

    x_username = (request.args.get("xuser") or "").replace("@", "").strip()
    if not x_username and link_info:
        x_username = (link_info.get("x_username") or "").replace("@", "").strip()
    if not x_username:
        x_username = _extract_x_username(target_url)

    save_click(
        session_num=sess_num,
        post_num=post_num,
        tg_id=uid,
        tg_username=tg_username,
        x_username=f"@{x_username}" if x_username != "Unknown" else "Unknown",
        x_link=target_url,
    )
    return redirect(target_url)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/clicks/<int:session_num>")
def get_session_clicks(session_num):
    con = get_db_conn()
    rows = con.execute(
        """
        SELECT post_num, tg_id, tg_username, x_username, x_link, clicked_at
        FROM clicks
        WHERE session_num=?
        ORDER BY clicked_at
        """,
        (session_num,),
    ).fetchall()
    con.close()

    clicks = [
        {
            "post_num": row["post_num"],
            "tg_id": row["tg_id"],
            "tg_username": row["tg_username"],
            "x_username": row["x_username"],
            "x_link": row["x_link"],
            "clicked_at": row["clicked_at"],
        }
        for row in rows
    ]
    return jsonify({"clicks": clicks})


@app.route("/api/clear/<int:session_num>", methods=["POST"])
def clear_session_clicks(session_num):
    con = get_db_conn()
    con.execute("DELETE FROM clicks WHERE session_num=?", (session_num,))
    con.commit()
    con.close()
    return jsonify({"ok": True, "session": session_num})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
