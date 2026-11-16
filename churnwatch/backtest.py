"""回测引擎。

核心约束(与需求一一对应):
1. 每份回测绑定"决策当时可见"的数据:
   - 特征只用 ingested_at <= 决策日 的活动/服务记录;
   - 身份更正按 corrected_at <= 决策日 的状态生效;
   - 结果(流失)只认 recorded_at <= 证据截止日 的记录,迟到结果按删失处理;
   - 规则按版本显式绑定,历史预警用当时生效的版本(见 seed)。
2. 做过干预的客户后续表现已受干预影响,不能直接给原风险打对错:
   - naive_all 仅作对照展示;no_intervention 近似预测质量;
   - stratified 做分层描述性对照(不声称因果);time_holdout 做时间留出。
3. 所有口径把"无法识别的偏差"写成文字随结果返回。
"""
import json
from datetime import date, timedelta

from .rules import RULES

TODAY = "2026-11-16"

DEFAULTS = {
    "rule_version": "v2",
    "threshold": 0.5,
    "decision_date": "2026-08-01",
    "horizon_days": 60,
    "grace_days": 45,        # 观察窗结束后等待结果录入的宽限期
    "holdout_days": 180,     # 时间留出:校准期决策日 = 决策日 - holdout_days
    "methods": ["naive_all", "no_intervention", "time_holdout", "stratified"],
}

METHOD_LABELS = {
    "naive_all": "朴素全样本(含被干预者,仅作对照)",
    "no_intervention": "未干预样本(近似预测质量)",
    "time_holdout": "时间留出(校准期 vs 留出期)",
    "stratified": "分层对照(干预描述性对比)",
}


def add_days(d, n):
    return (date.fromisoformat(d) + timedelta(days=int(n))).isoformat()


def _days(d1, d2):
    return (date.fromisoformat(d1) - date.fromisoformat(d2)).days


# ---------------------------------------------------------------- 可见性

def visible_id_map(conn, as_of):
    """as_of 时点已生效的身份更正:duplicate_id -> primary_id。"""
    rows = conn.execute(
        "SELECT duplicate_id, primary_id FROM identity_corrections WHERE corrected_at <= ?",
        (as_of,),
    ).fetchall()
    return {r["duplicate_id"]: r["primary_id"] for r in rows}


def compute_features(conn, D, idmap=None):
    """决策日 D 可见的特征。只用 ingested_at <= D 且 event_time <= D 的记录,
    客户 id 按 D 时点的身份更正状态归一。"""
    if idmap is None:
        idmap = visible_id_map(conn, D)
    d30, d60, d90, d180 = (add_days(D, -n) for n in (30, 60, 90, 180))

    def blank():
        return {
            "logins_30": 0.0, "logins_prev30": 0.0,
            "tickets_90": 0, "tickets_30": 0, "tickets_prev60": 0,
            "high_sev_90": 0, "billing_180": 0,
            "last_event": None,
        }

    feats = {}
    for r in conn.execute(
        "SELECT customer_id, event_time, kind, value FROM activities "
        "WHERE ingested_at <= ? AND event_time <= ?", (D, D)
    ):
        cid = idmap.get(r["customer_id"], r["customer_id"])
        f = feats.setdefault(cid, blank())
        et = r["event_time"]
        if r["kind"] == "login":
            if et > d30:
                f["logins_30"] += r["value"]
            elif et > d60:
                f["logins_prev30"] += r["value"]
        if f["last_event"] is None or et > f["last_event"]:
            f["last_event"] = et

    for r in conn.execute(
        "SELECT customer_id, event_time, issue_type, severity FROM service_records "
        "WHERE ingested_at <= ? AND event_time <= ?", (D, D)
    ):
        cid = idmap.get(r["customer_id"], r["customer_id"])
        f = feats.setdefault(cid, blank())
        et = r["event_time"]
        if et > d90:
            f["tickets_90"] += 1
            if r["severity"] >= 3:
                f["high_sev_90"] += 1
        if et > d30:
            f["tickets_30"] += 1
        elif et > d90:
            f["tickets_prev60"] += 1
        if et > d180 and r["issue_type"] == "billing":
            f["billing_180"] += 1
        if f["last_event"] is None or et > f["last_event"]:
            f["last_event"] = et

    for cid, f in feats.items():
        f["days_idle"] = _days(D, f["last_event"]) if f["last_event"] else None
    return feats


# ---------------------------------------------------------------- 单期评估

def evaluate_cohort(conn, rule_version, threshold, D, horizon_days, evidence_cutoff):
    """在决策日 D 用 rule_version+threshold 打分,返回逐客户行与计数。

    返回 rows: [{cid, segment, score, alert, y, excluded, intervened}]
    y: 1=窗内流失(结果已在截止日前录入), 0=未流失, None=排除(删失/状态异常)
    """
    idmap = visible_id_map(conn, D)
    end = add_days(D, horizon_days)
    cutoff = min(evidence_cutoff, TODAY)

    known_churned = {
        r["customer_id"]
        for r in conn.execute("SELECT customer_id FROM churn WHERE recorded_at <= ?", (D,))
    }
    customers = conn.execute(
        "SELECT customer_id, segment, signup_date FROM customers WHERE signup_date <= ?", (D,)
    ).fetchall()

    feats = compute_features(conn, D, idmap)
    rule_fn = RULES[rule_version]["fn"]

    # 观察窗内的干预(用于分组,绝不进入特征)
    intervened = set()
    for r in conn.execute(
        "SELECT customer_id FROM interventions "
        "WHERE action_time > ? AND action_time <= ? AND recorded_at <= ?",
        (D, end, cutoff),
    ):
        intervened.add(idmap.get(r["customer_id"], r["customer_id"]))

    # 结果标签:只认 recorded_at <= cutoff 的流失记录
    label, censored_late, late_state = {}, set(), set()
    for r in conn.execute("SELECT customer_id, churn_date, recorded_at FROM churn"):
        cid, cd, rec = r["customer_id"], r["churn_date"], r["recorded_at"]
        if D < cd <= end:
            if rec <= cutoff:
                label[cid] = 1
            else:
                censored_late.add(cid)      # 结果迟到:窗内流失但截止日后才录入
        elif cd <= D and rec > D:
            late_state.add(cid)             # 决策时看似在册,实际已流失(迟到记录)

    counts = {
        "censored_late_outcome": 0, "late_state": 0, "dup_in_cohort": 0,
        "intervened_total": 0, "intervened_alerted": 0,
    }
    rows = []
    for c in customers:
        cid = c["customer_id"]
        if cid in idmap:            # D 时点已并入他户,不是独立客户
            continue
        if cid in known_churned:    # D 时点已知流失
            continue
        if cid in censored_late:
            counts["censored_late_outcome"] += 1
            rows.append({"cid": cid, "segment": c["segment"], "score": None,
                         "alert": 0, "y": None, "excluded": "结果迟到(删失)", "intervened": 0})
            continue
        if cid in late_state:
            counts["late_state"] += 1
            rows.append({"cid": cid, "segment": c["segment"], "score": None,
                         "alert": 0, "y": None, "excluded": "状态迟到(决策时未知已流失)",
                         "intervened": 0})
            continue
        f = dict(feats.get(cid) or {})
        f.setdefault("logins_30", 0.0); f.setdefault("logins_prev30", 0.0)
        f.setdefault("tickets_90", 0); f.setdefault("tickets_30", 0)
        f.setdefault("tickets_prev60", 0); f.setdefault("high_sev_90", 0)
        f.setdefault("billing_180", 0)
        idle = f.get("days_idle")
        f["days_idle"] = idle if idle is not None else min(_days(D, c["signup_date"]), 999)
        f["tenure_days"] = _days(D, c["signup_date"])
        f["segment"] = c["segment"]
        score = round(rule_fn(f), 4)
        alert = 1 if score >= threshold else 0
        interv = 1 if cid in intervened else 0
        if interv:
            counts["intervened_total"] += 1
            if alert:
                counts["intervened_alerted"] += 1
        rows.append({
            "cid": cid, "segment": c["segment"], "score": score, "alert": alert,
            "y": label.get(cid, 0), "excluded": None, "intervened": interv,
            "features": {k: f[k] for k in ("logins_30", "logins_prev30", "tickets_90",
                                           "high_sev_90", "billing_180", "days_idle",
                                           "tenure_days")},
        })

    # 决策日之后才更正、但决策时已在册的重复账号(特征被拆散,分数受噪声影响)
    counts["dup_in_cohort"] = conn.execute(
        "SELECT COUNT(*) AS n FROM identity_corrections ic "
        "JOIN customers c ON c.customer_id = ic.duplicate_id "
        "WHERE ic.corrected_at > ? AND c.signup_date <= ?", (D, D)).fetchone()["n"]
    counts["identity_corrections_after_D"] = conn.execute(
        "SELECT COUNT(*) AS n FROM identity_corrections WHERE corrected_at > ?", (D,)
    ).fetchone()["n"]
    return rows, counts, {"decision_date": D, "window_end": end, "evidence_cutoff": cutoff}


# ---------------------------------------------------------------- 指标

def metrics(rows):
    """rows: 已排除删失、含 alert/y 的列表。"""
    tp = sum(1 for r in rows if r["alert"] and r["y"] == 1)
    fp = sum(1 for r in rows if r["alert"] and r["y"] == 0)
    fn = sum(1 for r in rows if not r["alert"] and r["y"] == 1)
    tn = sum(1 for r in rows if not r["alert"] and r["y"] == 0)
    n = tp + fp + fn + tn
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    base = (tp + fn) / n if n else 0.0
    return {
        "n": n, "alerts": tp + fp, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
        "alert_rate": round((tp + fp) / n, 4) if n else 0.0,
        "base_rate": round(base, 4),
        "lift": round(prec / base, 3) if base else None,
    }


def _included(rows):
    return [r for r in rows if r["excluded"] is None]


def _stratified(rows, threshold):
    """被预警客户按风险分分层,层内对比干预/未干预流失率(描述性)。"""
    alerted = [r for r in _included(rows) if r["alert"]]
    if not alerted:
        return {"bands": [], "raw_diff": None, "adj_diff": None}
    width = (1.0 - threshold) / 5.0
    bands = []
    for i in range(5):
        lo = threshold + i * width
        hi = lo + width
        band = [r for r in alerted if lo <= r["score"] < hi or (i == 4 and r["score"] >= lo)]
        iv = [r for r in band if r["intervened"]]
        nv = [r for r in band if not r["intervened"]]
        cr_i = sum(r["y"] for r in iv) / len(iv) if iv else None
        cr_n = sum(r["y"] for r in nv) / len(nv) if nv else None
        bands.append({
            "range": [round(lo, 3), round(hi, 3)], "n": len(band),
            "n_intervened": len(iv), "n_not": len(nv),
            "churn_intervened": round(cr_i, 4) if cr_i is not None else None,
            "churn_not": round(cr_n, 4) if cr_n is not None else None,
            "diff": round(cr_i - cr_n, 4) if cr_i is not None and cr_n is not None else None,
        })
    iv_all = [r for r in alerted if r["intervened"]]
    nv_all = [r for r in alerted if not r["intervened"]]
    raw = (sum(r["y"] for r in iv_all) / len(iv_all)
           - sum(r["y"] for r in nv_all) / len(nv_all)) if iv_all and nv_all else None
    total = len(alerted)
    adj = None
    if total:
        parts = [b["n"] / total * b["diff"] for b in bands if b["diff"] is not None and b["n"]]
        adj = sum(parts) if parts else None
    return {
        "bands": bands,
        "raw_diff": round(raw, 4) if raw is not None else None,
        "adj_diff": round(adj, 4) if adj is not None else None,
    }


# ---------------------------------------------------------------- 主入口

def run_backtest(conn, config):
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (config or {}).items() if v is not None})
    cfg["threshold"] = float(cfg["threshold"])
    cfg["horizon_days"] = int(cfg["horizon_days"])
    cfg["grace_days"] = int(cfg["grace_days"])
    cfg["holdout_days"] = int(cfg["holdout_days"])
    if cfg["rule_version"] not in RULES:
        raise ValueError(f"未知规则版本: {cfg['rule_version']}")
    if not 0.05 <= cfg["threshold"] <= 0.95:
        raise ValueError("阈值需在 0.05..0.95 之间")

    D = cfg["decision_date"]
    cutoff = add_days(D, cfg["horizon_days"] + cfg["grace_days"])
    cutoff_clamped = min(cutoff, TODAY)

    rows, counts, period = evaluate_cohort(
        conn, cfg["rule_version"], cfg["threshold"], D, cfg["horizon_days"], cutoff)

    inc = _included(rows)
    out = {
        "config": dict(cfg, evidence_cutoff=cutoff_clamped),
        "period": period,
        "cohort_size": len(inc),
        "counts": counts,
        "methods": {},
        "rows": rows,
    }

    selected = set(cfg["methods"])
    if "naive_all" in selected:
        out["methods"]["naive_all"] = metrics(inc)
    if "no_intervention" in selected:
        out["methods"]["no_intervention"] = metrics([r for r in inc if not r["intervened"]])
    if "time_holdout" in selected:
        D0 = add_days(D, -cfg["holdout_days"])
        c0 = add_days(D0, cfg["horizon_days"] + cfg["grace_days"])
        rows0, counts0, period0 = evaluate_cohort(
            conn, cfg["rule_version"], cfg["threshold"], D0, cfg["horizon_days"], c0)
        inc0 = _included(rows0)
        out["methods"]["time_holdout"] = {
            "calibration": {"period": period0, "cohort_size": len(inc0),
                            "naive_all": metrics(inc0),
                            "no_intervention": metrics([r for r in inc0 if not r["intervened"]])},
            "holdout": {"period": period, "cohort_size": len(inc),
                        "naive_all": metrics(inc),
                        "no_intervention": metrics([r for r in inc if not r["intervened"]])},
        }
    if "stratified" in selected:
        out["methods"]["stratified"] = _stratified(rows, cfg["threshold"])

    out["bias_notes"] = _bias_notes(counts, out["methods"])
    return out


def _bias_notes(counts, methods):
    notes = []
    n_iv = counts.get("intervened_alerted", 0)
    if n_iv:
        notes.append(
            f"干预污染:本回测有 {n_iv} 个被预警客户在观察窗内接受了挽留干预,"
            "其后续表现已受干预影响。naive_all 把他们干预后的'未流失'直接记为误报,"
            "会系统性低估精确率——预测质量请以 no_intervention 口径为准。")
    if counts.get("censored_late_outcome"):
        notes.append(
            f"结果迟到:有 {counts['censored_late_outcome']} 个客户在窗内流失、"
            "但结果录入晚于证据截止日,已按删失排除。这是数据可见性约束,不计入模型对错。")
    if counts.get("late_state"):
        notes.append(
            f"状态迟到:有 {counts['late_state']} 个客户在决策时看似在册、"
            "实际流失记录迟于决策日才录入,已排除,避免用未来信息美化成绩。")
    if counts.get("identity_corrections_after_D"):
        notes.append(
            f"身份更正:决策日之后有 {counts['identity_corrections_after_D']} 条身份合并生效。"
            "回测按决策时点的身份状态计算特征(重复账号特征不完整),不做事后合并,"
            "因此这部分客户的分数与上线时一样受噪声影响。")
    if "no_intervention" in methods:
        notes.append(
            "选择偏差(无法识别):no_intervention 按'是否被干预'这一决策后变量筛选,"
            "被干预者本身风险更高、客群更受关注,该口径只是预测质量的近似,不是无偏估计。")
    if "stratified" in methods:
        notes.append(
            "残余混杂(无法识别):分层对照中干预分配与风险分、客户价值相关(非随机),"
            "层内差值只是描述性对比,不能解释为干预的因果效果;"
            "干预时机、坐席倾向、未记录特征等混杂无法从现有数据识别。")
    if "time_holdout" in methods:
        notes.append(
            "时间留出:校准期与留出期使用同一规则版本与阈值、各自决策日可见的数据;"
            "两期差异反映时间漂移与客群变化,不能归因于规则本身。")
    return notes


# ---------------------------------------------------------------- 版本与恢复

def save_run(conn, config, results, lineage_id=None, supersedes=None):
    created = TODAY
    if lineage_id:
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM runs WHERE lineage_id = ?",
            (lineage_id,)).fetchone()["v"]
    else:
        version = 1
    cur = conn.execute(
        "INSERT INTO runs(lineage_id, version, created_at, config_json, status, supersedes, results_json)"
        " VALUES(?,?,?,?,?,?,?)",
        (lineage_id, version, created, json.dumps(config, ensure_ascii=False),
         "done", supersedes, json.dumps(results, ensure_ascii=False)),
    )
    run_id = cur.lastrowid
    if not lineage_id:
        conn.execute("UPDATE runs SET lineage_id = ? WHERE run_id = ?", (run_id, run_id))
        lineage_id = run_id
    conn.commit()
    return {"run_id": run_id, "lineage_id": lineage_id, "version": version}


def list_runs(conn):
    rows = conn.execute(
        "SELECT run_id, lineage_id, version, created_at, config_json, status, supersedes"
        " FROM runs ORDER BY run_id DESC").fetchall()
    out = []
    for r in rows:
        cfg = json.loads(r["config_json"])
        out.append({
            "run_id": r["run_id"], "lineage_id": r["lineage_id"], "version": r["version"],
            "created_at": r["created_at"], "status": r["status"], "supersedes": r["supersedes"],
            "summary": f"{cfg.get('rule_version')} 阈值{cfg.get('threshold')} 决策日{cfg.get('decision_date')}",
        })
    return out


def get_run(conn, run_id):
    r = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not r:
        return None
    return {
        "run_id": r["run_id"], "lineage_id": r["lineage_id"], "version": r["version"],
        "created_at": r["created_at"], "status": r["status"], "supersedes": r["supersedes"],
        "config": json.loads(r["config_json"]),
        "results": json.loads(r["results_json"]) if r["results_json"] else None,
    }


def recompute(conn, run_id):
    """按原配置重算:产生同谱系新版本,旧版本保留可恢复。"""
    old = get_run(conn, run_id)
    if not old:
        raise ValueError(f"run {run_id} 不存在")
    results = run_backtest(conn, old["config"])
    meta = save_run(conn, old["config"], results,
                    lineage_id=old["lineage_id"], supersedes=run_id)
    return {"meta": meta, "results": results}


# ---------------------------------------------------------------- 报告

def save_report(conn, name, run_id, notes=""):
    run = get_run(conn, run_id)
    if not run or not run["results"]:
        raise ValueError(f"run {run_id} 不存在或无结果")
    snapshot = {
        "run_id": run["run_id"], "lineage_id": run["lineage_id"], "version": run["version"],
        "config": run["config"], "results": run["results"], "saved_at": TODAY,
    }
    cur = conn.execute(
        "INSERT INTO reports(name, created_at, run_id, notes, snapshot_json) VALUES(?,?,?,?,?)",
        (name, TODAY, run_id, notes, json.dumps(snapshot, ensure_ascii=False)),
    )
    conn.commit()
    return {"report_id": cur.lastrowid}


def list_reports(conn):
    rows = conn.execute(
        "SELECT report_id, name, created_at, run_id, notes FROM reports ORDER BY report_id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_report(conn, report_id):
    r = conn.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
    if not r:
        return None
    return {
        "report_id": r["report_id"], "name": r["name"], "created_at": r["created_at"],
        "run_id": r["run_id"], "notes": r["notes"],
        "snapshot": json.loads(r["snapshot_json"]),
    }


# ---------------------------------------------------------------- 客户证据

def customer_timeline(conn, customer_id, decision_date, rule_version):
    """单个客户的证据视图:决策时可见 vs 之后可见,分开标注。"""
    D = decision_date
    idmap = visible_id_map(conn, D)
    cust = conn.execute("SELECT * FROM customers WHERE customer_id = ?", (customer_id,)).fetchone()
    if not cust:
        return None

    feats = compute_features(conn, D, idmap)
    f = dict(feats.get(customer_id) or {})
    for k, v in (("logins_30", 0.0), ("logins_prev30", 0.0), ("tickets_90", 0),
                 ("tickets_30", 0), ("tickets_prev60", 0), ("high_sev_90", 0),
                 ("billing_180", 0), ("days_idle", None)):
        f.setdefault(k, v)
    if f["days_idle"] is None:
        f["days_idle"] = min(_days(D, cust["signup_date"]), 999)
    f["tenure_days"] = _days(D, cust["signup_date"])
    f["segment"] = cust["segment"]
    score = round(RULES[rule_version]["fn"](f), 4)

    # 涉及本客户的身份更正(本客户是主档 or 是重复档)
    corr = conn.execute(
        "SELECT * FROM identity_corrections WHERE primary_id = ? OR duplicate_id = ?",
        (customer_id, customer_id)).fetchall()
    related_ids = {customer_id}
    for c in corr:
        if c["primary_id"] == customer_id:
            related_ids.add(c["duplicate_id"])

    events = []
    for r in conn.execute(
        "SELECT customer_id, event_time, kind, value, ingested_at FROM activities "
        "WHERE customer_id IN (%s)" % ",".join("?" * len(related_ids)),
        tuple(related_ids)):
        events.append({
            "source": r["customer_id"], "event_time": r["event_time"],
            "kind": "活动:" + r["kind"], "detail": f"量={r['value']}",
            "ingested_at": r["ingested_at"],
        })
    for r in conn.execute(
        "SELECT customer_id, event_time, channel, issue_type, severity, ingested_at "
        "FROM service_records WHERE customer_id IN (%s)" % ",".join("?" * len(related_ids)),
        tuple(related_ids)):
        events.append({
            "source": r["customer_id"], "event_time": r["event_time"],
            "kind": "工单", "detail": f"{r['channel']}/{r['issue_type']}/严重度{r['severity']}",
            "ingested_at": r["ingested_at"],
        })
    # 决策时可见性:记录本身已入库,且其归属身份在 D 时点已归一到本客户
    corr_by_dup = {c["duplicate_id"]: c for c in corr}
    for e in events:
        src = e["source"]
        if src == customer_id:
            identity_visible = True   # 本档记录始终归属于本档
        else:
            c = corr_by_dup.get(src)
            identity_visible = bool(c and c["corrected_at"] <= D)
        e["visible_at_decision"] = bool(e["ingested_at"] <= D and identity_visible)
    events.sort(key=lambda e: (e["event_time"], e["ingested_at"]))

    interventions = [dict(r) for r in conn.execute(
        "SELECT action_time, action_type, recorded_at, decision_date FROM interventions "
        "WHERE customer_id = ? ORDER BY action_time", (customer_id,)).fetchall()]
    churns = [dict(r) for r in conn.execute(
        "SELECT churn_date, recorded_at FROM churn WHERE customer_id = ?",
        (customer_id,)).fetchall()]
    alerts = [dict(r) for r in conn.execute(
        "SELECT rule_version, fired_at, score, threshold FROM alerts_history "
        "WHERE customer_id = ? ORDER BY fired_at", (customer_id,)).fetchall()]

    return {
        "customer": dict(cust),
        "decision_date": D,
        "rule_version": rule_version,
        "features_at_decision": f,
        "score_at_decision": score,
        "identity_corrections": [dict(c) for c in corr],
        "events": events,
        "interventions": interventions,
        "churn": churns,
        "alerts_history": alerts,
    }
