# churnwatch · 流失预警回测分析(全虚构样本)

把客户活动、服务记录、历史预警和挽留动作接到一个分析页,用于**回看预警效果**。
所有客户、事件、干预均为本地生成的虚构数据;不要求、也不声称凭空证明干预的因果效果。

## 运行

```bash
python3 run.py            # 首次运行自动生成虚构样本,然后打开 http://127.0.0.1:8000/
python3 run.py --reseed   # 重新生成样本
python3 scripts/smoke.py  # 端到端冒烟测试
```

仅依赖 Python 3 标准库(sqlite3 + http.server),无需安装任何包。

## 数据模型:每条记录都有两个时间

| 表 | 发生时间 | 系统知晓时间 |
|---|---|---|
| activities(登录/使用) | event_time | ingested_at |
| service_records(工单) | event_time | ingested_at |
| churn(流失结果) | churn_date | recorded_at(可迟到 0–120 天) |
| interventions(挽留动作) | action_time | recorded_at |
| identity_corrections(身份更正) | — | corrected_at(更正才生效) |

**每份回测绑定决策当时可见的数据**:特征只用 `ingested_at <= 决策日` 的记录;
身份更正按 `corrected_at <= 决策日` 的状态生效(更正前,重复账号就是"另一个客户",
特征被拆散);结果只认 `recorded_at <= 证据截止日` 的流失,迟到结果按删失排除。
规则升级(v1→v2→v3)不影响历史回测——历史预警由当时生效的版本产生,
回测则显式绑定所选版本重算。不能用未来服务记录提升成绩(冒烟测试第 3 组专门验证)。

## 为什么不能直接给被干预客户的原风险"打对错"

做过挽留干预的客户,后续表现已经受干预影响:他们没流失,可能正是干预起了作用。
把他们记为"误报"会系统性低估精确率。因此页面提供四种口径,并把**预测质量**与
**干预效果**分开:

| 方法 | 回答的问题 | 已知/无法识别的偏差 |
|---|---|---|
| 朴素全样本 naive_all | 对照组:把干预后表现直接计入会怎样 | 干预污染,精确率被低估(仅作反面对照) |
| 未干预样本 no_intervention | 预警本身准不准(近似) | 按"是否被干预"这一决策后变量筛选 → 选择偏差,非无偏 |
| 时间留出 time_holdout | 同一规则跨期稳不稳 | 两期差异含客群/季节漂移,不能归因于规则 |
| 分层对照 stratified | 被干预者与相似未干预者差多少(描述性) | 干预分配非随机,残余混杂无法识别,**不能解释为因果效果** |

结果区自动列出当次回测的偏差说明(干预污染数、迟到删失数、身份更正数等)。

## 页面功能

- 调阈值(滑块)、选规则版本、决策日、观察窗、结果宽限期,勾选评估方法后运行回测;
- 方法对比图与指标表、分层对照表、偏差说明;
- **误报检视**:逐条查看误报客户的决策时点证据(哪些记录当时可见、哪些后到的,
  是否被干预、身份更正、历史预警);
- **保存报告**:对某次运行的不可变快照 + 备注,可随时回看;
- **运行历史**:每次运行落库,带谱系版本号(Lx·vy);可"恢复查看"任意历史版本,
  或"按原配置重算"生成同谱系新版本(旧版本保留)。

## API

```
GET  /api/meta                          规则版本、默认参数
POST /api/backtest   {配置}             运行回测并落库 → {meta, results}
GET  /api/runs                          运行历史(版本)
GET  /api/run?id=                       恢复某次运行
POST /api/recompute  {run_id}           按原配置重算 → 同谱系新版本
POST /api/reports    {name,run_id,notes} 保存报告快照
GET  /api/reports  /api/report?id=      报告列表 / 快照
GET  /api/customer?id=&decision_date=&rule_version=  客户证据时间线
```

## 结构

```
run.py                 入口
churnwatch/
  db.py                SQLite schema(发生时间/知晓时间分离)
  rules.py             预警规则 v1/v2/v3(带生效日期)
  seed.py              虚构样本生成(固定随机种子,可复现)
  backtest.py          回测引擎:决策时点特征、四种口径、版本与报告
  server.py            HTTP 服务(仅标准库)
web/                   分析页(原生 HTML/JS,SVG 图表)
scripts/smoke.py       冒烟测试(含"未来记录不影响分数"验证)
```
