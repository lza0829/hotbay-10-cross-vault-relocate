from __future__ import annotations
import sqlite3
import time
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = '''
CREATE TABLE IF NOT EXISTS vaults (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  capacity INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS partitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  release_day TEXT NOT NULL,
  vault_id INTEGER NOT NULL,
  tier TEXT NOT NULL,
  object_prefix TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '在册',
  created_at TEXT NOT NULL,
  FOREIGN KEY(vault_id) REFERENCES vaults(id)
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  object_key TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  entered_day TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);
CREATE TABLE IF NOT EXISTS migrate_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  from_tier TEXT NOT NULL,
  to_tier TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);
CREATE TABLE IF NOT EXISTS retention_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  max_age_days INTEGER NOT NULL,
  action TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS destroy_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  policy_id INTEGER NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id),
  FOREIGN KEY(policy_id) REFERENCES retention_policies(id)
);
CREATE TABLE IF NOT EXISTS inventory_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  object_key TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  vault_name TEXT NOT NULL,
  scanned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verify_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  drift_count INTEGER NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- 跨库改挂回执（架构一：同键改所属，不做关册新登，分区键/分区id不变）
CREATE TABLE IF NOT EXISTS vault_moves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  part_key TEXT NOT NULL,
  from_vault_id INTEGER NOT NULL,
  from_vault_name TEXT NOT NULL,
  to_vault_id INTEGER NOT NULL,
  to_vault_name TEXT NOT NULL,
  tier TEXT NOT NULL,
  object_prefix TEXT NOT NULL,
  clip_count INTEGER NOT NULL,
  request_id TEXT,
  status TEXT NOT NULL DEFAULT '成功',
  created_at TEXT NOT NULL,
  FOREIGN KEY(partition_id) REFERENCES partitions(id),
  FOREIGN KEY(from_vault_id) REFERENCES vaults(id),
  FOREIGN KEY(to_vault_id) REFERENCES vaults(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vault_moves_request
  ON vault_moves(request_id) WHERE request_id IS NOT NULL;
'''


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 写者拿锁时排队等待，避免并发改挂/登记直接报 database is locked
    conn.execute("PRAGMA busy_timeout=30000")
    # WAL 官方推荐档：应用崩溃不丢，仅整机断电的极端情况可能丢最后一笔；
    # 避免 FULL 档每提交两次 fsync 在容器/网络盘上的百毫秒级延迟。
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_db():
    # 架构一的并发口径：所有写事务统一 BEGIN IMMEDIATE，
    # 同一时刻只有一个写者；容量校验、所属改写、目录改库名、回执落账
    # 全部在同一把锁、同一笔事务内完成，失败整体回滚。
    # 拿锁阶段对偶发 BUSY（写者调度避让）做有界重试，绝不部分提交。
    deadline = time.monotonic() + 35
    attempt = 0
    while True:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as e:
            conn.close()
            if "database is locked" in str(e).lower() and time.monotonic() < deadline:
                time.sleep(min(0.05 * (attempt + 1), 0.5))
                attempt += 1
                continue
            raise
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    # WAL 是库级持久属性，只在初始化切一次，禁止每个连接重复执行
    # （重复 PRAGMA journal_mode=WAL 会反复触发 checkpoint/锁等待）。
    boot = connect()
    try:
        boot.execute("PRAGMA journal_mode=WAL")
        boot.commit()
    except sqlite3.OperationalError:
        # 不支持 WAL 的文件系统自动回退默认回滚日志模式
        pass
    finally:
        boot.close()
    with db() as conn:
        conn.executescript(SCHEMA)
