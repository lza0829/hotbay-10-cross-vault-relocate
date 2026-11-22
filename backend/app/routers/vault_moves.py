from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import vault_move as svc

router = APIRouter(tags=["跨库改挂"])


class VaultMoveIn(BaseModel):
    partition_id: int
    to_vault_id: int
    # 幂等键：同键重放只返回原回执，占用变迁不会重复发生
    request_id: str | None = Field(default=None, max_length=80)


@router.get("/改挂")
def list_moves():
    return svc.list_moves()


@router.post("/改挂")
def move(body: VaultMoveIn):
    return svc.move_partition(body.partition_id, body.to_vault_id, body.request_id)
