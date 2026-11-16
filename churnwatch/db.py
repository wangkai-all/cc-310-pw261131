"""SQLite 存储层。

设计要点(对应"决策当时可见"约束):
- 每条业务记录都带两个时间:事件发生时间(event_time / churn_date / action_time)
  与系统知晓时间(ingested_at / recorded_at / corrected_at)。
- 任何回测在决策日 D 取数时,一律以 "知晓时间 <= D" 过滤,
  身份更正(identity_corrections.corrected_at)也按 D 之前的状态生效。
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "churnwatch.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    segment       TEXT NOT NULL,          -- A=高价值 / B=普通 / C=长尾
    region        TEXT NOT NULL,
    signup_date   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   TEXT NOT NULL,
    event_time    TEXT NOT NULL,          -- 事件发生日
    kind          TEXT NOT NULL,          -- login / usage
    value         REAL NOT NULL,          -- 次数或时长
    ingested_at   TEXT NOT NULL           -- 入库(系统可见)日
);
CREATE INDEX IF NOT EXISTS idx_act_cust ON activities(customer_id, event_time);
CREATE INDEX IF NOT EXISTS idx_act_ing  ON activities(ingested_at);

CREATE TABLE IF NOT EXISTS service_records (
    record_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   TEXT NOT NULL,
    event_time    TEXT NOT NULL,
    channel       TEXT NOT NULL,          -- phone / online / onsite
    issue_type    TEXT NOT NULL,          -- billing / technical / shipping / other
    severity      INTEGER NOT NULL,       -- 1..3
    ingested_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_svc_cust ON service_records(customer_id, event_time);
CREATE INDEX IF NOT EXISTS idx_svc_ing  ON service_records(ingested_at);

-- 身份更正:duplicate_id 在 corrected_at 才被确认并入 primary_id。
-- 在 corrected_at 之前的回测视角里,两个 id 仍是"两个客户"。
CREATE TABLE IF NOT EXISTS identity_corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    duplicate_id  TEXT NOT NULL,
    primary_id    TEXT NOT NULL,
    corrected_at  TEXT NOT NULL,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS churn (
    customer_id   TEXT NOT NULL,
    churn_date    TEXT NOT NULL,          -- 流失实际发生日
    recorded_at   TEXT NOT NULL           -- 结果入系统日(可能迟到)
);
CREATE INDEX IF NOT EXISTS idx_churn_rec ON churn(recorded_at);

CREATE TABLE IF NOT EXISTS interventions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   TEXT NOT NULL,
    action_time   TEXT NOT NULL,          -- 挽留动作发生日
    action_type   TEXT NOT NULL,          -- 电话回访 / 优惠券 / 专属客服 / 套餐调整
    recorded_at   TEXT NOT NULL,
    decision_date TEXT NOT NULL           -- 由哪一期预警决策触发
);
CREATE INDEX IF NOT EXISTS idx_int_cust ON interventions(customer_id, action_time);

-- 历史上真实发出过的预警(由当时生效的规则版本产生)
CREATE TABLE IF NOT EXISTS alerts_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   TEXT NOT NULL,
    rule_version  TEXT NOT NULL,
    fired_at      TEXT NOT NULL,
    score         REAL NOT NULL,
    threshold     REAL NOT NULL
);

-- 回测运行:每次运行落库,带 lineage/version,支持恢复与重算
CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lineage_id    INTEGER,                -- 同一配置谱系
    version       INTEGER NOT NULL,       -- 谱系内版本号,从 1 开始
    created_at    TEXT NOT NULL,
    config_json   TEXT NOT NULL,
    status        TEXT NOT NULL,          -- done / failed
    supersedes    INTEGER,                -- 重算时指向被替代 run
    results_json  TEXT                    -- 完整结果(含逐客户行),不可变
);

-- 保存的报告:对某次 run 的不可变快照 + 人工备注
CREATE TABLE IF NOT EXISTS reports (
    report_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    run_id        INTEGER NOT NULL,
    notes         TEXT,
    snapshot_json TEXT NOT NULL
);
"""


def connect(path=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def is_seeded(conn):
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()
        return row["n"] > 0
    except sqlite3.OperationalError:
        return False
