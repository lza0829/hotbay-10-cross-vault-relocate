"""跨库改挂端到端闭合测试（全局唯一架构：同键改所属）。

运行：
  PYTHONPATH=. python tests/test_vault_move.py
覆盖：五视图联动同刷、幂等可重放、失败整体回滚、并发不得双在册/双计、容量边界。
"""
import os
import sys
import tempfile
import threading
import traceback

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

_tmp = tempfile.mkdtemp(prefix="hotbay-test-")
os.environ["HOTBAY_DB"] = os.path.join(_tmp, "hotbay.db")

from fastapi import HTTPException  # noqa: E402

from app.db import db, write_db  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import vault_move as mv  # noqa: E402
from app.services import partitions as psvc  # noqa: E402
from app.services import verify as vsvc  # noqa: E402
from app.services.core import vault_partition_count  # noqa: E402
from app.routers.dashboard import overview  # noqa: E402

seed_if_empty()
failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)


def counts():
    with db() as c:
        rows = c.execute(
            "SELECT vault_id, COUNT(*) c FROM partitions WHERE status='在册' GROUP BY vault_id"
        ).fetchall()
        grp = {r["vault_id"]: r["c"] for r in rows}
        return {vid: grp.get(vid, 0) for vid in (1, 2, 3)}


def closure_invariant(tag):
    """闭合不变量：占用页==分区册聚合；目录库名==所属库；前缀不双在册；和守恒。"""
    with db() as c:
        grp = dict((r["vault_id"], r["c"]) for r in c.execute(
            "SELECT vault_id, COUNT(*) c FROM partitions WHERE status='在册' GROUP BY vault_id"))
        for vid in (1, 2, 3):
            check(f"[{tag}] 库{vid}占用页与分区册一致",
                  grp.get(vid, 0) == vault_partition_count(c, vid))
        bad = c.execute(
            """SELECT p.id FROM partitions p
               JOIN clips cl ON cl.partition_id=p.id
               JOIN inventory_objects io ON io.object_key=cl.object_key
               JOIN vaults v ON v.id=p.vault_id
               WHERE p.status='在册' AND io.vault_name <> v.name""").fetchall()
        check(f"[{tag}] 目录库名与所属库全一致（无半刷）", not bad, str([r["id"] for r in bad]))
        dual = c.execute(
            """SELECT object_prefix FROM partitions
               WHERE status='在册' GROUP BY object_prefix HAVING COUNT(DISTINCT vault_id) > 1""").fetchall()
        check(f"[{tag}] 无对象前缀双在册", not dual)
        total = c.execute("SELECT COUNT(*) c FROM partitions WHERE status='在册'").fetchone()["c"]
        check(f"[{tag}] 各库占用之和 == 在册分区总数", sum(grp.values()) == total)


base = counts()
check("种子基线 库1=1 库2=2 库3=1", base == {1: 1, 2: 2, 3: 1})
closure_invariant("基线")

# 1. 正常改挂：分区册/目录/两侧占用/回执/总览同刷
before = counts()
r = mv.move_partition(1, 2, request_id="req-001")
after = counts()
check("回执已落账", r["id"] > 0 and r["重放"] is False)
check("甲库在册减一", after[1] == before[1] - 1 and r["甲库在册"] == after[1])
check("乙库在册加一", after[2] == before[2] + 1 and r["乙库在册"] == after[2])
check("占用和守恒（甲未减乙已加即未闭合）", sum(after.values()) == sum(before.values()))
with db() as c:
    p = c.execute("SELECT * FROM partitions WHERE id=1").fetchone()
    check("分区键不变", p["part_key"] == "P-红海2024-主成片")
    check("温层保留", p["tier"] == "热档")
    check("所属库已改且仍在册", p["vault_id"] == 2 and p["status"] == "在册")
    check("素材段保留（3段）",
          c.execute("SELECT COUNT(*) c FROM clips WHERE partition_id=1").fetchone()["c"] == 3)
    inv = c.execute(
        """SELECT io.vault_name, v.name FROM inventory_objects io
           JOIN clips cl ON cl.object_key=io.object_key
           JOIN partitions p ON p.id=cl.partition_id
           JOIN vaults v ON v.id=p.vault_id
           WHERE p.id=1""").fetchall()
    check("目录清单3段库名全刷为乙库", len(inv) == 3 and all(x["vault_name"] == x["name"] for x in inv))
closure_invariant("改挂后")

# 2. 幂等重放
r2 = mv.move_partition(1, 2, request_id="req-001")
check("同键返回重放且同一回执", r2["重放"] is True and r2["id"] == r["id"])
check("重放后占用未再变迁", counts() == after)
with db() as c:
    check("同键回执仅一条",
          c.execute("SELECT COUNT(*) c FROM vault_moves WHERE request_id='req-001'").fetchone()["c"] == 1)

# 3. 失败回滚
for kw, pid, vid, code in (("同库改挂被拒", 1, 2, 400), ("幽灵库被拒", 2, 999, 404)):
    try:
        mv.move_partition(pid, vid); check(kw, False)
    except HTTPException as e:
        check(kw, e.status_code == code)
check("拒绝后占用不变（无半挂）", counts() == after)

with write_db() as c:
    c.execute("UPDATE vaults SET capacity=(SELECT COUNT(*) FROM partitions WHERE vault_id=2 AND status='在册') WHERE id=2")
try:
    mv.move_partition(4, 2, request_id="req-full"); check("满载拒绝改挂", False)
except HTTPException as e:
    check("满载拒绝改挂", e.status_code == 400 and "已满" in e.detail)
with db() as c:
    check("满载拒绝无回执", c.execute("SELECT COUNT(*) c FROM vault_moves WHERE request_id='req-full'").fetchone()["c"] == 0)
    check("分区4仍挂原库", c.execute("SELECT vault_id FROM partitions WHERE id=4").fetchone()["vault_id"] == 3)
    c.execute("UPDATE vaults SET capacity=24 WHERE id=2")

with write_db() as c:
    c.execute("UPDATE partitions SET status='已过期' WHERE id=4")
try:
    mv.move_partition(4, 1, request_id="req-dead"); check("非在册改挂被拒", False)
except HTTPException as e:
    check("非在册改挂被拒", e.status_code == 400)
with write_db() as c:
    c.execute("UPDATE partitions SET status='在册' WHERE id=4")
closure_invariant("失败回滚后")

try:
    with write_db() as c:
        c.execute("INSERT INTO vaults(name, capacity, active) VALUES ('临时回滚库', 1, 1)")
        raise RuntimeError("boom")
except RuntimeError:
    pass
with db() as c:
    check("写事务异常整体回滚",
          c.execute("SELECT COUNT(*) c FROM vaults WHERE name='临时回滚库'").fetchone()["c"] == 0)

# 4. 并发改挂风暴：不得双在册/双计
errors = []


def worker_storm(wid):
    try:
        for i in range(20):
            pid = (i + wid) % 4 + 1
            target = ((pid + i + wid) % 3) + 1
            try:
                mv.move_partition(pid, target, request_id=f"storm-{wid}-{i}")
            except HTTPException:
                pass
    except Exception:
        errors.append(traceback.format_exc())


ts = [threading.Thread(target=worker_storm, args=(w,)) for w in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
check("并发风暴无未捕获异常", not errors, str(errors[:1]))
closure_invariant("并发风暴后")

# 5. 容量边界：改挂与登记并发，恰好填满不超限
with write_db() as c:
    cur = vault_partition_count(c, 2)
    c.execute("UPDATE vaults SET capacity=? WHERE id=2", (cur + 2,))
bound_cap = cur + 2
ok = {"n": 0}; rejected = {"n": 0}; lock = threading.Lock()


def boundary_move(i):
    try:
        mv.move_partition([1, 3, 4][i % 3], 2, request_id=f"bnd-m-{i}")
        with lock: ok["n"] += 1
    except HTTPException:
        with lock: rejected["n"] += 1


def boundary_create(i):
    try:
        psvc.create_partition(f"BK-{i}-{threading.get_ident()}", f"边界{i}", "2026-01-01",
                              2, "热档", f"reel/bk{i}/",
                              [{"object_key": f"reel/bk{i}/seg-{threading.get_ident()}.bin", "bytes": 10}])
        with lock: ok["n"] += 1
    except HTTPException:
        with lock: rejected["n"] += 1


jobs = [boundary_move] * 6 + [boundary_create] * 6
ts = [threading.Thread(target=f, args=(i,)) for i, f in enumerate(jobs)]
[t.start() for t in ts]; [t.join() for t in ts]
with db() as c:
    check("乙库最终恰好额定、未超限", vault_partition_count(c, 2) == bound_cap)
    check("成功+拒绝==总请求", ok["n"] + rejected["n"] == 12)
    check("恰好2个请求成功（无双计）", ok["n"] == 2, f"ok={ok['n']}")
    c.execute("UPDATE vaults SET capacity=24 WHERE id=2")
closure_invariant("容量边界后")

# 6. 同 request_id 并发：一条回执、一次变迁
try:
    mv.move_partition(2, 1, request_id="reset-p2")
except HTTPException:
    pass
pre = counts()
replays = {"n": 0}; wins = {"n": 0}; lk = threading.Lock()


def idem(i):
    try:
        out = mv.move_partition(2, 3, request_id="idem-shared")
        with lk:
            (replays if out.get("重放") else wins)["n"] += 1
    except HTTPException:
        pass


ts = [threading.Thread(target=idem, args=(i,)) for i in range(6)]
[t.start() for t in ts]; [t.join() for t in ts]
with db() as c:
    rcpt = c.execute("SELECT * FROM vault_moves WHERE request_id='idem-shared'").fetchone()
    check("同键并发仅一条回执", c.execute(
        "SELECT COUNT(*) c FROM vault_moves WHERE request_id='idem-shared'").fetchone()["c"] == 1)
check("一次真实变迁+其余重放", wins["n"] == 1 and replays["n"] == 5)
post = counts()
check("同键并发占用只变迁一次（按回执方向）",
      post[rcpt["from_vault_id"]] == pre[rcpt["from_vault_id"]] - 1
      and post[rcpt["to_vault_id"]] == pre[rcpt["to_vault_id"]] + 1
      and sum(post.values()) == sum(pre.values()))
closure_invariant("幂等并发后")

# 7. 互证与总览徽章
vr = vsvc.run_verify()
check("改挂后互证一致无漂移", vr["status"] == "一致" and vr["drift_count"] == 0, str(vr.get("drifts")))
ov = overview()
api_vaults = {v["id"]: v["在册分区数"] for v in ov["片库间"]}
real = counts()
check("总览在册徽章 == 实际聚合", all(api_vaults.get(k) == real.get(k, 0) for k in (1, 2, 3)), str(api_vaults))

print()
if failures:
    print(f"❌ {len(failures)} 项失败:", failures)
    sys.exit(1)
print("✅ 全部闭合测试通过")
