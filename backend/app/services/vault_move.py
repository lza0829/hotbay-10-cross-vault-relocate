"""跨库改挂（跨片库间迁册）。

全局只贯彻一种架构 —— 架构一「以同键改所属为准」：
  * 分区键 part_key、分区 id 不变；温层 tier、素材段 clips 原样保留；
  * 同一笔写事务内：改 partitions.vault_id（分区所属库字段）
    + 改 inventory_objects.vault_name（目录清单库名列）
    + 落 vault_moves 回执（关册/改挂回执口径）；
  * 两侧占用页（各库在册分区数）是从 partitions.status='在册' 实时聚合的派生值，
    所属行一移动，甲库在册减一、乙库在册封加一自动同刷，禁止任何手工计数；
  * 总览在册徽章同样只从分区册聚合，无独立计数可漂移；
  * 写事务统一 BEGIN IMMEDIATE 串行化，并发改挂/登记不会双在册、不会双计；
  * request_id 幂等：同键重试返回同一回执，占用变迁只发生一次（可重放）。

架构二「先关册再新登、新键」在本系统不实现、不混用：分区册没有
关册/重开生命周期，任何改挂都不得改 part_key、不得制造第二条在册封行。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.db import db, write_db
from app.services.core import now, partition_row, vault_partition_count


def _vault_row(conn, vault_id: int):
    v = conn.execute("SELECT * FROM vaults WHERE id=?", (vault_id,)).fetchone()
    if not v:
        raise HTTPException(404, detail="目标片库间不存在")
    if not v["active"]:
        raise HTTPException(400, detail="目标片库间已停用，拒绝改挂")
    return v


def _receipt_payload(conn, mv) -> dict:
    """回执 + 两侧占用页同刷快照：调用方读的就是事务提交后的一致状态。"""
    d = dict(mv)
    d["甲库在册"] = vault_partition_count(conn, mv["from_vault_id"])
    d["乙库在册"] = vault_partition_count(conn, mv["to_vault_id"])
    d["甲库额定"] = conn.execute(
        "SELECT capacity FROM vaults WHERE id=?", (mv["from_vault_id"],)
    ).fetchone()["capacity"]
    d["乙库额定"] = conn.execute(
        "SELECT capacity FROM vaults WHERE id=?", (mv["to_vault_id"],)
    ).fetchone()["capacity"]
    return d


def move_partition(partition_id: int, to_vault_id: int, request_id: str | None = None) -> dict:
    with write_db() as conn:
        # 幂等重放：同一请求键直接回放原回执，占用不会第二次加减。
        if request_id:
            prev = conn.execute(
                "SELECT * FROM vault_moves WHERE request_id=?", (request_id,)
            ).fetchone()
            if prev:
                return {"重放": True, **_receipt_payload(conn, prev)}

        p = partition_row(conn, partition_id)
        if p["status"] != "在册":
            raise HTTPException(400, detail="仅在册分区可改挂，非在册对象不得跨库登记")
        if p["vault_id"] == to_vault_id:
            raise HTTPException(400, detail="目标片库间与当前所属相同，无须改挂")

        src = conn.execute("SELECT * FROM vaults WHERE id=?", (p["vault_id"],)).fetchone()
        dst = _vault_row(conn, to_vault_id)

        # 容量校验在写锁内做，与登记共用同一串行点，杜绝并发双计超限。
        dst_used = vault_partition_count(conn, dst["id"])
        if dst_used >= dst["capacity"]:
            raise HTTPException(400, detail="目标片库间在册分区已满，拒绝改挂")

        clip_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM clips WHERE partition_id=?", (partition_id,)
        ).fetchone()["c"]
        if clip_cnt == 0:
            raise HTTPException(400, detail="分区无素材段，拒绝空册改挂")

        # 1) 分区册：同键改所属。分区键、温层、对象前缀、素材段一律不动。
        conn.execute(
            "UPDATE partitions SET vault_id=? WHERE id=? AND status='在册'",
            (dst["id"], partition_id),
        )
        # 2) 目录清单：该分区全部素材段的库名列同刷为乙库（同对象键改库名，不删不插）。
        conn.execute(
            """UPDATE inventory_objects
                 SET vault_name=?, scanned_at=?
               WHERE object_key IN (
                 SELECT object_key FROM clips WHERE partition_id=?
               )""",
            (dst["name"], now(), partition_id),
        )
        # 3) 改挂回执落账：甲乙不得同时把同一对象前缀算两册 —— 全程只有一条在册封行。
        cur = conn.execute(
            """INSERT INTO vault_moves(
                 partition_id, part_key,
                 from_vault_id, from_vault_name,
                 to_vault_id, to_vault_name,
                 tier, object_prefix, clip_count, request_id, status, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?, '成功', ?)""",
            (
                partition_id,
                p["part_key"],
                src["id"],
                src["name"],
                dst["id"],
                dst["name"],
                p["tier"],
                p["object_prefix"],
                clip_cnt,
                request_id,
                now(),
            ),
        )
        mv = conn.execute("SELECT * FROM vault_moves WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"重放": False, **_receipt_payload(conn, mv)}


def list_moves():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM vault_moves ORDER BY id DESC"
        ).fetchall()
        return [_receipt_payload(conn, r) for r in rows]
