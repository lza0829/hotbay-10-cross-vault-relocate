# 热档片间（Hotbay）

影院素材分区温层：登记 → 冷热迁移 → 跨库改挂 → 保留销毁 → 目录互证。

- 前端 http://127.0.0.1:6300
- API http://127.0.0.1:11300
- `docker compose up -d --build`

## 跨库改挂（跨片库间迁册）架构口径

全局只贯彻一种架构：**以同键改所属为准（架构一）**，禁止与「先关册再新登」混用。

- 分区键 `part_key`、分区 id 不变；温层与素材段原样保留；
- 一笔 `BEGIN IMMEDIATE` 写事务内联动：分区册所属库（`partitions.vault_id`）
  + 目录清单库名列（`inventory_objects.vault_name`）+ 改挂回执（`vault_moves`）同刷；
- 两侧占用页与总览在册徽章只从分区册 `status='在册'` 实时聚合，无独立计数，
  甲库减一、乙库加一随所属行移动自动闭合，甲未减乙已加/双在册即缺陷；
- 任何一步失败整体回滚，不存在半挂状态；全写路径共用同一串行点，
  并发改挂与登记不会双在册、不会双计；
- `request_id` 为幂等键，同键重放只回放原回执，占用变迁只发生一次。

回归测试：`cd backend && PYTHONPATH=. python tests/test_vault_move.py`
