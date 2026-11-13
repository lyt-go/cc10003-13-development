"""漏斗留存联合视图接口。

以漏斗为联合业务入口：转化窗口与留存观察周期共用同一口径版本，
对外提供联合视图、群组快照明细、换版重算、手动生成与审计查询。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services.joint_retention import joint_service

router = APIRouter(prefix="/api/funnel-retention", tags=["漏斗留存联合视图"])


@router.get("")
def list_views(
    funnel_code: str | None = Query(default=None, description="按漏斗编号过滤，如 FUNN-0003"),
) -> dict:
    """联合视图：每个漏斗一行，含同版本的转化率、留存结果与异常层级汇总。"""
    return {"items": joint_service.list_views(funnel_code=funnel_code)}


@router.get("/funnels/{funnel_id}")
def get_view(funnel_id: int) -> dict:
    view = joint_service.get_view(funnel_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"漏斗定义 {funnel_id} 不存在或已归档")
    return view


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int) -> dict:
    snapshot = joint_service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"群组快照 {snapshot_id} 不存在")
    return snapshot


@router.post("/funnels/{funnel_id}/generate", response_model=ActionResult)
def generate(funnel_id: int, payload: EntryPayload) -> ActionResult:
    """同版本手动重复生成：幂等去重，至多保留一份结果。"""
    idem_key = payload.values.get("idem_key")
    result, message = joint_service.generate(funnel_id, idem_key=idem_key)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.post("/funnels/{funnel_id}/revise", response_model=ActionResult)
def revise(funnel_id: int, payload: EntryPayload) -> ActionResult:
    """漏斗步骤换版：旧留存结果保留，受影响群组重算，未受影响群组续算沿用。"""
    idem_key = payload.values.pop("idem_key", None)
    view, message = joint_service.revise(funnel_id, payload.values, idem_key=idem_key)
    if view is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=view)


@router.get("/funnels/{funnel_id}/audits")
def list_audits(funnel_id: int) -> dict:
    """审计：每次生成/换版/去重都可追溯事件来源与窗口来源。"""
    return {"items": joint_service.list_audits(funnel_id)}
