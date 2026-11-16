"""预警规则版本。

每个版本是一个纯函数:输入"决策日可见的特征字典",输出 0..1 风险分。
规则带 effective_from,回测按决策日绑定当时生效的版本,
也允许显式指定版本做"如果当时用新规则会怎样"的回溯评估。
"""


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def decline_ratio(f):
    """近 30 天登录相对前 30 天的下滑比例,0..1。"""
    prev = f["logins_prev30"]
    cur = f["logins_30"]
    if prev <= 0:
        return 1.0 if cur <= 0 else 0.0
    return _clip(1.0 - cur / prev)


def _tenure_factor(f):
    return 1.0 if f["tenure_days"] < 180 else 0.2


def score_v1(f):
    """v1 基础规则:登录下滑 + 工单量 + 账单问题 + 沉寂天数。"""
    return _clip(
        0.45 * decline_ratio(f)
        + 0.25 * min(f["tickets_90"] / 4.0, 1.0)
        + 0.20 * min(f["billing_180"] / 2.0, 1.0)
        + 0.10 * min(f["days_idle"] / 30.0, 1.0)
    )


def score_v2(f):
    """v2:引入严重工单与新手期因子,降低账单权重。"""
    return _clip(
        0.35 * decline_ratio(f)
        + 0.20 * min(f["tickets_90"] / 4.0, 1.0)
        + 0.25 * min(f["high_sev_90"] / 2.0, 1.0)
        + 0.10 * min(f["billing_180"] / 2.0, 1.0)
        + 0.10 * _tenure_factor(f)
    )


def score_v3(f):
    """v3(候选):在 v2 基础上加入工单趋势项。"""
    trend = _clip((f["tickets_30"] - f["tickets_prev60"] / 2.0) / 3.0)
    return _clip(
        0.32 * decline_ratio(f)
        + 0.18 * min(f["tickets_90"] / 4.0, 1.0)
        + 0.22 * min(f["high_sev_90"] / 2.0, 1.0)
        + 0.08 * min(f["billing_180"] / 2.0, 1.0)
        + 0.08 * _tenure_factor(f)
        + 0.12 * trend
    )


RULES = {
    "v1": {
        "effective_from": "2025-01-01",
        "label": "v1 基础规则",
        "fn": score_v1,
        "desc": "登录下滑0.45 + 工单0.25 + 账单0.20 + 沉寂0.10",
    },
    "v2": {
        "effective_from": "2026-03-01",
        "label": "v2 严重度加权",
        "fn": score_v2,
        "desc": "登录下滑0.35 + 工单0.20 + 严重工单0.25 + 账单0.10 + 新手期0.10",
    },
    "v3": {
        "effective_from": "2026-09-01",
        "label": "v3 候选(加工单趋势)",
        "fn": score_v3,
        "desc": "v2 调权 + 工单趋势0.12",
    },
}


def rule_effective_at(date_str):
    """决策日 date_str 当天实际生效的规则版本。"""
    chosen = None
    for ver, meta in RULES.items():
        if meta["effective_from"] <= date_str:
            if chosen is None or meta["effective_from"] > RULES[chosen]["effective_from"]:
                chosen = ver
    return chosen
