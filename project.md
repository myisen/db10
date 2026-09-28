# DBSQL-Tuner —— Oracle SQL 优化平台项目计划

> 项目代号：DBSQL-Tuner
> 目标数据库：Oracle 10g / 11g / 12c / 19c（兼容老版本，优先保障 10g 场景）
> 交付形态：Web 平台 + 轻量 Agent（可选）
> 核心链路：**定位 Top SQL → 拉取对象统计信息 → 执行计划瓶颈分析 → 生成 Hint / 索引 / 改写建议 → 沙箱执行 → 前/后量化对比**

---

## 1. 背景与定位

在 Oracle 生产环境，DBA 面对慢 SQL 的典型痛点：

- Top SQL 散落在 AWR / ASH / V$SQL / 业务监控里，口径不一，定位成本高；
- 拿到 SQL 后，要跨多个视图（`DBA_TABLES`、`DBA_INDEXES`、`V$OBJECT_USAGE`、`V$STATNAME`）手工查表、统计、执行计划，效率低；
- 执行计划的瓶颈判断（全表扫描、基数估算偏差、嵌套循环不当、回表过多、Latch 争用）依赖个人经验，难以沉淀；
- 优化建议（Hint、索引、改写）无法快速验证，"改前改后"缺少统一的、可复现的对比机制；
- 10g 场景没有 12c 的 `Real-Time SQL Monitoring` 和 `SQL Tuning Set` 的全套能力，需要平台补齐。

**DBSQL-Tuner 的定位**：做 DBA 经验的产品化。它不是另一个 SQL Monitor，而是一把"优化工程化"的锤子——把"发现 → 诊断 → 建议 → 验证 → 对比"串成一条可重复、可审计的流水线。

---

## 2. 核心功能清单（功能点级别）

以下功能按 **使用链路顺序** 组织。每个功能点标注 **输入** / **输出** / **关键实现要点** / **Oracle 版本差异**。

### F1. Top SQL 定位

| 项目 | 说明 |
|------|------|
| 输入 | 时间窗口、指标阈值（执行时间 / CPU / 逻辑读 / 物理读 / 执行次数）、过滤条件（模块、用户、SQL 文本关键词） |
| 输出 | Top N SQL 列表，每行包含 SQL_ID、执行次数、总耗时、平均耗时、CPU、逻辑读、物理读、版本数、首次/末次执行、所属应用 |
| 关键实现 | ① 11g+ 从 `V$SQL` / `GV$SQL` 实时拉；10g 从 `V$SQLAREA` + `V$SESS_IO` 补 IO 统计；② 历史窗口从 `DBA_HIST_SQLSTAT` + `DBA_HIST_SNAPSHOT`（AWR）；③ 指标口径统一为"单次执行平均 × 执行次数"排序，避免高频快 SQL 被淹没；④ 支持 AWR 基线对比 |
| 版本差异 | 10g 无 `SQL_ID`（用 `ADDRESS + HASH_VALUE` 组合）；11g 引入 `SQL_ID` + `V$SQL_MONITOR`；12c 增强 `V$SQL` 的 `IO_INTERCONNECT_BYTES` |

### F2. 基础对象统计信息采集

| 项目 | 说明 |
|------|------|
| 输入 | SQL 文本（解析出涉及的表 / 索引） |
| 输出 | 对象画像卡片（见表） |
| 关键实现 | ① 用 `dbms_utility.dependency` 或解析 `EXPLAIN PLAN` 的 `OBJECT_OWNER.OBJECT_NAME` 拿到依赖表；② 并发查询统计视图；③ 10g 下需兼容 `NUM_ROWS` 可能陈旧的问题，必要时触发 `DBMS_STATS.GATHER_TABLE_STATS`（只读模式下跳过）；④ 索引使用情况从 `V$OBJECT_USAGE`（10g/11g）或 `DBA_INDEXES.USE`（12c+） |
| 版本差异 | 10g 无 `DBA_TAB_STATISTICS` 的 `LAST_ANALYZED` 精确时间；11g+ 有 `DBA_TAB_MODIFICATIONS` 可判断统计是否过时 |

**对象画像字段**：

- 表：Owner / 表名 / 总行数 (`NUM_ROWS`) / 块数 (`BLOCKS`) / 空块 (`EMPTY_BLOCKS`) / 平均行长 / 统计时间 / 是否分区 / 分区数 / 关联索引数
- 索引：Owner / 索引名 / 表 / 唯一 / 列序列 / 叶子块 / 唯一键数 (`DISTINCT_KEYS`) / 聚簇因子 (`CLUSTERING_FACTOR`) / 索引类型 / 是否被使用 / 统计时间
- 列：Owner / 表 / 列名 / 数据类型 / 空比例 (`NUM_NULLS`) / 基数 (`NUM_DISTINCT`) / 直方图类型 / 直方图桶数

### F3. 执行计划获取与可视化

| 项目 | 说明 |
|------|------|
| 输入 | SQL 文本 + SQL_ID |
| 输出 | 树状执行计划图 + 计划成本/基数/字节 + 实际执行 vs 估算对比（11g+） |
| 关键实现 | ① 先用 `EXPLAIN PLAN INTO` 拿估算计划；② 11g+ 若 SQL 在 `V$SQL` 中，追加从 `V$SQL_PLAN_STATISTICS_ALL` 拿 **实际行 / A-Rows / E-Rows 偏差**；③ 10g 只能用 `DBMS_XPLAN.DISPLAY` 或 `DISPLAY_CURSOR`（10.2 起支持）；④ 图形化：节点对齐 + 成本颜色编码（红 > 黄 > 绿），点击节点看详细谓词信息 |
| 版本差异 | 10.1 不支持 `DISPLAY_CURSOR`；12c 新增 `DISPLAY_CURSOR(FORMAT=>'ALLSTATS LAST')` 和 `SQL_REPORT`（HTML） |

### F4. 执行计划瓶颈自动分析引擎

这是平台的核心差异化能力，把 DBA 经验规则化。按 **"可疑信号 → 诊断原因 → 严重等级 → 关联对象"** 输出诊断报告。

| 规则 ID | 可疑信号（执行计划 / 统计） | 可能原因 | 严重度 |
|---------|-----------------------------|----------|--------|
| R-01 | 大表 FULL，且过滤条件有可索引列 | 缺索引 / 索引被抑制（函数、隐式转换、OR） | 高 |
| R-02 | `TABLE ACCESS BY INDEX ROWID` 的回表比例 > 30% | 索引覆盖不足，需复合索引 | 中 |
| R-03 | Nested Loop 内层行数远超估算（E-Rows vs A-Rows 差 5×） | 基数估算偏差 / 统计陈旧 | 高 |
| R-04 | Hash Join 两边严重不平衡（1 : 100+） | Join 顺序问题 | 中 |
| R-05 | Sort 操作下 `disk` > 0 | `SORT_AREA_SIZE` 不足 / `pga_aggregate_target` 不够 | 低 |
| R-06 | 出现 `MERGE JOIN CARTESIAN` | 缺 Join 条件 / 隐式笛卡尔 | 极高 |
| R-07 | `CONNECT BY` 层级深 + 无索引 | 树形查询无索引 | 中 |
| R-08 | 谓词列有函数包裹（`TO_CHAR(col)`） | 函数抑制索引，需函数索引或改写 | 中 |
| R-09 | `CLUSTERING_FACTOR` 接近表行数 | 索引顺序与表存储顺序不匹配，回表代价高 | 中 |
| R-10 | 版本数 > 20（`V$SQL.CHILD_NUMBER`） | 缺绑定变量 / 游标共享问题 | 中 |
| R-11 | `PARSE_CALLS / EXECUTIONS` > 0.8 | 软解析率低，library cache 压力大 | 中 |
| R-12 | Top SQL 无统计信息（`DBA_TAB_STATISTICS.LAST_ANALYZED` 为空或 > 30 天） | 需 `DBMS_STATS.GATHER_*` | 高 |

> 引擎实现：规则按优先级遍历 `EXPLAIN PLAN` 结果 + 对象统计，命中即打标签，最终按严重度聚合。后续可扩展为"规则库 + SQL 模板"的可插拔架构。

### F5. 优化建议生成

将 F4 的诊断结果转化为 **可直接执行** 的优化动作，按"立即可用 → 需 DBA 审批"分层。

| 建议类型 | 示例 | 生成依据 | 风险等级 |
|----------|------|----------|----------|
| **Hint** | `/*+ INDEX(t idx_t_col1_col2) USE_NL(t1 t2) LEADING(t1) */` | R-01/R-03/R-04 命中 + 索引存在 | 低（仅作用于当前 SQL） |
| **Hint（反连接）** | `/*+ NO_INDEX(t) FULL(t) */` | 索引选择错误、全表扫描实际更优 | 低 |
| **复合索引** | `CREATE INDEX idx_order_cust_date ON order(cust_id, status, order_date);` | R-01/R-02 命中 + 列基数 + 查询谓词顺序 | 中（需 DBA 审批，生产窗口执行） |
| **函数索引** | `CREATE INDEX idx_log_time_func ON log(TRUNC(occur_time));` | R-08 命中 | 中 |
| **SQL 改写** | `OR → UNION ALL`、`IN → EXISTS`、子查询合并、标量子查询改 CASE 聚合 | R-06 + 模式识别 | 中（业务口径需确认） |
| **统计刷新** | `EXEC DBMS_STATS.GATHER_TABLE_STATS(...)` | R-12 命中 | 低 |
| **参数建议** | 调 `pga_aggregate_target` / `optimizer_index_cost_adj` / `cursor_sharing=FORCE` | R-05/R-10/R-11 | 高（影响全局） |

> 生成器实现：每个建议是一段可运行的 SQL（含 Hint 或 DDL），附带 **预期收益理由**（引用 F4 的哪条规则、哪个指标）和 **副作用说明**。Hint 生成基于 Oracle 官方 Hint 字典 + 经验映射表。

### F6. 优化后 SQL 沙箱运行

| 项目 | 说明 |
|------|------|
| 输入 | 原始 SQL + 建议应用后的 SQL（含 Hint 或改写）+ 运行参数（绑定变量样本、并发度、采样次数） |
| 输出 | 执行结果 + 实际执行计划 + 实际耗时/CPU/IO 指标 |
| 关键实现 | ① 必须走 **独立数据库用户**（`DBSQL_TUNER`），只授予 `CREATE SESSION`、`SELECT_CATALOG_ROLE`、目标表的 `SELECT` 权限；② 不允许 DDL 在沙箱执行（建议生成 DDL 后需要跳转 DBA 流程）；③ 用 `dbms_hprof.start_profiling` 或 `V$SQL_TIMING`（11g+）采集精确指标；④ 10g 用 `AUTOTRACE STATISTICS ONLY` 补齐耗时 |
| 版本差异 | 10g 无 `DBMS_HPROF`（可用 `DBMS_PROFILER` 但粒度不同）；11g 起支持 `V$SQL_TIMING` 和 `GATHER_PLAN_STATISTICS` Hint |

### F7. 优化前 / 优化后量化对比报告

| 项目 | 说明 |
|------|------|
| 输入 | F6 沙箱运行的两组结果 |
| 输出 | 并排对比卡片 + 雷达图 + 关键指标降幅表格 |
| 对比维度 | ① 单次执行耗时（秒）；② 总 CPU 时间；③ 逻辑读（buffer gets）；④ 物理读（disk reads）；⑤ 执行计划节点数；⑥ Full Scan 表数；⑦ 索引回表比例；⑧ 成本估算（10g/11g 统一 `OPTIMIZER_COST`） |
| 关键实现 | 同一份绑定变量、同一条 SQL、同一会话级别参数（用 `ALTER SESSION` 固定），保证 apples-to-apples；必要时多次运行取中位数，避免 buffer cache 冷/热差异 |

**对比报告示例字段**：

| 指标 | 优化前 | 优化后 | 降幅 |
|------|--------|--------|------|
| 单次耗时 | 12.4s | 1.8s | **-85.5%** |
| 逻辑读 | 284,000 | 12,300 | -95.7% |
| 物理读 | 12,100 | 0 | -100% |
| Full Scan | 3 | 0 | - |
| 回表比例 | 42% | 8% | - |
| 执行计划节点 | 17 | 9 | - |

### F8. 优化历史与可审计追踪

| 项目 | 说明 |
|------|------|
| 功能 | 每条 SQL 的每次优化尝试（建议、运行结果、对比报告、执行人、时间）都入库；支持按 SQL_ID、对象、执行人、时间倒查；支持把 Hint 固化为 SQL Profile（11g+ `DBMS_SQLPROF`）或 SQL Patch（12c+） |
| 版本差异 | 10g 无 SQL Profile，只能走 Hint 注入（应用层或 Oracle Outline）；11g 有 `DBMS_SQLPROF`；12c+ 有 `DBMS_SQL_PATCH` |

### F9. 集成诊断辅助（可选增强）

- **锁阻塞快照**：`V$LOCK` + `V$SESSION`，定位慢 SQL 是否被锁
- **执行等待事件**：`V$SESSION_WAIT` Top 5 等待（db file sequential read / log file sync / latch free）
- **ASM / IO 诊断**：`V$ASM_DISK_IOSTAT`（若用 ASM）
- **参数快照**：`V$PARAMETER` 关键参数（optimizer_*、sga_*、pga_*、cursor_sharing）

---

## 3. 技术架构（概要）

```
┌─────────────────────────────────────────────────────────────────┐
│                       Web UI (React)                           │
│  Top SQL 列表 │ 对象画像 │ 执行计划图 │ 诊断报告 │ 对比面板     │
└────────────────────────────┬────────────────────────────────────┘
                             │ REST API / gRPC
┌────────────────────────────▼────────────────────────────────────┐
│                后端服务 (Go / Python FastAPI)                    │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐ │
│  │ F1 Top  │ │ F2 对象  │ │ F3 执行  │ │ F4 规则  │ │ F5.. │ │
│  │ SQL 采集│ │ 画像采集 │ │ 计划获取 │ │ 引擎     │ │ F8   │ │
│  └────┬────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └──┬───┘ │
│       │           │            │            │          │      │
│  ┌────▼───────────▼────────────▼────────────▼──────────▼───┐  │
│  │                    Oracle Driver 池化                    │  │
│  │  - 10g: cx_Oracle (Python) / godror (Go)                │  │
│  │  - 11g+: OCI / Thin Mode                                │  │
│  │  - 只读账号 + 沙箱账号分离                               │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  元数据存储：SQLite / PostgreSQL（存 Top SQL 列表、建议历史）   │
└─────────────────────────────────────────────────────────────────┘
```

**部署模式**：
- **SaaS / 集中式**：平台自身连各目标 Oracle 实例（需只读账号），集中管理
- **Agent 模式**：目标 Oracle 侧部署轻量 Agent，平台通过 Agent 拿数据（适合网络隔离环境）

---

## 4. 关键技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| Oracle 驱动 | Go 端用 `godror`；Python 端用 `oracledb`（Thick/Thin 自适应） | 10g 必须 Thick 模式（Thin 从 12.1 起） |
| 执行计划获取 | 主路径用 `V$SQL_PLAN`（SQL_ID + CHILD_NUMBER），兜底 `EXPLAIN PLAN INTO` | 避免硬解析生产 SQL |
| Top SQL 历史 | AWR（DBA_HIST_SQLSTAT）为主，ASH（V$ACTIVE_SESSION_HISTORY）补实时 | 两者互补，AWR 有聚合、ASH 有事件 |
| 沙箱隔离 | 独立 schema + 纯 SELECT + ALTER SESSION 固定参数 | 严禁影响生产执行计划 |
| 规则引擎 | 硬编码 Python 规则 + JSON 配置 | 初期够用，后续接 Drools/QLExpress |
| SQL 改写 | 内置 20 条常见改写模式（OR→UNION ALL、标量子查询→CASE 聚合等） | 覆盖 80% 场景，特殊 case 提示人工介入 |

---

## 5. 安全与权限模型

| 角色 | 权限 |
|------|------|
| 只读采集账号 | `SELECT_CATALOG_ROLE` + 目标表 `SELECT` + `EXECUTE ANY PROCEDURE`（`DBMS_XPLAN`/`DBMS_STATS`） |
| 沙箱执行账号 | 独立 schema，继承只读采集 + 允许 `EXPLAIN PLAN` / `ALTER SESSION`；禁用 DDL/DML |
| DBA 审批角色 | 可发起 `CREATE INDEX` / `DBMS_STATS` / SQL Profile 创建 |
| 普通观察员 | 只读所有分析结果，无法触发沙箱执行 |

---

## 6. 里程碑（建议 6 周 MVP）

| 周 | 交付物 |
|----|--------|
| W1 | F1 Top SQL 定位（AWR + V$SQL）+ Oracle 连接池 + 权限梳理 |
| W2 | F2 对象画像 + F3 执行计划获取与可视化（树状图） |
| W3 | F4 规则引擎（R-01 ~ R-12 首批）+ F5 Hint/索引建议生成器 |
| W4 | F6 沙箱运行 + F7 前/后量化对比（卡片 + 雷达图） |
| W5 | F8 历史审计 + F9 锁/等待事件辅助 + 前端 UI 整合 |
| W6 | 10g/11g/12c 三版本环境回归测试 + 灰度上线 + 运维手册 |

---

## 7. 后续演进方向（不在首版范围）

- [ ] 全库 Top SQL 自动巡检 + 日报推送
- [ ] ML 模型：基于历史 Top SQL 预测未来慢 SQL
- [ ] 慢 SQL 指纹聚类（把结构相似的 SQL 归为一类，一次优化覆盖多条）
- [ ] SQL Profile / Outline 自动下发与灰度
- [ ] 多数据库扩展（MySQL / PostgreSQL 的 Oracle 风格适配层）

---

## 附：Oracle 版本差异速查（首版需兼容）

| 能力 | 10g R2 | 11g | 12c+ |
|------|--------|-----|------|
| SQL_ID | ❌ 用 HASH_VALUE | ✅ | ✅ |
| V$SQL_PLAN_STATISTICS_ALL | ❌ | ✅ | ✅ |
| DBMS_XPLAN.DISPLAY_CURSOR | ⚠️ 10.2+ | ✅ | ✅ |
| SQL Profile | ❌ | ✅ | ✅ |
| SQL Patch | ❌ | ❌ | ✅ |
| Real-Time SQL Monitor | ❌ | ✅（需 Tuning Pack） | ✅ |
| GATHER_PLAN_STATISTICS Hint | ❌ | ✅ | ✅ |
| SELECT 隐式结果集 | ❌ | ❌ | ✅ |
| EXPLAIN PLAN INTO 自动管理 | ✅ | ✅ | ✅ |
