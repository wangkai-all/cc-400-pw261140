# 多来源身份裁决 · 贡献重建 · 可追溯报表（虚构数据平台）

从零实现的演示平台：把多个来源系统中的身份先合并为**分析实体**，支持发现误合并后
**拆分撤销**；裁决、重建、报表是三个**独立进程服务**；所有报表数字都能追溯到
**裁决版本**和**源事件**。

> ⚠️ 全部身份、事件均由程序生成（`idres/synth.py`，固定词表 + 随机 id，
> `meta.data_kind='synthetic'`）。系统**不包含、也不实现任何真实个人识别功能**。

## 快速开始

```bash
python3 -m unittest discover -s tests -v   # 10 项不变量测试
./start.sh                                 # 初始化数据并启动 4 个进程
# 浏览器打开 http://127.0.0.1:8100
python3 run.py smoke                       # HTTP 端到端冒烟
./stop.sh
```

仅依赖 Python 3.11 标准库（`http.server` + SQLite WAL），无第三方包、无构建步骤。

| 进程 | 端口 | 职责 | 可独立停止 |
|---|---|---|---|
| 身份裁决服务 `services/adjudication_service.py` | 8101 | 证据、版本化合并/拆分、CAS、影响范围预估 | 是 |
| 贡献重建器 `services/rebuilder_service.py` | 8102 | 逐事件/区间/链路重建、中断恢复、后台 worker | 是 |
| 报表服务 `services/reporting_service.py` | 8103 | 新鲜度、实体报表、事件级溯源 | 是 |
| Web 控制台 `services/web_service.py` | 8100 | 静态 SPA + 到上述服务的反向代理 | 是 |

服务间只通过共享 SQLite 文件（WAL 模式）通信，停止任一服务不影响其余服务
（代理对不可用上游返回 503，页面其余部分照常工作）。

## 核心设计与需求对照

### 1. 先合并、误合并可拆，且不会只改映射而留下错误去重指标

* 每次裁决都是一条**不可变版本** `adjudication_versions`；成员关系
  `entity_membership` 只做"关闭旧行 + 开新行"，历史可完整回放
  （`GET /evidence/<sid>/history`）。
* 拆分不是改一张映射表：`commit_decision()` 在同一事务里
  （`idres/adjudication.py`）：
  1. 关闭/新建成员关系、作废空实体；
  2. 把受影响的逐事件行、区间快照、行为链全部置 `is_fresh=0`
     （`metric_freshness` 同步记账）；
  3. 为三种存储层分别排队重建任务。
* 因此"映射已改、去重指标仍旧正确显示"的状态在结构上不可能出现：
  报表把 stale 与 fresh 分开计数并显式标红（测试 `test_02`、`test_04`）。

### 2. 三个服务独立，三类结果的影响半径不同

合并/拆分确认前，`POST /impact` 返回成本对比，页面弹表展示：

| 存储层 | 影响范围 | 能否局部修复 |
|---|---|---|
| 逐事件归属 `event_attribution` | **local**：只重写被移动 sid 的事件行 | 能，按行 upsert |
| 区间快照 `interval_snapshots` | 受影响月份窗**整窗重建** | **不能**：聚合内不存逐事件增量，无法只改一个数 |
| 跨实体行为链 `behavior_chains` | **full**：有序序列，触一链即整链重算 | 不能，半径最大 |

区间快照另用 `interval_snapshot_events` 保留到源事件的出处，聚合不是黑盒。

### 3. 逐事件归属 vs 按区间重建：成本与"无法局部修复"的展示

* 逐事件：每事件一行，存储量 O(事件数)，局部修复成本 O(受影响事件)。
* 区间：每实体每月一行，存储量小，但任何归属变化都要重算整个月份窗；
  UI 影响预览直接给出"窗口数 N/总窗口 M、repairable_locally=false"。
* 重建器同一套游标机制支持两种模式（`idres/rebuilder.py`），可在任务列表对比
  `impact_scope` 与实际 `processed/total`。

### 4. 网页可看证据、撤销合并、中断重建，并在期间接收新事件

SPA 六个面板（`web/`）：

1. **候选证据**：强证据（同邮箱/同设备）与弱证据（name+geo+设备，分值 0.58）、
   矛盾证据（邮箱冲突，0.15）并列，可一键进入合并流程。
2. **分析实体**：合并前先看影响成本；拆分面板勾选 sid 即可撤销误合并。
3. **重建进程**：进度条、持久化游标、**中断**/恢复按钮，支持 1 秒轮询。
4. **新事件注入**：重建进行中也可注入；任务按小批次提交游标，批次查询只依赖
   游标（高水位），新事件在下一批自动纳入，无需重启、不漏事件
   （测试 `test_05`、`test_06`、`test_10`）。
5. **旧裁决拦截**：一键模拟晚到的旧合并。
6. **报表与溯源**：新鲜度总览、两种口径报表、事件级钻取。

中断语义：`interrupting` 是协作标志，worker 在批次边界落定为 `interrupted`
并保留游标；若标志在任务被认领前就到达，`_sweep_interrupting()` 也会把它
正确落定，不会卡死（测试 `test_10`）。

### 5. 旧裁决晚到不得盖掉新结论（乐观并发 / CAS）

`commit_decision()` 以 `BEGIN IMMEDIATE` 开启事务并校验
`base_version == 当前版本`：

* 不匹配 → 回滚业务事务，把该决策原文写入 `stale_decisions` 审计表，返回 409；
* 审计写入在独立事务，拒绝动作本身持久可查（页面面板⑤）。

测试 `test_03`：v2 合并 → v3 拆分后，持有 base_version=2 的晚到合并被拒，
映射保持 v3 结论。

### 6. 报表可追溯到裁决版与源事件

* 报表头带 `adjudication_version`，每行带 `decided_versions`；
* 逐事件口径直接列出源事件 id；区间口径通过 `interval_snapshot_events`
  列出构成快照的源事件；
* `GET /trace/<event_id>` 钻取：源事件 → 成员关系时间线（含每版裁决理由）
  → 当前归属 → 所在区间快照 → 所在行为链（测试 `test_07`）；
* 每份报表持久化在 `report_records`，可按 id 回看。

## 数据模型摘要（`idres/db.py`）

```
source_identities ──< source_events
candidate_evidence（证据与裁决状态）
adjudication_versions（不可变版本）
entity_membership（sid→eid 时间线） / analysis_entities
stale_decisions（被 CAS 拒绝的旧裁决审计）
rebuild_jobs（游标、进度、中断状态）
event_attribution（逐事件，local 修复）
interval_snapshots + interval_snapshot_events（区间聚合 + 出处）
behavior_chains（跨实体有序链）
metric_freshness（每层数据的版本/新鲜度记账）
report_records（持久化报表）
```

## 测试

`tests/test_platform.py`（每个用例使用独立临时库）：

1. 初始播种与全量构建 2. 合并即失效指标并排队 3. CAS 拒绝晚到旧裁决
4. 拆分仅局部重写并最终 fresh 5. 批次级中断与游标恢复
6. 重建期间新事件不丢 7. 报表/事件溯源完整 8. 区间整窗重建且无孤儿出处
9. 拆分后行为链重算 10. 认领前中断的清扫与恢复
