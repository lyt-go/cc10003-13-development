"""同期群分析业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "cohort"
REQUIRED_FIELDS = ["群组编号", "群组名称", "划分事件"]
STATUS_ORDER = ["草稿", "模型校验中", "已启用", "已归档"]
ACTION_RULES = {"提交模型校验": "模型校验中", "启用同期群": "已启用", "归档同期群": "已归档"}
NEGATIVE_ACTIONS = []


class CohortService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("群组编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        # 联合同期群：状态页直接展示与漏斗、联合视图一致的口径版本与窗口来源
        detail = dict(entry)
        funnel_code = entry.get("来源漏斗编号")
        if funnel_code:
            from app.services.joint_retention import joint_service

            for funnel in store.rows("funnel"):
                if funnel.get("漏斗编号") == funnel_code:
                    view = joint_service.get_view(int(funnel["id"]))
                    detail["活跃版本"] = (view or {}).get("口径版本")
                    detail["历史版本"] = (view or {}).get("历史版本", [])
                    break
        return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同期群定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于同期群分析可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"同期群定义已{action}"
