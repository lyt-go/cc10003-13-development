"""联合视图 HTTP 端到端验证：启用动作、幂等、换版、审计、三处同版本引用。"""
from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    # 1. 联合视图列表：FUNN-0003 启动即生成，含 1/7/30 留存
    views = call("GET", "/api/funnel-retention")["items"]
    by_code = {v["漏斗"]["漏斗编号"]: v for v in views}
    v3 = by_code["FUNN-0003"]
    version = v3["口径版本"]
    assert v3["同期群"]["口径版本"] == version
    days = [r["检查点天"] for r in v3["群组结果"][0]["留存结果"]]
    assert days == [1, 7, 30], days
    print("[1] 联合视图 FUNN-0003 版本", version, "检查点", days,
          "汇总", v3["汇总"])

    # 2. 同版本重复提交：去重
    r1 = call("POST", "/api/funnel-retention/funnels/3/generate", {"values": {}})
    assert r1["ok"] and r1["entry"]["deduplicated"], r1
    print("[2] 重复生成被去重:", r1["message"])

    # 3. 启用动作联动：启用 FUNN-0005（草稿→已启用），自动生成联合结果
    r2 = call("POST", "/api/funnel/4/actions", {"values": {"action": "启用漏斗"}})
    assert r2["ok"], r2
    assert "联合" in r2["message"], r2["message"]
    v5 = call("GET", "/api/funnel-retention/funnels/4")
    flags = [f for g in v5["群组结果"] for f in g["异常标记"]]
    assert any("缺少中间步骤" in f for f in flags), flags
    out_of_range = [
        r for g in v5["群组结果"] for r in g["留存结果"]
        if r["状态"] == "观察周期越界"
    ]
    assert out_of_range and all(r["留存率"] is None for r in out_of_range)
    print("[3] 启用漏斗自动联动生成，异常:", flags, "越界单元格数:", len(out_of_range))

    # 4. 换版：改转化窗口，旧版本保留
    r3 = call("POST", "/api/funnel-retention/funnels/3/revise",
              {"values": {"转化窗口天": 10}})
    assert r3["ok"], r3
    new_version = r3["entry"]["口径版本"]
    assert new_version != version
    assert version in r3["entry"]["历史版本"]
    print("[4] 换版:", r3["message"], "历史版本:", r3["entry"]["历史版本"])

    # 5. 漏斗详情 / 同期群状态 / 联合视图三处版本一致
    funnel_detail = call("GET", "/api/funnel/3")
    cohort_detail = call("GET", "/api/cohort/2")
    joint = call("GET", "/api/funnel-retention/funnels/3")
    assert funnel_detail["活跃版本"] == cohort_detail["活跃版本"] == joint["口径版本"]
    print("[5] 三处同版本:", funnel_detail["活跃版本"])

    # 6. 审计可看到事件与窗口来源
    audits = call("GET", "/api/funnel-retention/funnels/3/audits")["items"]
    for a in audits[:3]:
        assert "evt_register" in a["事件来源"] and "窗口" in a["窗口来源"]
    print("[6] 审计条数:", len(audits), "最新动作:", audits[0]["动作"],
          "|", audits[0]["窗口来源"])

    # 7. 快照明细
    snap_id = joint["群组结果"][0]["id"]
    snap = call("GET", f"/api/funnel-retention/snapshots/{snap_id}")
    assert snap["事件来源"] and snap["窗口来源"] and snap["留存结果"]
    print("[7] 快照", snap_id, snap["群组标签"], snap["计算状态"], "留存行数", len(snap["留存结果"]))

    print("\nHTTP 端到端全部通过 ✅")


if __name__ == "__main__":
    main()
