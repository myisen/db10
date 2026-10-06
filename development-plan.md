# DBSQL-Turner 开发计划（Execution Plan）

> 基于 [project.md](file:///workspace/project.md) 功能说明制定
> 文档性质：**可执行的开发任务拆解**，按里程碑逐步推进
> 编号规则：`M{里程碑}-{模块}{序号}`，如 `M1-DBCONN-01`
> 任务粒度：单个任务 = 1 名开发人员 1~3 个工作日可完成并自测

---

## 0. 总览

### 0.1 技术栈定型

| 层 | 选型 | 说明 |
|----|------|------|
| 后端语言 | **Python 3.11 + FastAPI** | 规则引擎、SQL 文本解析、与 Oracle 生态贴合；AsyncIO 适合 IO 密集型采集 |
| Oracle 驱动 | `oracledb` (Thick 模式，需 19c+ Client) | Thick 模式兼容 10g；Thin 仅 12.1+，首版统一 Thick |
| 规则引擎 | 自研轻量规则框架（规则 = Python 函数 + 元数据） | 不引入 Drools，避免学习成本；规则可热加载 |
| 元数据存储 | **PostgreSQL 14**（开发期可用 SQLite） | 存 Top SQL 快照、优化历史、建议、对比结果 |
| 前端 | **React 18 + TypeScript + Ant Design 5** | 执行计划树用 `react-flow` 或自研树组件 |
| 图表 | ECharts | 对比雷达图、执行耗时趋势 |
| 部署 | Docker Compose（后端 + 前端 + PG） | 首版单体，后续按 Agent 模式拆分 |

### 0.2 里程碑总览

| 里程碑 | 周期 | 核心交付 | 对应功能 |
|--------|------|----------|----------|
| **M1 基建** | W1 | 项目骨架、Oracle 连接池、元数据 DB、F1 Top SQL 采集 | F1 |
| **M2 诊断** | W2~W3 | F2 对象画像、F3 执行计划获取与可视化 | F2、F3 |
| **M3 智能** | W3~W4 | F4 规则引擎、F5 建议生成器 | F4、F5 |
| **M4 验证** | W4~W5 | F6 沙箱执行、F7 量化对比 | F6、F7 |
| **M5 闭环** | W5 | F8 历史审计、F9 辅助诊断、前端整合 | F8、F9 |
| **M6 上线** | W6 | 多版本回归、灰度、运维手册 | 全部 |

---

## 1. M1 — 基建 + Top SQL 定位

### 1.1 项目骨架

| 任务 ID | 任务 | 依赖 | 交付 |
|---------|------|------|------|
| M1-STRUCT-01 | FastAPI 项目初始化（目录结构、配置中心 `pydantic-settings`、日志 `loguru`、异常中间件） | — | 可启动的空服务，`/health` 可访问 |
| M1-STRUCT-02 | PostgreSQL 接入（`SQLAlchemy 2.0` 异步 + `alembic` 迁移） + 开发期 SQLite 兼容层 | M1-STRUCT-01 | 建表脚本 + 迁移框架 |
| M1-STRUCT-03 | 前端骨架（Vite + React + TS + AntD + 路由 + 请求封装 `axios`） | — | 可访问的空白首页 |

### 1.2 Oracle 连接层（核心基础设施，所有模块依赖）

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M1-DBCONN-01 | Oracle 连接配置模型（host/port/service/user/pwd/role/version）+ 多实例管理 | M1-STRUCT-01 | `oracle_instances` 表 + CRUD API |
| M1-DBCONN-02 | 连接池封装（`oracledb.create_pool`，min=2/max=20，按实例隔离） | M1-DBCONN-01 | `OracleConnectionManager` 单例，支持 `get_conn(instance_id)` |
| M1-DBCONN-03 | 连接探活 + 版本探测（`SELECT * FROM v$version`，解析出版本号 10g/11g/12c/19c） | M1-DBCONN-02 | 实例详情含 `oracle_version` 字段，用于后续版本分支 |
| M1-DBCONN-04 | 只读权限校验（连接后检查 `SELECT_CATALOG_ROLE`、目标表 SELECT 权限） | M1-DBCONN-02 | API 返回权限校验结果，缺失权限时明确提示 |

### 1.3 F1 Top SQL 定位

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M1-F1-01 | **实时 Top SQL 采集器**（11g+） | M1-DBCONN-03 | 查询 `GV$SQL`，字段：SQL_ID/EXECUTIONS/ELAPSED_TIME/CPU_TIME/BUFFER_GETS/DISK_READS/PARSE_CALLS/CHILD_NUMBER/FIRST_LOAD_TIME/MODULE/ACTION；10g 降级到 `V$SQLAREA` |
| M1-F1-02 | **历史 Top SQL 采集器**（AWR） | M1-DBCONN-03 | `DBA_HIST_SQLSTAT` + `DBA_HIST_SNAPSHOT`，按 snap_id 聚合，支持时间窗口过滤 |
| M1-F1-03 | **排序口径统一** | M1-F1-01 | 排序指标 = `avg(ELAPSED_TIME/EXECUTIONS) * EXECUTIONS`（总耗时优先），同时暴露 CPU/逻辑读/物理读/执行次数四个维度供切换 |
| M1-F1-04 | **SQL 文本获取** | M1-F1-01 | `DBA_HIST_SQLTEXT` / `V$SQLAREA.SQL_TEXT`，截断处理（>4000 字符存 CLOB） |
| M1-F1-05 | **Top SQL 列表 API + 前端页面** | M1-F1-01~04 | `GET /api/v1/top-sql?instance_id=&window=&metric=&limit=`；前端表格 + 指标切换 Tab + 时间范围选择器 |

**M1 验收口径**：能在 11g/12c 环境拉到 Top 20 SQL 列表，10g 环境能降级拉到 `V$SQLAREA` 数据；前端能按四个指标排序切换。

---

## 2. M2 — 对象画像 + 执行计划

### 2.1 F2 基础对象统计信息采集

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M2-F2-01 | **SQL 依赖对象解析** | M1-DBCONN-03 | 优先方案：对 SQL 执行 `EXPLAIN PLAN INTO plan_table`，从 `PLAN_TABLE` 的 `OBJECT_OWNER/OBJECT_NAME/OBJECT_TYPE` 抽取所有表/索引；兜底：正则解析 FROM/JOIN 子句（精度低，仅兜底） |
| M2-F2-02 | **表画像采集** | M2-F2-01 | `DBA_TABLES`：OWNER/TABLE_NAME/NUM_ROWS/BLOCKS/EMPTY_BLOCKS/AVG_ROW_LEN/LAST_ANALYZED/PARTITIONED；分区数从 `DBA_TAB_PARTITIONS` COUNT |
| M2-F2-03 | **索引画像采集** | M2-F2-01 | `DBA_INDEXES`：INDEX_NAME/TABLE_NAME/UNIQUENESS/LEAF_BLOCKS/DISTINCT_KEYS/CLUSTERING_FACTOR/INDEX_TYPE/LAST_ANALYZED；列序列从 `DBA_IND_COLUMNS` |
| M2-F2-04 | **列画像采集** | M2-F2-01 | `DBA_TAB_COLUMNS`：COLUMN_NAME/DATA_TYPE/NUM_NULLS/NUM_DISTINCT/HISTOGRAM；直方图桶数从 `DBA_TAB_HISTOGRAMS` COUNT |
| M2-F2-05 | **索引使用情况** | M2-F2-03 | 10g/11g：`V$OBJECT_USAGE`（需先 `ALTER INDEX ... MONITORING USAGE`，只读账号可能无权限，降级为 `DBA_INDEXES` 不返回使用态）；12c+：`DBA_INDEXES.USE` |
| M2-F2-06 | **统计陈旧判断** | M2-F2-02 | 11g+：`DBA_TAB_MODIFICATIONS` 的 `INSERTS+UPDATES+DELETES / NUM_ROWS > 10%` 或 `LAST_ANALYZED > 30天`；10g：仅判断 `LAST_ANALYZED` 时间 |
| M2-F2-07 | **对象画像 API + 前端卡片** | M2-F2-02~06 | `GET /api/v1/object-profile?instance_id=&sql_id=`；前端按"表 / 索引 / 列"分 Tab，索引卡片高亮 `CLUSTERING_FACTOR` 接近行数的情况 |

### 2.2 F3 执行计划获取与可视化

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M3-F3-01 | **估算执行计划获取** | M1-DBCONN-03 | `EXPLAIN PLAN SET STATEMENT_ID=:stmt FOR :sql` → `DBMS_XPLAN.DISPLAY('PLAN_TABLE', :stmt, 'ALL')`；解析为结构化 JSON（id/parent_id/operation/options/object_owner/object_name/cardinality/bytes/cost/time/access_predicates/filter_predicates） |
| M3-F3-02 | **实际执行计划获取（11g+）** | M3-F3-01 | 若 SQL 在 `V$SQL`：`DBMS_XPLAN.DISPLAY_CURSOR(sql_id=>:sql_id, format=>'ALLSTATS LAST')`，解析 `A-Rows`/`E-Rows`/`Buffers`/`Reads` |
| M3-F3-03 | **10g 执行计划兼容** | M3-F3-01 | 10.2 用 `DISPLAY_CURSOR`（需 `STATISTICS_LEVEL=ALL`），10.1 只能 `DISPLAY`；版本分支处理 |
| M3-F3-04 | **执行计划树状可视化** | M3-F3-01 | 前端用 `react-flow` 或自研：节点按 `parent_id` 构建树，横向排列；节点显示 operation + cost；cost 颜色编码（>50% 总成本红，>20% 黄，其余绿）；点击节点展开谓词详情 |
| M3-F3-05 | **E-Rows vs A-Rows 偏差高亮** | M3-F3-02 | 若 `A-Rows / E-Rows > 5` 或 `< 0.2`，节点标注"基数偏差"并链接到 F4 规则 R-03 |

**M2 验收口径**：输入任意 SQL，能拿到表/索引/列三层画像，能展示树状执行计划，11g+ 能看到实际行 vs 估算行偏差。

---

## 3. M3 — 规则引擎 + 建议生成

### 3.1 F4 规则引擎（核心差异化）

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M4-RULE-01 | **规则框架** | M3-F3-01 | 定义 `BaseRule` 抽象类：`rule_id`/`name`/`severity`/`description`/`match(plan_nodes, object_stats) -> List[Finding]`；规则注册用装饰器 `@register_rule` |
| M4-RULE-02 | **R-01 全表扫描检测** | M4-RULE-01 | 遍历 plan_nodes，命中 `TABLE ACCESS FULL` 且表 `NUM_ROWS > 10000` 且 filter_predicates 中有可索引列（从列画像取有索引的列对比 filter 中的列） → 命中 |
| M4-RULE-03 | **R-02 回表比例过高** | M4-RULE-01 | `TABLE ACCESS BY INDEX ROWID` 节点：`A-Rows / index_range_scan_A-Rows > 0.3`；10g 无 A-Rows 时用 `CARDINALITY` 估算 |
| M4-RULE-04 | **R-03 基数估算偏差** | M4-RULE-01 | 11g+：`A-Rows / E-Rows > 5 or < 0.2`；关联 `LAST_ANALYZED` 判断是否统计陈旧 |
| M4-RULE-05 | **R-04 Join 不平衡** | M4-RULE-01 | `HASH JOIN` / `NESTED LOOPS` 两个子节点 `CARDINALITY` 比值 > 100 → Join 顺序建议 |
| M4-RULE-06 | **R-05 Sort 落盘** | M4-RULE-01 | 11g+ 解析 `TempSpc` > 0 或 `DISK` > 0（从 `V$SQL_PLAN_STATISTICS_ALL`）→ 建议调 PGA |
| M4-RULE-07 | **R-06 笛卡尔积检测** | M4-RULE-01 | 命中 `MERGE JOIN CARTESIAN` 操作 → 极高严重度 |
| M4-RULE-08 | **R-07 CONNECT BY 无索引** | M4-RULE-01 | `CONNECT BY` 操作且相关表无对应索引 |
| M4-RULE-09 | **R-08 函数抑制索引** | M4-RULE-01 | filter_predicates 正则匹配 `\w+\(\s*\w+\s*\)`（列上有函数）→ 建议函数索引或改写 |
| M4-RULE-10 | **R-09 聚簇因子异常** | M4-RULE-01 | 索引 `CLUSTERING_FACTOR` 接近 `NUM_ROWS`（> 80%）且索引被使用 → 提示回表代价高 |
| M4-RULE-11 | **R-10 游标版本爆炸** | M3-F3-01 | `V$SQL.CHILD_NUMBER` 计数 > 20 → 建议绑定变量 / `cursor_sharing` |
| M4-RULE-12 | **R-11 软解析率低** | M3-F3-01 | `PARSE_CALLS / EXECUTIONS > 0.8` |
| M4-RULE-13 | **R-12 统计信息缺失/陈旧** | M2-F2-02 | `LAST_ANALYZED IS NULL` 或 > 30 天，或 `DBA_TAB_MODIFICATIONS` 变更率 > 10% |
| M4-RULE-14 | **规则引擎聚合 + 诊断报告 API** | M4-RULE-02~13 | `GET /api/v1/diagnose?instance_id=&sql_id=` 返回 `Findings[]`，按严重度排序 |

### 3.2 F5 优化建议生成

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M5-ADV-01 | **建议生成器框架** | M4-RULE-14 | `Suggestion` 结构：`type`/`sql`/`reason`/`risk_level`/`related_rules[]`；每个 Finding 可映射 0~N 条 Suggestion |
| M5-ADV-02 | **Hint 生成器** | M5-ADV-01 | R-01 → `/*+ INDEX(t idx_name) */`（从现有索引中选最优）；R-03/R-04 → `USE_NL`/`USE_HASH`/`LEADING`；R-01反 → `NO_INDEX`/`FULL`；Hint 字典内置常用 Hint 及语法校验 |
| M5-ADV-03 | **复合索引建议生成器** | M5-ADV-01 | R-01/R-02 命中：收集谓词列，按"等值列在前、范围列在后、基数高的等值列优先"排序，生成 `CREATE INDEX idx_<table>_<cols> ON <owner>.<table>(<cols>)`；附 `CLUSTERING_FACTOR` 风险提示 |
| M5-ADV-04 | **函数索引建议生成器** | M5-ADV-01 | R-08 命中：生成 `CREATE INDEX idx_<table>_<func> ON <owner>.<table>(<func(col)>)` |
| M5-ADV-05 | **SQL 改写建议生成器** | M5-ADV-01 | 内置改写模式库（首版 5 条）：①`OR` → `UNION ALL`；②标量子查询 → `CASE WHEN` 聚合；③`IN` → `EXISTS`（相关子查询场景）；④`NOT IN` → `LEFT JOIN ... IS NULL`；⑤子查询合并。每条模式给出 before/after 模板 + 适用条件 |
| M5-ADV-06 | **统计刷新建议** | M5-ADV-01 | R-12 命中 → `EXEC DBMS_STATS.GATHER_TABLE_STATS(ownname=>:owner, tabname=>:table, cascade=>TRUE, estimate_percent=>DBMS_STATS.AUTO_SAMPLE_SIZE)` |
| M5-ADV-07 | **参数建议** | M5-ADV-01 | R-05 → `pga_aggregate_target`；R-10/R-11 → `cursor_sharing=FORCE`（标注全局影响，高风险） |
| M5-ADV-08 | **建议 API + 前端展示** | M5-ADV-02~07 | `GET /api/v1/suggestions?instance_id=&sql_id=`；前端按风险等级分色，每条建议可"复制 SQL"或"发送到沙箱执行" |

**M3 验收口径**：对一条已知慢 SQL（如大表全表扫描+缺索引），规则引擎能命中 R-01/R-12，建议生成器能给出可用的 Hint + CREATE INDEX 建议。

---

## 4. M4 — 沙箱执行 + 量化对比

### 4.1 F6 优化后 SQL 沙箱运行

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M6-SBX-01 | **沙箱账号独立连接池** | M1-DBCONN-02 | 实例配置增加 `sandbox_user`/`sandbox_password` 字段；连接池独立，与只读池隔离 |
| M6-SBX-02 | **SQL 安全校验** | M6-SBX-01 | 白名单：只允许 `SELECT` / `EXPLAIN PLAN` / `ALTER SESSION`；拒绝 `INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/TRUNCATE/GRANT`；多语句检测（`;` 分隔多条只执行第一条或全拒） |
| M6-SBX-03 | **会话级参数锁定** | M6-SBX-01 | 执行前 `ALTER SESSION SET statistics_level=ALL`（11g+ 拿 A-Rows）、`workarea_size_policy=AUTO`；执行后 `ALTER SESSION RESET`；保证每次执行环境一致 |
| M6-SBX-04 | **执行 + 指标采集** | M6-SBX-02/03 | 执行 SQL 前记录开始时间；执行后从 `V$SQL` 取最近一条（`SQL_ID` + `LAST_ACTIVE_TIME` 匹配）：ELAPSED_TIME/CPU_TIME/BUFFER_GETS/DISK_READS/ROWS_PROCESSED；10g 用 `V$SQLAREA` + 客户端计时兜底 |
| M6-SBX-05 | **实际执行计划采集** | M6-SBX-03 | 11g+：`DBMS_XPLAN.DISPLAY_CURSOR(sql_id, NULL, 'ALLSTATS LAST')`；10g：`EXPLAIN PLAN` 兜底 |
| M6-SBX-06 | **多次执行取中位数** | M6-SBX-04 | 支持 `runs=N` 参数（默认 3），取耗时中位数，避免冷缓存抖动；第一次不计入（预热） |
| M6-SBX-07 | **沙箱执行 API** | M6-SBX-04~06 | `POST /api/v1/sandbox/run` body: `{instance_id, sql, bind_vars?, runs, timeout_sec}` → 返回 `{elapsed_ms, cpu_ms, buffer_gets, disk_reads, rows_processed, plan: [...]}` |

### 4.2 F7 优化前 / 优化后量化对比

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M7-CMP-01 | **对比任务编排** | M6-SBX-07 | 输入：`{original_sql, optimized_sql, instance_id, bind_vars, runs}`；依次执行两条 SQL（同一会话参数、同一绑定变量），返回两组指标 |
| M7-CMP-02 | **对比指标计算** | M7-CMP-01 | 计算 8 维指标的 before/after/降幅：耗时、CPU、逻辑读、物理读、计划节点数、Full Scan 数、回表比例、OPTIMIZER_COST |
| M7-CMP-03 | **对比报告生成** | M7-CMP-02 | `GET /api/v1/compare/:task_id` 返回结构化对比数据 + 降幅百分比 |
| M7-CMP-04 | **前端对比面板** | M7-CMP-03 | 并排卡片（before | after | 降幅）+ ECharts 雷达图（8 维归一化）+ 关键指标表格；降幅 > 50% 绿色高亮，< 0（劣化）红色警示 |

**M4 验收口径**：同一条 SQL 的原始版和 Hint 版，在沙箱中对比能输出 8 维降幅，耗时降幅与手工 `DBMS_XPLAN` 一致。

---

## 5. M5 — 历史审计 + 辅助诊断 + 整合

### 5.1 F8 优化历史与审计

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M8-HIS-01 | **优化记录表** | M1-STRUCT-02 | `optimization_records`：id/instance_id/sql_id/sql_text/suggestions(JSON)/before_metrics(JSON)/after_metrics(JSON)/operator/created_at/status |
| M8-HIS-02 | **对比结果持久化** | M7-CMP-03 | 对比完成后自动写入 `optimization_records` |
| M8-HIS-03 | **历史查询 API** | M8-HIS-01 | `GET /api/v1/history?instance_id=&sql_id=&operator=&time_range=` 分页返回 |
| M8-HIS-04 | **历史详情页** | M8-HIS-03 | 展示某次优化的完整链路：原始 SQL → 诊断报告 → 建议列表 → 前后对比 |
| M8-HIS-05 | **SQL Profile 固化（11g+）** | M5-ADV-02 | 提供 `POST /api/v1/sql-profile` 调用 `DBMS_SQLTUNE.IMPORT_SQL_PROFILE`（需 DBA 审批流，首版只生成脚本不自动执行）；10g 生成 Outline 创建脚本 |

### 5.2 F9 集成诊断辅助

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M8-AUX-01 | **锁阻塞快照** | M1-DBCONN-03 | `V$LOCK` + `V$SESSION`：返回阻塞链（blocker → waiter），含 SID/SERIAL#/SQL_ID/等待事件 |
| M8-AUX-02 | **等待事件 Top 5** | M1-DBCONN-03 | `V$SESSION_WAIT` 聚合 `EVENT`，取 COUNT Top 5；对 db file sequential read / log file sync / latch free 给出解读 |
| M8-AUX-03 | **关键参数快照** | M1-DBCONN-03 | `V$PARAMETER`：optimizer_mode/optimizer_index_cost_adj/cursor_sharing/pga_aggregate_target/sga_target/statistics_level |
| M8-AUX-04 | **辅助诊断面板** | M8-AUX-01~03 | 在 SQL 详情页侧栏展示锁/等待/参数，帮助判断慢 SQL 是否非 SQL 本身问题 |

### 5.3 前端整合

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M8-UI-01 | **SQL 详情页（主工作台）** | 全部 | 一站式页面：左侧 SQL 文本 + 执行计划树；右侧对象画像 + 诊断报告 + 建议列表；底部对比面板 |
| M8-UI-02 | **实例管理页** | M1-DBCONN-01 | 增删改查 Oracle 实例配置 + 连接测试 |
| M8-UI-03 | **历史记录页** | M8-HIS-03 | 优化历史列表 + 详情跳转 |

---

## 6. M6 — 回归 + 上线

| 任务 ID | 任务 | 依赖 | 关键实现要点 |
|---------|------|------|--------------|
| M9-REG-01 | **10g 环境回归** | 全部 | 搭建 Oracle 10.2 测试实例，验证 F1~F9 所有降级路径（无 SQL_ID、无 DISPLAY_CURSOR、无 SQL Profile 等） |
| M9-REG-02 | **11g 环境回归** | 全部 | 验证 11g 特性（SQL_ID、SQL Profile、V$SQL_PLAN_STATISTICS_ALL） |
| M9-REG-03 | **12c/19c 环境回归** | 全部 | 验证 12c 特性（SQL Patch、Thin 驱动可选） |
| M9-REG-04 | **压测** | 全部 | 模拟 50 并发 Top SQL 查询 + 沙箱执行，验证连接池稳定性 |
| M9-REL-01 | **Docker Compose 部署包** | 全部 | `docker-compose.yml`：后端 + 前端 + PostgreSQL；一键启动 |
| M9-REL-02 | **运维手册** | 全部 | 安装步骤、Oracle 账号权限脚本、配置说明、常见问题 |

---

## 7. 平台元数据数据库设计

### 7.1 表清单

| 表名 | 用途 |
|------|------|
| `oracle_instances` | Oracle 实例连接配置 |
| `top_sql_snapshots` | Top SQL 快照（每次采集存一份） |
| `object_profiles` | 对象画像缓存（表/索引/列统计） |
| `execution_plans` | 执行计划缓存（JSON） |
| `diagnosis_findings` | 诊断结果（规则命中记录） |
| `suggestions` | 优化建议 |
| `sandbox_runs` | 沙箱执行记录 |
| `comparisons` | 前后对比记录 |
| `optimization_records` | 优化历史（主表） |
| `users` | 平台用户 |
| `roles` | 角色（观察员/DBA/管理员） |

### 7.2 关键表结构（DDL 概要）

```sql
-- Oracle 实例
CREATE TABLE oracle_instances (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    host            VARCHAR(255) NOT NULL,
    port            INTEGER NOT NULL DEFAULT 1521,
    service_name    VARCHAR(100) NOT NULL,
    read_user       VARCHAR(100) NOT NULL,
    read_password   VARCHAR(255) NOT NULL,  -- 加密存储
    sandbox_user    VARCHAR(100),
    sandbox_password VARCHAR(255),
    oracle_version  VARCHAR(20),             -- 自动探测
    status          VARCHAR(20) DEFAULT 'unknown',  -- online/offline/unknown
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Top SQL 快照
CREATE TABLE top_sql_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    instance_id     BIGINT REFERENCES oracle_instances(id),
    snapshot_time   TIMESTAMP NOT NULL,
    sql_id          VARCHAR(13),             -- 10g 用 HASH_VALUE 填充
    hash_value      NUMBER,
    sql_text        TEXT,
    executions      BIGINT,
    elapsed_time    NUMBER,                  -- 微秒
    cpu_time        NUMBER,
    buffer_gets     BIGINT,
    disk_reads      BIGINT,
    parse_calls     BIGINT,
    child_number    INTEGER,
    module          VARCHAR(64),
    first_load_time DATE,
    metric_rank     INTEGER                  -- 采集时的排名
);

-- 优化历史（主表）
CREATE TABLE optimization_records (
    id              BIGSERIAL PRIMARY KEY,
    instance_id     BIGINT REFERENCES oracle_instances(id),
    sql_id          VARCHAR(13),
    original_sql    TEXT NOT NULL,
    optimized_sql   TEXT,
    findings        JSONB,                   -- F4 诊断结果
    suggestions     JSONB,                   -- F5 建议列表
    before_metrics  JSONB,                   -- F7 优化前指标
    after_metrics   JSONB,                   -- F7 优化后指标
    improvement_pct NUMERIC(5,2),            -- 综合降幅
    operator        VARCHAR(100),
    status          VARCHAR(20),             -- pending/running/done/failed
    created_at      TIMESTAMP DEFAULT NOW(),
    finished_at     TIMESTAMP
);
```

---

## 8. REST API 总览

| 方法 | 路径 | 说明 | 模块 |
|------|------|------|------|
| GET | `/api/v1/instances` | 实例列表 | 基建 |
| POST | `/api/v1/instances` | 新增实例 | 基建 |
| POST | `/api/v1/instances/:id/test` | 连接测试 | 基建 |
| GET | `/api/v1/top-sql` | Top SQL 列表 | F1 |
| GET | `/api/v1/object-profile` | 对象画像 | F2 |
| GET | `/api/v1/execution-plan` | 执行计划 | F3 |
| GET | `/api/v1/diagnose` | 诊断报告 | F4 |
| GET | `/api/v1/suggestions` | 优化建议 | F5 |
| POST | `/api/v1/sandbox/run` | 沙箱执行 | F6 |
| POST | `/api/v1/compare` | 发起对比 | F7 |
| GET | `/api/v1/compare/:id` | 对比结果 | F7 |
| GET | `/api/v1/history` | 优化历史 | F8 |
| GET | `/api/v1/history/:id` | 历史详情 | F8 |
| GET | `/api/v1/aux/locks` | 锁阻塞 | F9 |
| GET | `/api/v1/aux/wait-events` | 等待事件 | F9 |
| GET | `/api/v1/aux/params` | 参数快照 | F9 |

---

## 9. 关键模块接口定义

### 9.1 规则引擎接口

```python
# app/rules/base.py
from dataclasses import dataclass
from enum import Enum
from typing import List

class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

@dataclass
class Finding:
    rule_id: str
    rule_name: str
    severity: Severity
    description: str
    object_name: str | None = None
    evidence: dict | None = None  # 命中证据，如 E-Rows/A-Rows

class BaseRule:
    rule_id: str
    name: str
    severity: Severity
    description: str

    def match(self, plan_nodes: list, object_stats: dict) -> List[Finding]:
        raise NotImplementedError

# 规则注册
_rules = {}
def register_rule(cls):
    _rules[cls.rule_id] = cls()
    return cls

def run_all_rules(plan_nodes, object_stats) -> List[Finding]:
    findings = []
    for rule in _rules.values():
        findings.extend(rule.match(plan_nodes, object_stats))
    return sorted(findings, key=lambda f: Severity[f.severity.upper()].value)
```

### 9.2 沙箱执行器接口

```python
# app/sandbox/executor.py
from dataclasses import dataclass

@dataclass
class SandboxResult:
    elapsed_ms: float
    cpu_ms: float
    buffer_gets: int
    disk_reads: int
    rows_processed: int
    plan: list          # 结构化执行计划
    error: str | None = None

class SandboxExecutor:
    def __init__(self, instance_id: str):
        self.instance_id = instance_id

    async def run(self, sql: str, bind_vars: dict = None,
                  runs: int = 3, timeout_sec: int = 300) -> SandboxResult:
        # 1. 安全校验（白名单）
        # 2. 锁定会话参数
        # 3. 执行 runs 次，取中位数
        # 4. 采集 V$SQL 指标
        # 5. 采集实际执行计划
        ...
```

---

## 10. 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 10g 环境无 SQL_ID、无 A-Rows | F1/F3/F4 降级逻辑复杂 | M1 先搭版本探测框架，所有采集器按版本分支；10g 用 HASH_VALUE 替代 SQL_ID，用 CARDINALITY 替代 A-Rows |
| 只读账号权限不足（无法 MONITORING USAGE、无法 V$OBJECT_USAGE） | F2 索引使用态缺失 | 检测到权限不足时降级为"未知"并在前端标注，不阻塞主流程 |
| 沙箱执行影响生产（误执行 DML） | 安全事故 | M6-SBX-02 白名单 + 独立账号 + 审计日志；上线前渗透测试 |
| 规则误报（把正常计划标为异常） | 降低可信度 | 每条规则带 `evidence`，前端展示命中依据；首版规则阈值可配置 |
| 大 SQL 文本（>4000 字符） | 存储/展示问题 | TEXT/CLOB 存储，前端折叠展示 |
| Oracle 驱动 Thick 模式需 Client 库 | 部署复杂度 | Docker 镜像内置 Oracle Instant Client 19c（兼容向下连接 10g） |

---

## 11. 开发完成定义（DoD）

每个任务完成需满足：

1. 代码合入主干，通过 CI（lint + 单测）
2. 对应 API 可在 Postman / Swagger 调通
3. 前端页面（如有）可操作，无控制台报错
4. 在 11g 测试环境验证通过；涉及版本差异的在 10g/12c 各验证一次
5. 关键逻辑有单元测试（规则引擎、SQL 解析、指标计算）
6. 代码有必要的注释，复杂逻辑说明"为什么这样做"

---

## 12. 后续推进顺序（建议）

按里程碑顺序推进，每个里程碑结束做一次演示评审：

1. **立即启动**：M1-STRUCT、M1-DBCONN（基建先行，所有模块依赖）
2. **M1 完成后**：M2-F2、M3-F3（诊断数据基础）
3. **M2 完成后**：M4-RULE（规则引擎，核心）
4. **M3 完成后**：M5-ADV（建议生成）
5. **M4 完成后**：M6-SBX、M7-CMP（验证闭环）
6. **M5 完成后**：M8-HIS、M8-AUX、M8-UI（审计 + 整合）
7. **最后**：M9 回归 + 上线

> 每个里程碑内部任务可并行（如 F2 的表/索引/列采集可三人并行），跨里程碑严格串行。
