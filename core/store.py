# -*- coding: utf-8 -*-
"""
方案持久化 + 分享短码存储（SQLite）

为什么用 SQLite：
    · 纯标准库（sqlite3），零额外依赖；
    · 云端发布沙箱只给一个 HTTP 端口、不提供数据库服务，SQLite 随项目走，
      是唯一能在沙箱里落地的持久化方案（MySQL/PG/Redis 都会被拒发布）；
    · 单文件、可随项目一起拷贝，本地与线上同一套代码。

两张表：
    plans    —— 生成的方案正文（原「最近 5 份内存历史」升级为不限量 + 重启不丢）
    shares   —— 分享短码 → 方案（一个方案可生成多个短码，各自记录创建时间）

线程安全：
    FastAPI/Gradio 是多线程处理请求的，sqlite3 连接不能跨线程共用。
    这里用 threading.local() 给每个线程各持一个连接，并开启 WAL
    提升并发读性能（写少读多，WAL 正合适）。
"""

import os
import secrets
import sqlite3
import threading
import time

_DB_LOCK = threading.Lock()
_LOCAL = threading.local()

_DB_PATH = None


def set_db_path(path):
    """指定数据库文件位置（默认项目根下 data/plans.db）"""
    global _DB_PATH
    _DB_PATH = path


def _default_db_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "plans.db")


def _conn():
    """取当前线程的连接（惰性建连 + 建表）"""
    if _DB_PATH is None:
        set_db_path(_default_db_path())
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        c = sqlite3.connect(_DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _LOCAL.conn = c
    return c


def init_db():
    """建表（幂等，可重复调用）"""
    c = _conn()
    with _DB_LOCK:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS plans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                destination TEXT,
                origin      TEXT,
                days        INTEGER,
                people      INTEGER,
                budget      TEXT,
                interests   TEXT,
                mode        TEXT,
                profile     TEXT,
                content     TEXT NOT NULL,
                created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_plans_created
                ON plans(created_at DESC);

            CREATE TABLE IF NOT EXISTS shares (
                code       TEXT PRIMARY KEY,
                plan_id    INTEGER NOT NULL,
                created_at REAL NOT NULL,
                hits       INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE
            );
            """
        )
        c.commit()


# ---------------------------------------------------------------------------
# 方案
# ---------------------------------------------------------------------------

def save_plan(title, content, destination="", origin="", days=0, people=0,
              budget="", interests=None, mode="", profile=""):
    """存一份方案，返回它的 id（失败返回 None）"""
    if not str(content or "").strip():
        return None
    if isinstance(interests, (list, tuple)):
        interests = "、".join(str(x) for x in interests)
    try:
        init_db()
        c = _conn()
        with _DB_LOCK:
            cur = c.execute(
                "INSERT INTO plans (title, destination, origin, days, people,"
                " budget, interests, mode, profile, content, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(title)[:200], str(destination or ""), str(origin or ""),
                 int(days or 0), int(people or 0), str(budget or ""),
                 str(interests or ""), str(mode or ""), str(profile or ""),
                 str(content), time.time()),
            )
            c.commit()
            return cur.lastrowid
    except Exception:
        return None


def list_plans(limit=50):
    """按时间倒序列出方案（不含正文，省内存）"""
    try:
        init_db()
        c = _conn()
        rows = c.execute(
            "SELECT id, title, destination, days, people, created_at"
            " FROM plans ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_plan(plan_id):
    try:
        init_db()
        c = _conn()
        r = c.execute("SELECT * FROM plans WHERE id=?", (int(plan_id),)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def delete_plan(plan_id):
    try:
        init_db()
        c = _conn()
        with _DB_LOCK:
            c.execute("DELETE FROM shares WHERE plan_id=?", (int(plan_id),))
            cur = c.execute("DELETE FROM plans WHERE id=?", (int(plan_id),))
            c.commit()
            return cur.rowcount > 0
    except Exception:
        return False


def count_plans():
    try:
        init_db()
        return int(_conn().execute("SELECT COUNT(*) FROM plans").fetchone()[0])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 分享短码
# ---------------------------------------------------------------------------

_CODE_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"   # 去掉易混的 l/o/0/1
_CODE_LEN = 8


def create_share(plan_id):
    """给某份方案生成一个分享短码（同一方案重复调用会生成新码，互不影响）"""
    try:
        init_db()
        c = _conn()
        for _ in range(12):
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
            try:
                with _DB_LOCK:
                    c.execute(
                        "INSERT INTO shares (code, plan_id, created_at) VALUES (?,?,?)",
                        (code, int(plan_id), time.time()),
                    )
                    c.commit()
                return code
            except sqlite3.IntegrityError:
                continue        # 撞码，换一个重试
        return None
    except Exception:
        return None


def resolve_share(code):
    """短码 → (方案 dict, 状态)。状态：ok / not_found"""
    code = str(code or "").strip()
    if not code or len(code) > 32:
        return None, "not_found"
    try:
        init_db()
        c = _conn()
        r = c.execute("SELECT plan_id, hits FROM shares WHERE code=?", (code,)).fetchone()
        if not r:
            return None, "not_found"
        plan = get_plan(r["plan_id"])
        if not plan:
            return None, "not_found"
        with _DB_LOCK:
            c.execute("UPDATE shares SET hits=hits+1 WHERE code=?", (code,))
            c.commit()
        return plan, "ok"
    except Exception:
        return None, "not_found"


def count_shares():
    try:
        init_db()
        return int(_conn().execute("SELECT COUNT(*) FROM shares").fetchone()[0])
    except Exception:
        return 0


def get_or_create_share(plan_id):
    """取某份方案的稳定短码（已有则复用，没有才新建）

    海报上的二维码要用它：同一份方案反复下载海报，二维码必须指向同一个链接，
    否则每点一次就发一个新码，先前印出去/发出去的二维码全部作废（2026-10-14）。
    """
    try:
        init_db()
        c = _conn()
        r = c.execute(
            "SELECT code FROM shares WHERE plan_id=? ORDER BY created_at LIMIT 1",
            (int(plan_id),)).fetchone()
        if r:
            return r["code"]
        return create_share(plan_id)
    except Exception:
        return None
