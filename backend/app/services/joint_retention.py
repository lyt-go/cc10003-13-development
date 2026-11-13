"""漏斗留存联合视图领域服务。

数据链路（漏斗 → 同期群 → 留存结果）：
    漏斗定义（步骤事件 + 转化窗口）
        └─ 启用/换版时冻结为不可变口径版本（hash 出口径版本号）
            └─ 以首步事件作为同期群划分事件，生成/复用同期群定义（共同起算点）
                └─ 按 joint_group_fact 群组原始事实评估每个群组
                    ├─ joint_snapshot：按版本冻结的步骤转化率与异常标记
                    └─ joint_retention：第 1/7/30 天等检查点留存结果
                        └─ joint_audit：记录事件来源、窗口来源与版本变迁

关键约束：
- 转化窗口与观察周期共用同一口径版本，联合视图、漏斗详情、同期群状态都引用它；
- 同版本并发/重复提交至多生成一份结果（进程锁 + 幂等键 + 版本去重）；
- 换版后旧快照/旧留存原样保留，仅受影响群组重算，未受影响群组显式“续算沿用”；
- 缺少中间步骤、观察周期越界、群组粒度为空一律显式标记，对应单元格置空，
  绝不用上一个完整结果冒充。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

from app.store import store

FACT_TABLE = "joint_group_fact"
SNAPSHOT_TABLE = "joint_snapshot"
RETENTION_TABLE = "joint_retention"
AUDIT_TABLE = "joint_audit"

FUNNEL_TABLE = "funnel"
COHORT_TABLE = "cohort"

# 四类数据质量标记
FLAG_EMPTY_GRANULARITY = "群组粒度为空"
FLAG_MISSING_STEP = "缺少中间步骤"
FLAG_SAMPLE_INSUFFICIENT = "样本不足"
FLAG_WINDOW_OUT_OF_RANGE = "观察周期越界"
FLAG_DATA_MISSING = "观察数据缺失"

RETENTION_CHECKPOINTS = (1, 7, 30)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if value in (None, ""):
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(value, digits)


class JointRetentionService:
    """漏斗留存联合视图的全部业务规则都收敛在这个服务里，路由层只做转发。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bootstrapped = False

    # ------------------------------------------------------------------
    # 启动引导：为种子里已是“已启用”的漏斗补齐联合快照（幂等）
    # ------------------------------------------------------------------
    def bootstrap(self) -> dict[str, Any]:
        with self._lock:
            if self._bootstrapped:
                return {"bootstrapped": False}
            generated: list[str] = []
            for funnel in store.rows(FUNNEL_TABLE):
                if funnel.get("status") != "已启用":
                    continue
                if self._active_snapshots(str(funnel["漏斗编号"])):
                    continue
                result = self._generate_locked(int(funnel["id"]), trigger="启动引导")
                if result.get("generated"):
                    generated.append(str(funnel["漏斗编号"]))
            self._bootstrapped = True
            return {"bootstrapped": True, "funnels": generated}

    def _ensure_bootstrapped(self) -> None:
        if not self._bootstrapped:
            self.bootstrap()

    # ------------------------------------------------------------------
    # 对外动作：启用漏斗时由 FunnelService 调用
    # ------------------------------------------------------------------
    def on_funnel_enabled(self, funnel_id: int) -> dict[str, Any]:
        self._ensure_bootstrapped()
        with self._lock:
            return self._generate_locked(funnel_id, trigger="启用漏斗")

    def generate(self, funnel_id: int, idem_key: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """手动触发生成；同版本重复提交直接返回已有结果，不新建第二份。"""
        self._ensure_bootstrapped()
        with self._lock:
            funnel = store.find(FUNNEL_TABLE, funnel_id)
            if funnel is None:
                return None, f"漏斗定义 {funnel_id} 不存在或已归档"
            result = self._generate_locked(funnel_id, trigger="手动生成", idem_key=idem_key)
            return result, result["message"]

    def revise(
        self,
        funnel_id: int,
        values: dict[str, Any],
        idem_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """漏斗步骤换版：旧结果保留，受影响群组重算，其余群组续算沿用。"""
        self._ensure_bootstrapped()
        with self._lock:
            funnel = store.find(FUNNEL_TABLE, funnel_id)
            if funnel is None:
                return None, f"漏斗定义 {funnel_id} 不存在或已归档"
            if funnel.get("status") == "已归档":
                return None, "漏斗已归档，不能再换版，请先复制为新漏斗"
            return self._revise_locked(funnel, values, idem_key)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_views(self, *, funnel_code: str | None = None) -> list[dict[str, Any]]:
        self._ensure_bootstrapped()
        views: list[dict[str, Any]] = []
        funnels = store.rows(FUNNEL_TABLE)
        for funnel in funnels:
            code = str(funnel["漏斗编号"])
            if funnel_code and code != funnel_code:
                continue
            snapshots = self._active_snapshots(code)
            cohort = self._find_cohort(code)
            views.append(self._assemble_view(funnel, cohort, snapshots))
        return views

    def get_view(self, funnel_id: int) -> dict[str, Any] | None:
        self._ensure_bootstrapped()
        funnel = store.find(FUNNEL_TABLE, funnel_id)
        if funnel is None:
            return None
        code = str(funnel["漏斗编号"])
        return self._assemble_view(funnel, self._find_cohort(code), self._active_snapshots(code))

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        self._ensure_bootstrapped()
        snapshot = store.find(SNAPSHOT_TABLE, snapshot_id)
        if snapshot is None:
            return None
        retentions = [
            dict(row)
            for row in store.rows(RETENTION_TABLE)
            if int(row.get("快照id", 0)) == snapshot_id
        ]
        detail = dict(snapshot)
        detail["留存结果"] = sorted(retentions, key=lambda row: _to_int(row.get("检查点天")))
        return detail

    def list_audits(self, funnel_id: int) -> list[dict[str, Any]]:
        self._ensure_bootstrapped()
        funnel = store.find(FUNNEL_TABLE, funnel_id)
        if funnel is None:
            return []
        code = str(funnel["漏斗编号"])
        rows = [dict(row) for row in store.rows(AUDIT_TABLE) if row.get("漏斗编号") == code]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    # ==================================================================
    # 内部：版本与口径
    # ==================================================================
    def _material_signature(self, funnel: dict[str, Any]) -> dict[str, Any]:
        return {
            "events": _to_list(funnel.get("事件定义")),
            "window": _to_int(funnel.get("转化窗口天")),
            "observe": _to_int(funnel.get("观察周期天")),
            "checkpoints": sorted(_to_int(day) for day in _to_list(funnel.get("留存检查点天"))),
            "min_sample": _to_int(funnel.get("最小样本量")),
        }

    def _version_hash(self, funnel: dict[str, Any]) -> str:
        payload = json.dumps(
            self._material_signature(funnel), ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]

    def _version_id(self, funnel: dict[str, Any]) -> str:
        return f"{funnel['漏斗编号']}@v{_to_int(funnel.get('版本序号'), 1)}+{self._version_hash(funnel)}"

    def _next_id(self, table: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1

    def _active_snapshots(self, funnel_code: str) -> list[dict[str, Any]]:
        active_version = None
        for funnel in store.rows(FUNNEL_TABLE):
            if str(funnel.get("漏斗编号")) == funnel_code:
                active_version = funnel.get("活跃版本")
                break
        rows = store.rows(SNAPSHOT_TABLE)
        if active_version:
            snapshots = [row for row in rows if row.get("口径版本") == active_version]
        else:
            # 兼容尚未回写活跃版本的场景：取该漏斗最新版本下的快照
            codes = {row.get("口径版本") for row in rows if row.get("漏斗编号") == funnel_code}
            latest = sorted(codes, key=str)[-1] if codes else None
            snapshots = [row for row in rows if row.get("口径版本") == latest] if latest else []
        return sorted(snapshots, key=lambda row: str(row.get("群组键")))

    def _find_cohort(self, funnel_code: str) -> dict[str, Any] | None:
        for row in store.rows(COHORT_TABLE):
            if row.get("来源漏斗编号") == funnel_code:
                return row
        return None

    def _next_cohort_code(self) -> str:
        biggest = 0
        for row in store.rows(COHORT_TABLE):
            code = str(row.get("群组编号", ""))
            if code.startswith("COHO-"):
                biggest = max(biggest, _to_int(code.split("-", 1)[1]))
        return f"COHO-{biggest + 1:04d}"

    def _ensure_cohort(self, funnel: dict[str, Any], version_id: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
        """生成或复用同期群：划分事件锁定漏斗首步事件，窗口与漏斗共用同一版本。"""
        code = str(funnel["漏斗编号"])
        events = _to_list(funnel.get("事件定义"))
        granularity = next((str(fact.get("群组粒度") or "") for fact in facts if fact.get("群组粒度")), "")
        cohort = self._find_cohort(code)
        common = {
            "划分事件": events[0] if events else "",
            "观察指标": funnel.get("观察指标") or "活跃留存",
            "观察周期天": _to_int(funnel.get("观察周期天")),
            "群组粒度": granularity or "未指定",
            "留存阈值": funnel.get("留存阈值", 0.3),
            "来源漏斗编号": code,
            "口径版本": version_id,
            "转化窗口天": _to_int(funnel.get("转化窗口天")),
            "最后计算时间": _now()[:10],
        }
        if cohort is None:
            cohort = {
                "id": self._next_id(COHORT_TABLE),
                "status": "已启用",
                "pending": False,
                "abnormal": False,
                "群组编号": self._next_cohort_code(),
                "群组名称": f"{code} 首步事件同期群",
                "联合状态": "已联合生成",
                **common,
            }
            store.rows(COHORT_TABLE).append(cohort)
        else:
            cohort.update(common)
            cohort["status"] = "已启用"
            cohort["pending"] = False
            cohort["联合状态"] = "已联合生成"
        return cohort

    # ==================================================================
    # 内部：联合生成
    # ==================================================================
    def _generate_locked(
        self,
        funnel_id: int,
        *,
        trigger: str,
        idem_key: str | None = None,
    ) -> dict[str, Any]:
        funnel = store.find(FUNNEL_TABLE, funnel_id)
        if funnel is None:
            return {"generated": False, "message": f"漏斗定义 {funnel_id} 不存在或已归档"}

        code = str(funnel["漏斗编号"])
        version_id = self._version_id(funnel)
        facts = [row for row in store.rows(FACT_TABLE) if row.get("漏斗编号") == code]
        effective_idem = idem_key or f"generate:{code}:{version_id}"

        # 幂等闸门：同版本已有活跃结果，重复/并发提交至多一份
        existing = self._active_snapshots(code)
        if existing and all(row.get("口径版本") == version_id for row in existing):
            self._write_audit(
                funnel, version_id, action="重复提交拦截",
                detail=f"口径版本 {version_id} 已存在 {len(existing)} 份群组快照，重复提交不再新建结果",
                snapshots=existing, trigger=trigger, idem_key=effective_idem,
            )
            return {
                "generated": False,
                "deduplicated": True,
                "message": f"口径版本 {version_id} 的联合结果已存在，本次提交已去重",
                "口径版本": version_id,
                "快照ids": [row["id"] for row in existing],
            }

        events_sanity = _to_list(funnel.get("事件定义"))
        if not events_sanity:
            return {"generated": False, "message": "漏斗步骤事件定义为空，无法生成同期群与留存结果"}
        if not facts:
            return {"generated": False, "message": f"漏斗 {code} 缺少群组原始事实（joint_group_fact），无法计算"}

        cohort = self._ensure_cohort(funnel, version_id, facts)
        snapshot_rows: list[dict[str, Any]] = []
        for fact in facts:
            snapshot, retentions = self._evaluate_group(funnel, fact, cohort, version_id, recompute=True)
            store.rows(SNAPSHOT_TABLE).append(snapshot)
            for retention in retentions:
                store.rows(RETENTION_TABLE).append(retention)
            snapshot_rows.append(snapshot)

        funnel["活跃版本"] = version_id
        funnel["口径版本"] = f"{code}@v{_to_int(funnel.get('版本序号'), 1)}"
        funnel["联合群组数"] = len(snapshot_rows)
        funnel["最后联合时间"] = _now()

        self._write_audit(
            funnel, version_id, action="联合生成",
            detail=f"{trigger}：按 {len(events_sanity)} 个步骤事件生成 {len(snapshot_rows)} 个群组快照，"
                   f"其中异常标记 {sum(1 for row in snapshot_rows if row['异常标记'])} 个",
            snapshots=snapshot_rows, trigger=trigger, idem_key=effective_idem,
            cohort=cohort,
        )
        return {
            "generated": True,
            "deduplicated": False,
            "message": f"已生成口径版本 {version_id} 的联合留存结果，共 {len(snapshot_rows)} 个群组",
            "口径版本": version_id,
            "同期群编号": cohort["群组编号"],
            "快照ids": [row["id"] for row in snapshot_rows],
        }

    # ==================================================================
    # 内部：换版重算
    # ==================================================================
    def _revise_locked(
        self,
        funnel: dict[str, Any],
        values: dict[str, Any],
        idem_key: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        code = str(funnel["漏斗编号"])
        old_hash = self._version_hash(funnel)
        old_version = funnel.get("活跃版本") or self._version_id(funnel)

        # 仅接受白名单字段，避免动作参数污染定义
        updatable = {
            "步骤名称列表": _to_list(values.get("步骤名称列表")),
            "事件定义": _to_list(values.get("事件定义")),
            "转化窗口天": values.get("转化窗口天"),
            "观察周期天": values.get("观察周期天"),
            "留存检查点天": _to_list(values.get("留存检查点天")),
            "最小样本量": values.get("最小样本量"),
            "漏斗名称": values.get("漏斗名称"),
        }
        for key, value in updatable.items():
            if value in (None, "", []):
                continue
            funnel[key] = value
        if updatable["步骤名称列表"]:
            funnel["步骤序列"] = " > ".join(
                f"步骤{i + 1}:{name}" for i, name in enumerate(updatable["步骤名称列表"])
            )

        new_hash = self._version_hash(funnel)
        effective_idem = idem_key or f"revise:{code}:{new_hash}"
        if new_hash == old_hash:
            self._write_audit(
                funnel, old_version, action="换版校验",
                detail="步骤事件、转化窗口、观察周期均未变化，不产生新版本，沿用当前活跃结果",
                snapshots=self._active_snapshots(code), trigger="换版提交", idem_key=effective_idem,
            )
            view = self.get_view(int(funnel["id"]))
            return view, "口径未发生实质变化，未新建版本，继续沿用当前联合结果"

        # 版本号递增，旧快照/旧留存保持原样不动
        funnel["版本序号"] = _to_int(funnel.get("版本序号"), 1) + 1
        new_version = self._version_id(funnel)
        facts = [row for row in store.rows(FACT_TABLE) if row.get("漏斗编号") == code]
        cohort = self._ensure_cohort(funnel, new_version, facts)
        cohort["联合状态"] = "换版重算中"

        old_snapshots = self._active_snapshots(code)
        old_by_group = {str(row["群组键"]): row for row in old_snapshots}
        new_snapshots: list[dict[str, Any]] = []
        affected_keys: list[str] = []
        carried_keys: list[str] = []

        for fact in facts:
            group_key = str(fact["群组键"])
            previous = old_by_group.get(group_key)
            # 续算边界：群组依赖的步骤事件、转化窗口、观察周期任一变化才重算
            affected = self._is_group_affected(previous, funnel)
            if affected or previous is None:
                snapshot, retentions = self._evaluate_group(funnel, fact, cohort, new_version, recompute=True)
                affected_keys.append(group_key)
            else:
                snapshot, retentions = self._carry_over(previous, funnel, new_version)
                carried_keys.append(group_key)
            store.rows(SNAPSHOT_TABLE).append(snapshot)
            for retention in retentions:
                store.rows(RETENTION_TABLE).append(retention)
            new_snapshots.append(snapshot)

        funnel["活跃版本"] = new_version
        funnel["口径版本"] = f"{code}@v{_to_int(funnel.get('版本序号'), 1)}"
        funnel["联合群组数"] = len(new_snapshots)
        funnel["最后联合时间"] = _now()
        cohort["联合状态"] = "已联合生成"

        self._write_audit(
            funnel, new_version, action="步骤换版",
            detail=(
                f"旧版本 {old_version} 的结果全部保留；受影响群组 {affected_keys or '无'} 已重算，"
                f"未受影响群组 {carried_keys or '无'} 显式续算沿用"
            ),
            snapshots=new_snapshots, trigger="换版提交", idem_key=effective_idem,
            cohort=cohort, old_version=old_version,
        )
        view = self._assemble_view(funnel, cohort, new_snapshots)
        return view, (
            f"已生成新版本 {new_version}：重算 {len(affected_keys)} 个群组，"
            f"续算沿用 {len(carried_keys)} 个群组，旧版本结果保留可审计"
        )

    def _is_group_affected(self, previous: dict[str, Any] | None, funnel: dict[str, Any]) -> bool:
        if previous is None:
            return True
        return (
            _to_list(previous.get("步骤事件")) != _to_list(funnel.get("事件定义"))
            or _to_int(previous.get("转化窗口天")) != _to_int(funnel.get("转化窗口天"))
            or _to_int(previous.get("观察周期天")) != _to_int(funnel.get("观察周期天"))
            or [_to_int(day) for day in _to_list(previous.get("留存检查点天"))]
            != sorted(_to_int(day) for day in _to_list(funnel.get("留存检查点天")))
        )

    def _carry_over(
        self,
        previous: dict[str, Any],
        funnel: dict[str, Any],
        new_version: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """未受影响群组：复制一份指向新版本的快照，显式标注来源，绝不冒充重算。"""
        snapshot = dict(previous)
        old_id = int(previous["id"])
        snapshot["id"] = self._next_id(SNAPSHOT_TABLE)
        snapshot["口径版本"] = new_version
        snapshot["版本hash"] = self._version_hash(funnel)
        snapshot["版本序号"] = _to_int(funnel.get("版本序号"), 1)
        snapshot["重算"] = False
        snapshot["来源快照id"] = old_id
        snapshot["计算状态"] = "续算沿用"
        snapshot["生成时间"] = _now()
        snapshot["事件来源"] = previous.get("事件来源")
        snapshot["窗口来源"] = self._window_source(funnel, new_version)

        retentions: list[dict[str, Any]] = []
        old_retentions = [
            row for row in store.rows(RETENTION_TABLE) if int(row.get("快照id", 0)) == old_id
        ]
        for old in sorted(old_retentions, key=lambda row: _to_int(row.get("检查点天"))):
            row = dict(old)
            row["id"] = self._next_id(RETENTION_TABLE)
            row["快照id"] = snapshot["id"]
            row["口径版本"] = new_version
            row["重算"] = False
            row["来源留存id"] = int(old["id"])
            retentions.append(row)
        return snapshot, retentions

    # ==================================================================
    # 内部：群组评估（转化率 + 留存 + 异常标记）
    # ==================================================================
    def _event_source(self, funnel: dict[str, Any], cohort: dict[str, Any]) -> str:
        return (
            f"漏斗事件定义 {_to_list(funnel.get('事件定义'))}；"
            f"同期群 {cohort['群组编号']} 划分事件「{cohort.get('划分事件')}」（=漏斗首步事件，共同起算点）"
        )

    def _window_source(self, funnel: dict[str, Any], version_id: str) -> str:
        return (
            f"转化窗口 {_to_int(funnel.get('转化窗口天'))} 天、观察周期 "
            f"{_to_int(funnel.get('观察周期天'))} 天，均来自口径版本 {version_id}"
        )

    def _evaluate_group(
        self,
        funnel: dict[str, Any],
        fact: dict[str, Any],
        cohort: dict[str, Any],
        version_id: str,
        *,
        recompute: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        events = _to_list(funnel.get("事件定义"))
        step_names = _to_list(funnel.get("步骤名称列表")) or [f"步骤{i + 1}" for i in range(len(events))]
        window_days = _to_int(funnel.get("转化窗口天"))
        observe_days = _to_int(funnel.get("观察周期天"))
        checkpoints = sorted(_to_int(day) for day in _to_list(funnel.get("留存检查点天")))
        min_sample = _to_int(funnel.get("最小样本量"))
        sample = _to_int(fact.get("样本量"))
        reach = [_to_int(step) if step is not None else None for step in _to_list(fact.get("步骤触达"))]
        granularity = str(fact.get("群组粒度") or "")

        flags: list[str] = []
        if not granularity.strip():
            flags.append(FLAG_EMPTY_GRANULARITY)

        missing_indexes = [
            i for i in range(len(events))
            if i >= len(reach) or reach[i] is None
        ]
        if missing_indexes:
            names = "、".join(step_names[i] for i in missing_indexes if i < len(step_names))
            flags.append(f"{FLAG_MISSING_STEP}（{names or '未命名步骤'}）")

        insufficient = sample < min_sample
        if insufficient:
            flags.append(f"{FLAG_SAMPLE_INSUFFICIENT}（样本 {sample} < 最小 {min_sample}）")

        # 步骤转化率：缺步骤及其下游一律置空，不用其他数据补
        step_rates: list[float | None] = []
        for i in range(len(events)):
            if i in missing_indexes or (i > 0 and (i - 1) in missing_indexes):
                step_rates.append(None)
                continue
            base = sample if i == 0 else reach[i - 1]
            if not base or reach[i] is None:
                step_rates.append(None)
            else:
                step_rates.append(_round(reach[i] / base))
        overall = None
        if reach and reach[-1] is not None and sample and not missing_indexes:
            overall = _round(reach[-1] / sample)

        # 粒度为空时分组不可信：保留原始人数，比率单元格全部置空并写明原因
        if FLAG_EMPTY_GRANULARITY in flags:
            step_rates = [None] * len(events)
            overall = None

        snapshot_id = self._next_id(SNAPSHOT_TABLE)
        snapshot = {
            "id": snapshot_id,
            "漏斗编号": funnel["漏斗编号"],
            "群组编号": cohort["群组编号"],
            "群组键": fact["群组键"],
            "群组标签": fact.get("群组标签") or fact["群组键"],
            "群组粒度": granularity or None,
            "锚点日期": fact.get("锚点日期"),
            "口径版本": version_id,
            "版本hash": version_id.split("+", 1)[-1],
            "版本序号": _to_int(funnel.get("版本序号"), 1),
            "步骤名称": step_names,
            "步骤事件": events,
            "转化窗口天": window_days,
            "观察周期天": observe_days,
            "留存检查点天": checkpoints,
            "样本量": sample,
            "步骤触达": reach,
            "步骤转化率": step_rates,
            "整体转化率": overall,
            "关联指标": funnel.get("关联指标"),
            "观察指标": funnel.get("观察指标"),
            "最小样本量": min_sample,
            "异常标记": flags,
            "计算状态": "异常标记：" + "、".join(flags) if flags else "完整",
            "重算": recompute,
            "来源快照id": None,
            "生成时间": _now(),
            "事件来源": self._event_source(funnel, cohort),
            "窗口来源": self._window_source(funnel, version_id),
            "群组事实id": fact.get("id"),
        }

        retained_map = fact.get("留存人数") or {}
        retentions: list[dict[str, Any]] = []
        for day in checkpoints:
            status = "正常"
            reason = ""
            rate: float | None = None
            retained = retained_map.get(day)
            if day > observe_days:
                status = FLAG_WINDOW_OUT_OF_RANGE
                reason = f"检查点第 {day} 天超出观察周期 {observe_days} 天，结果置空，不沿用历史完整值"
            elif FLAG_EMPTY_GRANULARITY in flags:
                status = FLAG_EMPTY_GRANULARITY
                reason = "群组粒度为空，分组口径不可信，留存率置空"
            elif retained is None:
                status = FLAG_DATA_MISSING
                reason = f"第 {day} 天留存人数缺失，结果置空"
            elif sample <= 0:
                status = FLAG_DATA_MISSING
                reason = "样本量为 0，无法计算留存率"
            else:
                rate = _round(_to_int(retained) / sample)
                if insufficient:
                    status = FLAG_SAMPLE_INSUFFICIENT
                    reason = f"样本 {sample} 低于最小样本量 {min_sample}，数值仅供参考"
            retentions.append({
                "id": self._next_id(RETENTION_TABLE),
                "快照id": snapshot_id,
                "漏斗编号": funnel["漏斗编号"],
                "群组键": fact["群组键"],
                "口径版本": version_id,
                "检查点天": day,
                "留存人数": retained,
                "样本量": sample,
                "留存率": rate,
                "状态": status,
                "原因": reason,
                "重算": recompute,
                "来源留存id": None,
            })
        return snapshot, retentions

    # ==================================================================
    # 内部：审计与视图组装
    # ==================================================================
    def _write_audit(
        self,
        funnel: dict[str, Any],
        version_id: str,
        *,
        action: str,
        detail: str,
        snapshots: list[dict[str, Any]],
        trigger: str,
        idem_key: str,
        cohort: dict[str, Any] | None = None,
        old_version: str | None = None,
    ) -> dict[str, Any]:
        audit = {
            "id": self._next_id(AUDIT_TABLE),
            "时间": _now(),
            "漏斗编号": funnel["漏斗编号"],
            "口径版本": version_id,
            "旧口径版本": old_version,
            "动作": action,
            "触发方式": trigger,
            "幂等键": idem_key,
            "事件来源": self._event_source(funnel, cohort) if cohort else f"漏斗事件定义 {_to_list(funnel.get('事件定义'))}",
            "窗口来源": self._window_source(funnel, version_id),
            "详情": detail,
            "结果快照": [row["id"] for row in snapshots],
        }
        store.rows(AUDIT_TABLE).append(audit)
        return audit

    def _assemble_view(
        self,
        funnel: dict[str, Any],
        cohort: dict[str, Any] | None,
        snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        groups: list[dict[str, Any]] = []
        overall_rates: list[float] = []
        for snapshot in snapshots:
            retentions = sorted(
                (
                    dict(row)
                    for row in store.rows(RETENTION_TABLE)
                    if int(row.get("快照id", 0)) == int(snapshot["id"])
                ),
                key=lambda row: _to_int(row.get("检查点天")),
            )
            group = dict(snapshot)
            group["留存结果"] = retentions
            groups.append(group)
            if snapshot.get("整体转化率") is not None:
                overall_rates.append(float(snapshot["整体转化率"]))

        abnormal = sum(1 for row in snapshots if row.get("异常标记"))
        insufficient = sum(
            1 for row in snapshots
            if any(FLAG_SAMPLE_INSUFFICIENT in str(flag) for flag in row.get("异常标记", []))
        )
        history_versions = sorted(
            {row.get("口径版本") for row in store.rows(SNAPSHOT_TABLE)
             if row.get("漏斗编号") == funnel.get("漏斗编号")}
        )
        return {
            "漏斗": dict(funnel),
            "口径版本": funnel.get("活跃版本"),
            "历史版本": history_versions,
            "同期群": dict(cohort) if cohort else None,
            "群组结果": groups,
            "汇总": {
                "群组数": len(snapshots),
                "完整群组": len(snapshots) - abnormal,
                "异常群组": abnormal,
                "样本不足群组": insufficient,
                "平均整体转化率": _round(sum(overall_rates) / len(overall_rates)) if overall_rates else None,
            },
        }


joint_service = JointRetentionService()
