# SQLOpt — SQL 历史性能分析 & 在线优化系统

> 从 Oracle / OceanBase 拉历史 SQL + 性能指标，自动归一化（绑定变量替换为字面量），支持 SQL 类型过滤 + 在线执行 + 策略白名单，元数据库用 SQLite（默认）或 PostgreSQL。

---

## 功能一览

| 模块 | 说明 |
|------|------|
| **实例管理** | 添加 Oracle / OceanBase 目标，Fernet 加密存密码，测试连接 |
| **手动/全量采集** | Oracle 走 AWR（`dba_hist_sql*`），OceanBase 走 `gv$ob_sql_audit` + `gv$sql_plan_monitor`，默认回溯 30 天 |
| **单条拉取** | 输入 sql_id / audit_sql_id，实时拉最新一次执行入库 |
| **归一化** | `canonical_sql`（`?` 占位）+ `literal_sql`（替换绑定值为字面量）+ `stmt_type` + 指纹 MD5 |
| **历史查询** | 按目标 / SQL 类型 / 关键字 / 时间 / 执行次数 / 是否有 literal 多维度过滤，展示指纹的全部快照折线图数据 |
| **在线执行** | SELECT 直接执行；写 SQL（INSERT/UPDATE/DELETE）自动回滚；DDL/PLSQL 强制拦截 |
| **高危硬拦** | `ALTER SYSTEM / DROP / TRUNCATE / CREATE USER / GRANT / REVOKE / CREATE TABLE/INDEX/PROCEDURE/FUNCTION/TRIGGER/PACKAGE` 等关键字 |
| **策略白名单** | 全局默认策略（允许 SELECT，禁止其他）+ 按目标级覆盖 + 可选正则精筛 |
| **执行历史** | 所有执行（包括被拦截的）都写入 `sql_exec_history` |

---

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.11+ / FastAPI / SQLAlchemy 2.0 / Alembic |
| 元数据库 | SQLite（默认，单文件 `sqlopt.db`）· PostgreSQL（改 `DATABASE_URL` 即切换） |
| Oracle 驱动 | `oracledb` thin mode（零 Oracle Client） |
| OceanBase 驱动 | `pymysql`（MySQL 协议） |
| 前端 | Vue 3 + Element Plus（CDN 单页，**无需 npm 构建**） |
| 加密 | `cryptography.Fernet` |

---

## 快速开始

```bash
cd sqlopt

# 1) 装依赖
pip install -r requirements.txt

# 2) 生成 Fernet key（用于加密目标实例密码）
python -c "from app.services.crypto import generate_key; print(generate_key())"
# 把输出的 key 写进 .env 里的 FERNET_KEY=xxx

# 3) 初始化元数据库（SQLite 自动建表；生产走 Alembic）
cp .env.example .env
# 编辑 .env，至少改 FERNET_KEY

# 4) 启动
python sqlopt_run.py
# 后端: http://localhost:8000/docs (OpenAPI)
# 前端: http://localhost:8000/   (CDN 单页，直接用浏览器打开)
```

### 切 PostgreSQL（生产）

改 `.env` 里一行即可，其他代码零改动：

```env
DATABASE_URL=postgresql+psycopg2://sqlopt:pwd@host:5432/sqlopt
```

然后跑 Alembic：

```bash
alembic upgrade head
python sqlopt_run.py
```

### Alembic 迁移

```bash
alembic revision --autogenerate -m "描述"   # 生成新迁移
alembic upgrade head                        # 应用
alembic downgrade -1                       # 回滚
```

---

## 使用流程（典型 DBA 第一天）

```
┌─ 1. 加实例 ──────────────────────────────────────────────────────┐
│ 前端 → 实例管理 → + 添加实例                                      │
│   Oracle: name=ORA-DEV, db_type=oracle, conn_url=10.0.0.1:1521/XE │
│           username=sqlopt, password=xxx,                          │
│           extra_conf={"pdb_service":"PDB1"}   ← PDB 名（可选）      │
│   OB    : name=OB-DEV, db_type=oceanbase, conn_url=10.0.0.2:2883 │
│           username=sqlopt, password=xxx,                          │
│           extra_conf={"tenant_name":"obdemo"}                      │
└───────────────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─ 2. 测试连接 ──────────────────────────────────────────────────────┐
│ 点「测试连接」→ 后端会真的连一次目标库，验证 CREATE SESSION + 核心  │
│ 视图 SELECT 权限。失败会返回具体错误。                               │
└───────────────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─ 3. 触发采集 ──────────────────────────────────────────────────────┐
│ 采集页面 → 选目标 → 选回溯天数（默认 30）→ 立即采集                   │
│ 后端：Oracle → dba_hist_sql* 视图一次拉取；                          │
│      OB    → gv$ob_sql_audit + gv$sql_plan_monitor 拉取。          │
│ 采集回来的每条 RawSQLRecord 自动走 Normalizer：                     │
│   a) canonical_sql（? 占位）                                        │
│   b) literal_sql（绑定变量渲染）                                    │
│   c) 指纹 MD5(canonical.lower())                                    │
│   d) stmt_type 推断                                                 │
│   e) upsert 到 sql_fingerprint + sql_stat_snapshot                  │
└───────────────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─ 4. 查历史 SQL ────────────────────────────────────────────────────┐
│ 历史 SQL 查询页面 → 过滤条件 → 查询                                 │
│   每条行显示 stmt_type tag + literal_sql（有则展示，无则展示          │
│   canonical_sql 并标记 ⚠）+ 总执行次数 + 平均耗时 + 最近执行时间       │
│   点击行 → 弹窗看详情（全部快照折线图字段 + fingerprint）             │
└───────────────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─ 5. 在线执行 ────────────────────────────────────────────────────┐
│ 在线执行页面 → 选目标 → 粘贴 SQL → 执行                              │
│ 流程：                                                              │
│   a) Normalizer 推断 stmt_type                                      │
│   b) PolicyService 高危关键字硬拦 → 策略表匹配                        │
│   c) 允许 → adapter.execute()                                       │
│      - SELECT  → 直接跑，返回结果（最多 1000 行，超时 30s）           │
│      - DELETE/UPDATE/INSERT → BEGIN ... ROLLBACK，不真改数据          │
│      - DDL/PLSQL → 强制拦截（即使策略允许也不准）                     │
│   d) 新 SQL 自动 fingerprint 入库                                    │
│   e) 执行（或拦截）历史写入 sql_exec_history                          │
└───────────────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─ 6. 策略管理（可选） ──────────────────────────────────────────────┐
│ 执行策略页面 → 新增策略                                              │
│   示例：只允许执行 user 表上的 SELECT                                 │
│   name="只读 user 表", target_id=NULL, stmt_type=SELECT,             │
│   pattern_regex="^SELECT .* FROM user_"                              │
└───────────────────────────────────────────────────────────────────┘
```

---

## 目录结构

```
sqlopt/
├── app/
│   ├── main.py                  # FastAPI 入口 + 前端 StaticFiles 挂载
│   ├── config.py                # pydantic-settings 配置
│   ├── database.py              # SQLAlchemy 引擎/会话（双库兼容）
│   ├── schemas.py               # Pydantic 请求/响应模型
│   ├── models/models.py         # ORM 模型（sql_target / sql_fingerprint / sql_stat_snapshot / sql_exec_history / sql_policy）
│   ├── services/
│   │   ├── crypto.py            # Fernet 加密/解密
│   │   ├── normalizer.py        # canonical / literal / 指纹 / stmt_type
│   │   ├── collector.py         # 采集服务（适配 + 归一化 + 入库）
│   │   ├── policy.py            # 高危关键字硬拦 + 策略表匹配
│   │   └── executor.py          # 在线执行 + 自动回滚
│   ├── adapters/
│   │   ├── factory.py           # 适配器工厂
│   │   ├── oracle_adapter.py    # Oracle（AWR 路径 + Cursor 路径）
│   │   └── oceanbase_adapter.py # OceanBase（审计 + 计划监控）
│   ├── routers/api.py           # 全部 REST API
├── migrations/                  # Alembic
│   ├── env.py
│   ├── script.py.mako
│   └── versions/65c0a3488f01_initial.py
├── frontend/index.html          # Vue 3 + Element Plus 单页（CDN）
├── requirements.txt
├── .env.example
└── sqlopt_run.py                # 便捷启动
```

---

## REST API

| Method | Path | 说明 |
|--------|------|------|
| GET  | `/` | 前端 HTML |
| GET  | `/docs` | OpenAPI 文档（自动生成） |
| **Target** | | |
| GET  | `/api/targets` | 列目标实例 |
| POST | `/api/targets` | 新增实例（密码加密） |
| DELETE | `/api/targets/{id}` | 软删除（enabled=False） |
| POST | `/api/targets/{id}/test` | 测试连接 |
| **采集** | | |
| POST | `/api/collect` | 全量采集（{target_id, since_days}） |
| POST | `/api/collect_one` | 单条拉取（{target_id, sql_id}） |
| **SQL 查询** | | |
| POST | `/api/sql/history` | 多条件查询（{target_id, stmt_type, keyword, only_with_literal, page, page_size, ...}） |
| GET  | `/api/sql/{fingerprint}` | 指纹详情 + 快照 |
| **在线执行** | | |
| POST | `/api/exec` | 在线执行（{target_id, sql_text}） |
| GET  | `/api/exec/history` | 执行历史 |
| **策略** | | |
| GET/POST/PUT/DELETE | `/api/policies[/...id]` | 策略 CRUD |

---

## Oracle 采集权限（速查）

```sql
-- CDB 级公共用户（推荐，一次授权跨所有 PDB）
CREATE USER C##SQLOPT IDENTIFIED BY "<pwd>" CONTAINER=ALL;
GRANT CREATE SESSION TO C##SQLOPT CONTAINER=ALL;
GRANT SELECT_CATALOG_ROLE TO C##SQLOPT CONTAINER=ALL;
-- 核心视图
GRANT SELECT ON SYS.DBA_HIST_SNAPSHOT TO C##SQLOPT CONTAINER=ALL;
GRANT SELECT ON SYS.DBA_HIST_SQLTEXT  TO C##SQLOPT CONTAINER=ALL;
GRANT SELECT ON SYS.DBA_HIST_SQLSTAT   TO C##SQLOPT CONTAINER=ALL;
GRANT SELECT ON SYS.DBA_HIST_SQLBIND   TO C##SQLOPT CONTAINER=ALL;
GRANT SELECT ON SYS.DBA_HIST_SQL_BIND_METADATA TO C##SQLOPT CONTAINER=ALL;
GRANT SELECT ON SYS.GV$PDBS TO C##SQLOPT CONTAINER=ALL;
-- 实时快照（可选）
GRANT SELECT ON SYS.GV_$SQL TO C##SQLOPT CONTAINER=ALL;
GRANT SELECT ON SYS.GV_$SQLTEXT_WITH_NEWLINES TO C##SQLOPT CONTAINER=ALL;
GRANT SELECT ON SYS.GV_$SQL_BIND_CAPTURE TO C##SQLOPT CONTAINER=ALL;
-- 在线执行必需
GRANT SELECT ANY TABLE TO C##SQLOPT CONTAINER=ALL;
GRANT EXECUTE ON DBMS_SQL TO C##SQLOPT CONTAINER=ALL;
-- 开启绑定变量采集（替换 literal 必需，开销 +5~15%）
ALTER SYSTEM SET awr_bind_capture = 'ALL' SCOPE=BOTH;
```

---

## OceanBase 采集权限（速查）

```sql
-- sys 租户采集用户（推荐，一次授权跨所有业务租户）
CREATE USER sqloopt IDENTIFIED BY "<pwd>" TENANT='sys';
GRANT CREATE SESSION TO sqloopt;
GRANT SELECT ON gv$ob_sql_audit TO sqloopt;
GRANT SELECT ON gv$ob_sql_audit_ext TO sqloopt;
GRANT SELECT ON gv$sql_plan_monitor TO sqloopt;
GRANT SELECT ON gv$sql_plan TO sqloopt;
GRANT SELECT ON gv$tenant TO sqloopt;
GRANT SELECT ON gv$database TO sqloopt;
GRANT SELECT ON *.* TO sqloopt TENANT=ALL;  -- 在线执行必需
-- 开启审计（默认只记慢 SQL，改为 all 才能拿所有 SQL）
SET GLOBAL sql_audit = ON;
SET GLOBAL sql_audit_print_level = 'all';
SET GLOBAL sql_audit_max_size = 1024;
```

---

## 安全红线（不可配置）

| 项目 | 规则 | 说明 |
|------|------|------|
| 高危关键字硬拦 | `\bALTER\s+SYSTEM\b / \bDROP\b / \bTRUNCATE\b / \bCREATE\s+USER\b / \bALTER\s+USER\b / \bGRANT\b / \bREVOKE\b / \bCREATE\s+TABLE\b / \bCREATE\s+INDEX\b / \bALTER\s+TABLE\b / \bALTER\s+INDEX\b / \bCREATE\s+PROCEDURE\b / \bCREATE\s+FUNCTION\b / \bCREATE\s+TRIGGER\b / \bCREATE\s+PACKAGE\b / \bALTER\s+DATABASE\b` | 无论策略怎么配都不准执行 |
| 写 SQL 自动回滚 | INSERT / UPDATE / DELETE → 包 `BEGIN ... ROLLBACK` | 不会真改业务数据 |
| DDL/PLSQL 强制拦截 | CREATE/DROP/ALTER/GRANT/REVOKE/CALL/EXECUTE ... | 即使策略允许也不准 |
| 单语句检查 | 分号切分只取第一句 | 防止 `SELECT 1; DELETE FROM t` 绕过 |
| 执行超时 | 默认 30s，硬上限 300s | — |
| 返回行数 | 默认最多 1000 行 | — |
| 密码加密 | Fernet 对称加密存库 | — |

---

## 绑定变量替换策略

| 来源 | literal_sql 能否生成 | 说明 |
|------|---------------------|------|
| Oracle AWR（`dba_hist_sqlbind`） | ✅ 有值就替换 | 需要 `awr_bind_capture=ALL`，开销中等 |
| Oracle Cursor（`gv$sql_bind_capture`） | ✅ 有值就替换 | 只针对当前内存中的 SQL |
| OB 审计（`parameters_json`） | ✅ 有值就替换 | 默认开启，开销小 |
| 拿不到绑定值 | ❌ literal_sql=NULL, literal_available=False | 不丢弃，前端标记「⚠ 仅 canonical」 |

literal 渲染规则：
- 数字 → 直接写 `42`
- 字符串 → 单引号 `'abc'`，内部单引号转义为 `''`
- 日期 → `TO_DATE('2024-01-01 12:00:00','YYYY-MM-DD HH24:MI:SS')`

---

## 已知限制（MVP 不做）

- ❌ 不做执行计划深度可视化
- ❌ 不做自动 SQL 改写/上线
- ❌ 不替代 DBA 工具链（AWR 报告、OB Dashboard 仍保留）
- ❌ 不支持实时监控（只做历史 + 手动触发的快照）
- ❌ 无 RBAC（MVP 单用户）
- ❌ 定时采集默认关闭（Iteration II 加 APScheduler）

---

## 下一阶段计划

- [ ] Iteration II：APScheduler 定时采集 + 仪表盘 Top N 慢 SQL
- [ ] Iteration III：执行计划拉取 + explain 展示
- [ ] Iteration IV：RBAC + 审批流 + Docker Compose 一键起
