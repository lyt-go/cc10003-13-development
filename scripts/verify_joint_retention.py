"""漏斗留存联合视图服务层验证脚本（不依赖网络，直接打领域服务）。"""
from __future__ import annotations

import sys

sys.path.insert(0, "/workspace/backend")

from app.services.joint_retention import joint_service  # noqa: E402
from app.store import store  # noqa: E402


def find_funnel(code: str) -> dict:
    for row in store.rows("funnel"):
        if row["漏斗编号"] == code:
            return row
    raise AssertionError(f"missing funnel {code}")


def main() -> None:
    # 1. 启动引导：已启用的 FUNN-0003 自动生成联合结果
    boot = joint_service.bootstrap()
    assert boot["bootstrapped"] is True, boot
    assert "FUNN-0003" in boot["funnels"], boot
    print("[1] 启动引导:", boot)

    view3 = joint_service.get_view(int(find_funnel("FUNN-0003")["id"]))
    assert view3["口径版本"], "联合视图必须带口径版本"
    version_v1 = view3["口径版本"]
    assert view3["同期群"]["群组编号"] == "COHO-0002", view3["同期群"]
    assert view3["同期群"]["口径版本"] == version_v1, "同期群必须引用同一版本"
    assert view3["同期群"]["划分事件"] == "evt_register", "划分事件必须等于漏斗首步事件"
    assert view3["同期群"]["转化窗口天"] == 7
    assert view3["同期群"]["观察周期天"] == 30
    print("[2] 同版本引用（视图/同期群）:", version_v1)

    groups = {g["群组键"]: g for g in view3["群组结果"]}
    assert set(groups) == {"2026-W40", "2026-W41", "2026-W42", "2026-W43"}

    # 正常群组：步骤转化率（首步锚点=100%，后续按相邻步骤触达比）+ 1/7/30 留存
    g40 = groups["2026-W40"]
    assert g40["计算状态"] == "完整", g40
    assert g40["步骤转化率"] == [1.0, round(86 / 120, 4), round(52 / 86, 4)]
    assert g40["整体转化率"] == round(52 / 120, 4)
    retention = {r["检查点天"]: r for r in g40["留存结果"]}
    assert [r["检查点天"] for r in g40["留存结果"]] == [1, 7, 30]
    assert retention[30]["留存率"] == round(57 / 120, 4)
    assert retention[1]["留存率"] == round(108 / 120, 4)
    print("[3] FUNN-0003 正常群组转化率/留存:", g40["步骤转化率"], retention[30])

    # 样本不足：层级保留并标记，数值标“仅供参考”
    g42 = groups["2026-W42"]
    assert any("样本不足" in flag for flag in g42["异常标记"]), g42["异常标记"]
    r42 = {r["检查点天"]: r for r in g42["留存结果"]}
    assert r42[7]["状态"] == "样本不足", r42[7]
    print("[4] 样本不足层级:", g42["异常标记"], "->", r42[7]["原因"])

    # 群组粒度为空：明确标记且比率单元格置空
    g43 = groups["2026-W43"]
    assert "群组粒度为空" in g43["异常标记"], g43
    assert g43["整体转化率"] is None
    assert g43["步骤转化率"] == [None, None, None]
    assert all(r["留存率"] is None and r["状态"] == "群组粒度为空" for r in g43["留存结果"])
    print("[5] 粒度为空标记且单元格置空:", g43["计算状态"])

    # 审计：事件来源与窗口来源可见
    audits = joint_service.list_audits(int(find_funnel("FUNN-0003")["id"]))
    assert audits and "evt_register" in audits[0]["事件来源"], audits
    assert "转化窗口 7 天" in audits[0]["窗口来源"] and "观察周期 30 天" in audits[0]["窗口来源"]
    print("[6] 审计来源:", audits[0]["事件来源"], "|", audits[0]["窗口来源"])

    # 2. 重复提交同版本：至多一份
    before = len(store.rows("joint_snapshot"))
    dup, msg = joint_service.generate(int(find_funnel("FUNN-0003")["id"]))
    after = len(store.rows("joint_snapshot"))
    assert dup["deduplicated"] is True and before == after, (dup, before, after)
    assert any(a["动作"] == "重复提交拦截" for a in joint_service.list_audits(3))
    print("[7] 同版本重复提交去重:", msg)

    # 3. 并发提交：多线程同时生成同版本，快照数量不增加
    import threading

    results: list = []

    def worker() -> None:
        results.append(joint_service.generate(3)[0])

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.rows("joint_snapshot")) == before, "并发生成必须被串行化去重"
    print("[8] 并发 5 次提交后快照仍为", before, "份")

    # 4. 启用异常样例漏斗 FUNN-0005：缺中间步骤 + 观察周期越界
    r5, msg5 = joint_service.generate(int(find_funnel("FUNN-0005")["id"]))
    assert r5 and r5["generated"], (r5, msg5)
    view5 = joint_service.get_view(int(find_funnel("FUNN-0005")["id"]))
    g5 = {g["群组键"]: g for g in view5["群组结果"]}
    g_missing = g5["2026-W42"]
    assert any("缺少中间步骤" in flag for flag in g_missing["异常标记"]), g_missing
    assert g_missing["步骤转化率"][1] is None and g_missing["步骤转化率"][2] is None
    g_range = g5["2026-W43"]
    r_range = {r["检查点天"]: r for r in g_range["留存结果"]}
    assert r_range[30]["状态"] == "观察周期越界", r_range[30]
    assert r_range[30]["留存率"] is None and "不沿用历史完整值" in r_range[30]["原因"]
    print("[9] FUNN-0005 异常标记:", g_missing["异常标记"], "/", r_range[30])

    # 5. 换版：只改转化窗口，旧结果保留，全部群组受影响重算
    v1_snapshot_count = len([
        s for s in store.rows("joint_snapshot") if s["漏斗编号"] == "FUNN-0003"
    ])
    view_new, msg_new = joint_service.revise(3, {"转化窗口天": 14})
    version_v2 = view_new["口径版本"]
    assert version_v2 != version_v1, (version_v1, version_v2)
    v2_snapshots = [s for s in store.rows("joint_snapshot") if s["口径版本"] == version_v2]
    assert len(v2_snapshots) == 4
    assert all(s["重算"] is True for s in v2_snapshots), "窗口变了，群组必须重算"
    # 旧版本结果原样保留，没有被覆盖
    assert len([s for s in store.rows("joint_snapshot") if s["漏斗编号"] == "FUNN-0003"]) == v1_snapshot_count + 4
    print("[10] 换版重算（窗口 7->14）:", msg_new)

    # 再换版：只改最小样本量 → 版本 hash 变化，但群组依赖的事件/窗口/周期未变 → 续算沿用
    view_carry, msg_carry = joint_service.revise(3, {"最小样本量": 80})
    version_v3 = view_carry["口径版本"]
    assert version_v3 != version_v2
    v3_snapshots = [s for s in store.rows("joint_snapshot") if s["口径版本"] == version_v3]
    assert len(v3_snapshots) == 4
    assert all(s["重算"] is False and s["来源快照id"] for s in v3_snapshots), "未受影响群组必须显式续算沿用"
    assert all(s["计算状态"] == "续算沿用" for s in v3_snapshots)
    # 续算留存行同样指向旧来源，不能冒充重算
    carry_retentions = [
        r for r in store.rows("joint_retention")
        if r["口径版本"] == version_v3
    ]
    assert carry_retentions and all(r["重算"] is False and r["来源留存id"] for r in carry_retentions)
    # 续算快照仍带事件与窗口来源
    assert all("evt_register" in s["事件来源"] and "口径版本" in s["窗口来源"] for s in v3_snapshots)
    print("[11] 未受影响群组续算沿用:", msg_carry)

    # 再换版：事件/窗口/周期都不变（仅名称），hash 不变 → 不产生新版本
    view_same, msg_same = joint_service.revise(3, {"漏斗名称": "注册到首单转化漏斗（改名）"})
    assert view_same["口径版本"] == version_v3, msg_same
    print("[12] 无实质变化不换版:", msg_same)

    # 再换版：窗口改回 7 → 群组依赖再次变化，重新重算
    view_back, msg_back = joint_service.revise(3, {"转化窗口天": 7})
    version_v4 = view_back["口径版本"]
    assert version_v4 != version_v3
    assert all(g["重算"] is True for g in view_back["群组结果"])
    print("[13] 窗口再次变化触发重算:", msg_back, version_v4)

    # 审计链：每个版本都可看到事件与窗口来源
    audit_chain = joint_service.list_audits(3)
    actions = {a["动作"] for a in audit_chain}
    assert {"联合生成", "重复提交拦截", "步骤换版", "换版校验"}.issubset(actions), actions
    for a in audit_chain:
        assert a["事件来源"] and a["窗口来源"] and a["幂等键"], a
    print("[14] 审计链动作:", sorted(actions))

    # 6. 漏斗详情与同期群状态引用同一版本
    from app.services.funnel import FunnelService
    from app.services.cohort import CohortService

    funnel_detail = FunnelService().get_entry(3)
    cohort_detail = CohortService().get_entry(2)
    assert funnel_detail["活跃版本"] == cohort_detail["活跃版本"] == version_v4
    assert funnel_detail["活跃版本"] == joint_service.get_view(3)["口径版本"]
    print("[15] 三处引用同一版本:", funnel_detail["活跃版本"])

    # 7. 快照详情接口结构
    snap = joint_service.get_snapshot(v2_snapshots[0]["id"])
    assert snap["留存结果"] and snap["事件来源"] and snap["窗口来源"]
    print("[16] 群组快照明细字段齐全:", snap["群组键"], snap["计算状态"])

    print("\n全部断言通过 ✅")


if __name__ == "__main__":
    main()
