"""生成全部虚构的本地样本数据。

时间轴:2025-01-01 .. 2026-11-16(固定 TODAY,保证可复现)。
生成顺序:
  0. 客户主档 + 重复账号(身份更正)计划
  1. 无干预情形下的"自然"流失日期(虚构真实过程)
  2. 活动(登录/使用)与服务记录(工单),带入库延迟;更正前的部分记录挂在重复账号下
  3. 逐月决策日:按当时生效的规则版本打分 -> 历史预警 -> 挽留干预(非随机分配)
  4. 干预影响(虚构):部分被干预客户流失被推迟/避免
  5. 流失结果入库,部分结果迟到(recorded_at 明显晚于 churn_date)
"""
import random
from datetime import date, timedelta

from .backtest import TODAY, compute_features, visible_id_map
from .rules import RULES, rule_effective_at

SEED = 20261116
START = "2025-01-01"

SURNAMES = "岚岑浦梧渚澄砚棠棣枳"
WORDS = ["云帆", "星野", "青禾", "白鹭", "知微", "观澜", "拾光", "未名", "栖迟", "望舒"]
INDUSTRIES = ["科技", "贸易", "餐饮", "物流", "教育", "制造", "咨询", "零售"]
REGIONS = ["华东", "华北", "华南", "西南", "东北"]
ACTION_TYPES = ["电话回访", "优惠券", "专属客服", "套餐调整"]


def _d(s):
    return date.fromisoformat(s)


def _s(d):
    return d.isoformat()


def seed(conn, n_customers=620, verbose=True):
    rng = random.Random(SEED)

    # ---- 0. 客户主档 + 重复账号计划 --------------------------------------
    customers = []          # (cid, name, segment, region, signup_date, base_risk)
    for i in range(n_customers):
        cid = f"C{i + 1:04d}"
        seg = rng.choices(["A", "B", "C"], weights=[0.25, 0.45, 0.30])[0]
        signup = _d("2024-06-01") + timedelta(days=rng.randrange(0, 790))
        base_risk = min(rng.random() ** 1.4 + (0.08 if seg == "C" else 0.0), 0.98)
        name = f"虚构·{rng.choice(SURNAMES)}{rng.choice(WORDS)}{rng.choice(INDUSTRIES)}"
        customers.append((cid, name, seg, rng.choice(REGIONS), _s(signup), base_risk))

    # 重复账号:primary -> (dup_id, dup_signup, corrected_at)
    dup_plan, dup_rows = {}, []
    for j, p in enumerate(rng.sample(customers, 14)):
        dup_id = f"D{j + 1:03d}"
        dup_signup = _d(p[4]) + timedelta(days=rng.randrange(150, 420))
        if dup_signup >= _d(TODAY):
            continue
        corrected = min(dup_signup + timedelta(days=rng.randrange(90, 300)), _d(TODAY))
        dup_plan[p[0]] = (dup_id, _s(dup_signup), _s(corrected))
        dup_rows.append((dup_id, f"虚构·{rng.choice(SURNAMES)}{rng.choice(WORDS)}(重复账号)",
                         p[2], p[3], _s(dup_signup)))

    # ---- 1. 自然流失(无干预的虚构真实过程) ------------------------------
    natural_churn = {}
    for cid, _n, _s_, _r, signup, risk in customers:
        t = max(_d(signup), _d(START))
        while t <= _d(TODAY):
            if rng.random() < 0.006 + 0.04 * risk:
                natural_churn[cid] = _s(min(t + timedelta(days=rng.randrange(0, 28)), _d(TODAY)))
                break
            t += timedelta(days=30)

    # ---- 2. 活动与服务记录 ----------------------------------------------
    act_rows, svc_rows = [], []
    for cid, _n, _s_, _r, signup, risk in customers:
        end_date = _d(natural_churn.get(cid, TODAY))
        t = max(_d(signup), _d(START)) - timedelta(days=3)
        while t <= min(end_date, _d(TODAY)):
            days_to_churn = (end_date - t).days
            decay = 1.0 if days_to_churn > 56 else max(0.25, days_to_churn / 56.0)
            lam = max(0.2, 9.0 * (1.15 - risk) * decay)
            n_login = int(round(rng.expovariate(1 / lam)))
            if n_login > 0:
                act_rows.append((cid, _s(t), "login", n_login,
                                 _s(t + timedelta(days=rng.randrange(0, 3)))))
            usage = round(max(0.0, rng.gauss(3.0 * (1.1 - risk) * decay, 0.8)), 2)
            if usage > 0.2:
                act_rows.append((cid, _s(t), "usage", usage,
                                 _s(t + timedelta(days=rng.randrange(0, 3)))))
            t += timedelta(days=7)
        t = max(_d(signup), _d(START))
        while t <= min(end_date, _d(TODAY)):
            near_churn = 0 <= (end_date - t).days <= 90
            lam = 0.4 + 0.8 * risk + (1.5 if near_churn else 0.0)
            for _ in range(int(round(rng.expovariate(1 / (lam + 0.01))))):
                day = t + timedelta(days=rng.randrange(0, 30))
                if day > min(end_date, _d(TODAY)):
                    continue
                sev = rng.choices([1, 2, 3],
                                  weights=[0.55, 0.30, 0.15 + (0.15 if near_churn else 0)])[0]
                issue = rng.choices(
                    ["billing", "technical", "shipping", "other"],
                    weights=[0.35 if near_churn else 0.18, 0.35, 0.15, 0.15])[0]
                svc_rows.append((cid, _s(day), rng.choice(["phone", "online", "onsite"]),
                                 issue, sev, _s(day + timedelta(days=rng.randrange(0, 6)))))
            t += timedelta(days=30)

    # 更正日之前,主档部分记录挂在重复账号下(决策时特征因此不完整)
    def reassign(rows):
        out = []
        for row in rows:
            dup = dup_plan.get(row[0])
            if dup and dup[1] <= row[1] < dup[2] and rng.random() < 0.5:
                row = (dup[0],) + row[1:]
            out.append(row)
        return out

    act_rows, svc_rows = reassign(act_rows), reassign(svc_rows)

    _insert_base(conn, customers, dup_rows, act_rows, svc_rows, dup_plan)
    _seed_alerts_and_outcomes(conn, customers, natural_churn, rng)
    if verbose:
        _report(conn)


def _insert_base(conn, customers, dup_rows, act_rows, svc_rows, dup_plan):
    conn.executemany(
        "INSERT INTO customers(customer_id, name, segment, region, signup_date) VALUES(?,?,?,?,?)",
        [(c[0], c[1], c[2], c[3], c[4]) for c in customers] + dup_rows)
    conn.executemany(
        "INSERT INTO activities(customer_id, event_time, kind, value, ingested_at) VALUES(?,?,?,?,?)",
        act_rows)
    conn.executemany(
        "INSERT INTO service_records(customer_id, event_time, channel, issue_type, severity, ingested_at)"
        " VALUES(?,?,?,?,?,?)", svc_rows)
    conn.executemany(
        "INSERT INTO identity_corrections(duplicate_id, primary_id, corrected_at, note) VALUES(?,?,?,?)",
        [(v[0], k, v[2], "同一企业重复开户,核实后合并") for k, v in dup_plan.items()])
    conn.commit()


def _seed_alerts_and_outcomes(conn, customers, natural_churn, rng):
    """历史预警 -> 挽留干预 -> 干预后的最终流失 -> 结果入库(部分迟到)。"""
    seg_of = {r["customer_id"]: r["segment"] for r in conn.execute(
        "SELECT customer_id, segment FROM customers")}
    signup_of = {r["customer_id"]: r["signup_date"] for r in conn.execute(
        "SELECT customer_id, signup_date FROM customers")}

    alert_rows, int_rows = [], []
    interventions_by_cust = {}
    decision = _d("2025-06-01")
    while decision <= _d("2026-10-01"):
        D = _s(decision)
        rule_ver = rule_effective_at(D)
        rule_fn = RULES[rule_ver]["fn"]
        idmap = visible_id_map(conn, D)
        feats = compute_features(conn, D, idmap)
        for cid, f in feats.items():
            if cid in idmap or signup_of.get(cid, "9999") > D:
                continue
            nc = natural_churn.get(cid)
            if nc and nc <= D:
                continue  # 已流失(结果可能尚未录入,但坐席已联系不上)
            f = dict(f)
            f["days_idle"] = f["days_idle"] if f["days_idle"] is not None else 60
            f["tenure_days"] = (_d(D) - _d(signup_of[cid])).days
            f["segment"] = seg_of.get(cid, "B")
            score = round(rule_fn(f), 4)
            if score >= 0.5:
                alert_rows.append((cid, rule_ver, D, score, 0.5))
                # 干预非随机:分数越高、价值越高越容易被干预 -> 选择偏差
                p_int = min(0.9, 0.25 + 0.5 * score + (0.15 if seg_of.get(cid) == "A" else 0))
                if rng.random() < p_int:
                    act_day = decision + timedelta(days=rng.randrange(1, 11))
                    if nc and _s(act_day) >= nc:
                        continue  # 流失已发生,干预来不及
                    interventions_by_cust.setdefault(cid, []).append(_s(act_day))
                    int_rows.append((cid, _s(act_day), rng.choice(ACTION_TYPES),
                                     _s(act_day + timedelta(days=rng.randrange(0, 4))), D))
        decision += timedelta(days=30)

    # 干预效果(虚构真实):部分干预把流失推迟;推迟后可能超出 TODAY
    final_churn = {}
    for cid, nc in natural_churn.items():
        churn_date = nc
        for iv in sorted(interventions_by_cust.get(cid, [])):
            if iv < churn_date and rng.random() < 0.45:
                churn_date = _s(_d(churn_date) + timedelta(days=int(rng.expovariate(1 / 150)) + 30))
        if churn_date <= TODAY:
            final_churn[cid] = churn_date

    # 结果入库:70% 准时,20% 迟到 8-30 天,10% 迟到 31-120 天;至今未录入的不存在
    churn_rows = []
    for cid, cd in final_churn.items():
        u = rng.random()
        lag = rng.randrange(0, 8) if u < 0.7 else (rng.randrange(8, 31) if u < 0.9 else rng.randrange(31, 121))
        rec = _s(_d(cd) + timedelta(days=lag))
        if rec <= TODAY:
            churn_rows.append((cid, cd, rec))

    conn.executemany(
        "INSERT INTO alerts_history(customer_id, rule_version, fired_at, score, threshold)"
        " VALUES(?,?,?,?,?)", alert_rows)
    conn.executemany(
        "INSERT INTO interventions(customer_id, action_time, action_type, recorded_at, decision_date)"
        " VALUES(?,?,?,?,?)", int_rows)
    conn.executemany(
        "INSERT INTO churn(customer_id, churn_date, recorded_at) VALUES(?,?,?)", churn_rows)
    conn.commit()


def _report(conn):
    for table in ("customers", "activities", "service_records", "identity_corrections",
                  "alerts_history", "interventions", "churn"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        print(f"  {table:22s} {n:>8,}")
