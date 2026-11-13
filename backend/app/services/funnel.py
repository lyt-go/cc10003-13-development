"""漏斗分析业务规则：状态流转、字段校验与筛选口径都收在这里。

漏斗是“漏斗留存联合视图”的业务入口：执行「启用漏斗」时，会把步骤事件与转化窗口
冻结成口径版本，并联动 JointRetentionService 生成按步骤事件划分的同期群留存结果。
"""
from __future__ import annotations

from typing import Any

from app.services.joint_retention import joint_service
from app.store import store

MODULE = "funnel"
REQUIRED_FIELDS = ["漏斗编号", "漏斗名称", "步骤序列"]
STATUS_ORDER = ["草稿", "模型校验中", "已启用", "已归档"]
ACTION_RULES = {"提交模型校验": "模型校验中", "启用漏斗": "已启用", "归档漏斗": "已归档"}
NEGATIVE_ACTIONS = []


class FunnelService:
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
            rows = [row for row in rows if keyword in str(row.get("漏斗编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [dict(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        # 漏斗详情引用与联合视图、同期群完全一致的口径版本
        joint = joint_service.get_view(entry_id)
        detail = dict(entry)
        if joint is not None:
            detail["活跃版本"] = joint.get("口径版本")
            detail["历史版本"] = joint.get("历史版本", [])
            detail["联合同期群"] = (joint.get("同期群") or {}).get("群组编号")
            detail["联合汇总"] = joint.get("汇总")
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
            return None, f"漏斗定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于漏斗分析可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS

        joint_message = ""
        if action == "启用漏斗":
            # 联合入口：启用即冻结口径版本并生成同期群留存；并发/重复提交在服务内去重
            result = joint_service.on_funnel_enabled(entry_id)
            if result.get("generated"):
                joint_message = f"；{result['message']}（同期群 {result.get('同期群编号', '')}）"
            elif result.get("deduplicated"):
                joint_message = f"；{result['message']}"
            else:
                joint_message = f"；联合结果未生成：{result.get('message', '原因未知')}"
        return entry, f"漏斗定义已{action}{joint_message}"
