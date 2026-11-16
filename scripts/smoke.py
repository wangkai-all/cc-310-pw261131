#!/usr/bin/env python3
"""端到端冒烟测试:数据生成、回测、决策时点约束、版本恢复、报告。

运行:python3 scripts/smoke.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from churnwatch import backtest  # noqa: E402
from churnwatch.db import connect, init_db  # noqa: E402
from churnwatch.seed import seed  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "smoke_test.db")
D = "2026-08-01"


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok  {name}")


def main():
    if os.path.exists(DB):
        os.remove(DB)
    conn = connect(DB)
    init_db(conn)
    seed(conn, n_customers=240, verbose=False)

    print("[1] 基础数据")
    for t in ("customers", "activities", "service_records", "alerts_history",
              "interventions", "churn", "identity_corrections"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        check(f"{t} 非空({n})", n > 0)

    print("[2] 回测结构")
    res = backtest.run_backtest(conn, {
        "rule_version": "v2", "threshold": 0.5, "decision_date": D,
        "horizon_days": 60, "grace_days": 45,
        "methods": ["naive_all", "no_intervention", "time_holdout", "stratified"],
    })
    check("队列非空", res["cohort_size"] > 100)
    m = res["methods"]
    check("四种方法齐全", all(k in m for k in
          ("naive_all", "no_intervention", "time_holdout", "stratified")))
    check("指标在 [0,1]", 0 <= m["no_intervention"]["precision"] <= 1
          and 0 <= m["no_intervention"]["recall"] <= 1)
    check("时间留出含校准期与留出期",
          m["time_holdout"]["calibration"]["period"]["decision_date"] < D)
    check("偏差说明非空", len(res["bias_notes"]) >= 3)
    check("存在被干预的预警客户(干预污染场景)",
          res["counts"]["intervened_alerted"] > 0)

    print("[3] 决策时点约束:未来入库的记录不影响分数")
    cid = conn.execute("SELECT customer_id FROM customers LIMIT 1").fetchone()["customer_id"]
    f0 = backtest.compute_features(conn, D).get(cid, {})
    conn.execute(
        "INSERT INTO service_records(customer_id, event_time, channel, issue_type, severity, ingested_at)"
        " VALUES(?,?,?,?,?,?)", (cid, "2026-07-28", "phone", "billing", 3, "2026-08-20"))
    conn.commit()
    f1 = backtest.compute_features(conn, D).get(cid, {})
    check("ingested_at 晚于决策日的工单不改变特征",
          f0.get("tickets_90", 0) == f1.get("tickets_90", 0))
    conn.execute(
        "INSERT INTO service_records(customer_id, event_time, channel, issue_type, severity, ingested_at)"
        " VALUES(?,?,?,?,?,?)", (cid, "2026-07-29", "phone", "billing", 3, "2026-07-30"))
    conn.commit()
    f2 = backtest.compute_features(conn, D).get(cid, {})
    check("决策日前已入库的工单改变特征",
          f2.get("tickets_90", 0) == f1.get("tickets_90", 0) + 1)

    print("[4] 结果迟到:宽限期越短删失越多")
    r0 = backtest.run_backtest(conn, {"decision_date": D, "grace_days": 0,
                                      "methods": ["naive_all"]})
    r120 = backtest.run_backtest(conn, {"decision_date": D, "grace_days": 120,
                                        "methods": ["naive_all"]})
    check("grace=0 的删失数 >= grace=120",
          r0["counts"]["censored_late_outcome"] >= r120["counts"]["censored_late_outcome"])

    print("[5] 版本与恢复")
    meta1 = backtest.save_run(conn, res["config"], res)
    re = backtest.recompute(conn, meta1["run_id"])
    check("重算产生同谱系新版本",
          re["meta"]["version"] == 2 and re["meta"]["lineage_id"] == meta1["lineage_id"])
    old = backtest.get_run(conn, meta1["run_id"])
    check("旧版本可恢复且结果不变",
          old["results"]["cohort_size"] == res["cohort_size"])

    print("[6] 报告快照")
    rep = backtest.save_report(conn, "冒烟报告", meta1["run_id"], "自动测试")
    snap = backtest.get_report(conn, rep["report_id"])
    check("报告快照可恢复", snap["snapshot"]["results"]["cohort_size"] == res["cohort_size"])

    print("[7] 非法输入")
    try:
        backtest.run_backtest(conn, {"rule_version": "v9"})
        check("未知规则版本报错", False)
    except ValueError:
        check("未知规则版本报错", True)

    conn.close()
    os.remove(DB)
    print("\nSMOKE PASS")


if __name__ == "__main__":
    main()
