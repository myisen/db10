# DBSQL-Tuner

> Oracle SQL 优化平台 — M1 基建 MVP。  
> 核心链路：**定位 Top SQL → 对象画像 → 执行计划 → 规则诊断 → 优化建议 → 沙箱验证 → 前后对比**。

---

## 目录结构

```
dbsql-tuner/
├── backend/                 # FastAPI + SQLAlchemy + oracledb
│   ├── app/
│   │   ├── main.py          # FastAPI 入口
│   │   ├── core/            # config / security / exceptions / logging
│   │   ├── db/              # SQLAlchemy async engine + session
│   │   ├── models/          # OracleInstance / TopSQLSnapshot / OptimizationRecord
│   │   ├── schemas/         # Pydantic request/response schemas
│   │   ├── oracle/          # OracleConnectionManager + collectors
│   │   └── api/             # REST routers (health / instances / top_sql)
│   ├── alembic/             # DB migrations
│   ├── alembic.ini
│   ├── requirements.txt
│   └── .env.example
├── frontend/                # React 18 + Vite + AntD
│   ├── src/
│   │   ├── pages/           # InstancesPage / TopSQLPage
│   │   ├── services/api.ts  # axios wrapper
│   │   └── types/
│   └── package.json
├── docker/                  # Dockerfiles + nginx.conf
├── docker-compose.yml
├── project.md               # 功能点说明（已存在）
└── development-plan.md      # 开发计划（已存在）
```

---

## 快速开始（本地开发）

### 前置

- Python 3.11+
- Node 20+
- Oracle Instant Client (19c+) — **Thick 模式必须**（兼容 10g/11g/12c）  
  下载：https://www.oracle.com/database/technologies/instant-client/downloads.html  
  解压后把 `libclntsh.so.*` 所在目录路径填到 `.env` 的 `ORACLE_CLIENT_LIB_DIR`。

### 后端

```bash
cd dbsql-tuner/backend

# 1. 建虚拟环境 + 装依赖
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 复制 .env 并改 SECRET_KEY / ORACLE_CLIENT_LIB_DIR
cp .env.example .env

# 3. 启动（debug 模式会自动建表）
python -m app
# 或：uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

后端启动后：

- 健康检查：http://localhost:8000/health
- Swagger UI：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc

### 前端

```bash
cd dbsql-tuner/frontend
npm install
npm run dev
```

浏览器访问 http://localhost:5173，Vite 会把 `/api/*` 代理到后端 8000。

### Docker（一键起）

```bash
# 在仓库根目录
docker compose up --build
# 前端:  http://localhost:8080
# 后端:  http://localhost:8000/docs
```

> **Thick 模式**：如果本机装了 Oracle Instant Client，把目录路径放到 `ORACLE_CLIENT_HOST_DIR` 环境变量里；或者在 `docker-compose.yml` 里删掉对应 volume 映射，改用 Thin 模式（仅支持 12.1+）。

---

## 下一步开发

当前状态：**M1 基建完成**（Oracle 连接池 + 实例 CRUD + Top SQL 采集 + 前端两个页面）。按 [development-plan.md](file:///workspace/development-plan.md) 继续：

| 里程碑 | 任务 |
|--------|------|
| M2 诊断 | F2 对象画像 + F3 执行计划获取与可视化 |
| M3 智能 | F4 规则引擎 R-01~R-12 + F5 建议生成 |
| M4 验证 | F6 沙箱执行 + F7 量化对比 |
| M5 闭环 | F8 历史审计 + F9 辅助诊断 |

---

## 关键技术决策（已落地）

| 决策 | 选择 | 理由 |
|------|------|------|
| Python 异步 DB | `SQLAlchemy 2.0` + `asyncpg` / `aiosqlite` | 统一 ORM，开发期用 SQLite、生产换 PostgreSQL 零改动 |
| Oracle 驱动 | `oracledb` Thick 模式 | 10g 必须 Thick；统一 Thick 可覆盖全版本 |
| 连接管理 | 进程级单例连接池，按 instance_id 缓存 | Oracle Pool 线程安全，避免频繁建连 |
| 密码存储 | Fernet 对称加密，密钥走 env | 绝不明文 |
| 版本适配 | `OracleConnection.version` 在首次连接时探测并缓存，所有采集器分支 | 10g/11g/12c 字段差异统一在采集层消化 |
| Top SQL 排序 | 总耗时优先（非平均耗时） | 高频快 SQL 比偶发慢 SQL 影响更大 |

---

## 数据库迁移

```bash
cd dbsql-tuner/backend
alembic upgrade head        # 应用所有迁移
alembic downgrade -1        # 回滚一个版本
alembic revision -m "描述"  # 新增一个迁移版本
```

> 开发环境 `DEBUG=true` 时 `lifespan` 会自动 `create_all`，**生产必须用 alembic**。

---

## Oracle 账号权限

只读采集账号最小权限集（参见 `backend/app/oracle/connection.py#REQUIRED_PRIVILEGES`）：

```sql
-- 推荐：授予角色（最省事）
GRANT SELECT_CATALOG_ROLE TO dbsql_read;
GRANT EXECUTE ANY PROCEDURE TO dbsql_read;

-- 或逐条授予
GRANT SELECT ANY DICTIONARY TO dbsql_read;
GRANT SELECT ON V_$SQL TO dbsql_read;
GRANT SELECT ON V_$SQLAREA TO dbsql_read;
GRANT SELECT ON DBA_HIST_SQLSTAT TO dbsql_read;  -- AWR 历史需要
GRANT SELECT ON DBA_TABLES TO dbsql_read;
GRANT SELECT ON DBA_INDEXES TO dbsql_read;
GRANT SELECT ON DBA_TAB_COLUMNS TO dbsql_read;
-- ... 按需扩展，实例页 "测试连接" 会自动报告缺失权限
```
