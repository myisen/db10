---
name: "alembic-autogenerate-clean"
description: "Cleans Alembic autogenerate migration files that incorrectly include duplicate create_table/index from earlier revisions. Invoke right after running `alembic revision --autogenerate`."
---

# Alembic Autogenerate Clean

## 问题

`alembic revision --autogenerate -m "xxx"` 在已有历史迁移的项目里，常会把之前 revision 已建的表/索引重复拷进新 migration 的 `upgrade()`。后果：

- fresh DB 上 `alembic upgrade head` 报 `OperationalError: table xxx already exists`
- 同样的 migration 在已有 DB 上能跑（表已经存在），但干净环境跑炸

这是 Alembic autogenerate 的已知行为缺陷。

## 触发时机

**每次**运行完 `alembic revision --autogenerate` 之后，**立刻** invoke 本 skill。如果不 clean，下一次 fresh DB upgrade 就会挂。

## 清理脚本

在仓库根目录运行（需要 migrations 目录结构正确）：

```bash
python3 - << 'PYEOF'
import ast, re, sys, pathlib

ROOT = pathlib.Path(".")
VERSIONS = ROOT / "migrations" / "versions"
if not VERSIONS.exists():
    print("no migrations/versions dir, abort"); sys.exit(1)

# 1) 按 revision 声明的 down_revision 拓扑排序，得到 revision 链
revs = {}
for f in VERSIONS.glob("*.py"):
    src = f.read_text()
    name = f.stem
    m_rev = re.search(r'revision\s*=\s*[\'"]([^\'"]+)[\'"]', src)
    m_down = re.search(r'down_revision\s*=\s*([^\n]+)', src)
    if not m_rev: continue
    rev = m_rev.group(1)
    down_raw = m_down.group(1).strip() if m_down else None
    down = None
    if down_raw and down_raw != "None":
        m_q = re.match(r'[\'"]([^\'"]+)[\'"]', down_raw)
        down = m_q.group(1) if m_q else None
    revs[rev] = {"file": f, "down": down}

# 2) 从 head 往 root 遍历，收集每个 revision upgrade 里 create_table 的表名
def extract_creates(py_file: pathlib.Path):
    """返回 (table_set, index_set) — 这个 revision 的 upgrade() 里新建的 table / index"""
    src = py_file.read_text()
    tree = ast.parse(src)
    tables = set()
    indexes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # op.create_table('foo', ...)  or  op.create_index('idx', 'tbl', ...)
            func = None
            if isinstance(node.func, ast.Attribute):
                func = node.func.attr
            elif isinstance(node.func, ast.Name):
                func = node.func.id
            if func in ("create_table", "create_index", "create_unique_constraint",
                        "add_column", "drop_table", "drop_index"):
                if node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if func == "create_table":
                            tables.add(arg.value)
                        elif func == "create_index":
                            indexes.add(arg.value)
                        # drop_* 也记下来：说明老 revision 已 drop 过，后续 revision 可以 recreate
    return tables, indexes

# 拓扑：找 root (down_revision = None)，按顺序收集
chain = []
# 先找 head（不被任何其他 revision 的 down 指向）
all_rev_ids = set(revs.keys())
downs = {r["down"] for r in revs.values() if r["down"]}
heads = all_rev_ids - downs

# 从 head 沿 down 走到 root
visited = set()
def walk(rev):
    if rev in visited or rev is None: return
    visited.add(rev)
    chain.append(rev)
    walk(revs[rev]["down"])
for h in heads: walk(h)
chain.reverse()  # 现在 chain[0]=root, chain[-1]=head

print(f"revision chain ({len(chain)}): {' -> '.join(chain)}")

# 3) 每个 revision 维护 "此 revision 之后 DB 上存在的表/索引"
#    从 root 往 head 扫：每步先看当前 revision 要 create 的东西，
#    如果已在 accumulator 里 → 就是重复，要删掉这行
accum_tables = set()
accum_indexes = set()

for rev in chain:
    info = revs[rev]
    py = info["file"]
    tables, indexes = extract_creates(py)

    dup_tables = tables & accum_tables
    dup_indexes = indexes & accum_indexes

    if not dup_tables and not dup_indexes:
        accum_tables |= tables
        accum_indexes |= indexes
        continue

    print(f"\n[{rev}] {py.name}")
    print(f"  duplicate tables : {dup_tables}")
    print(f"  duplicate indexes: {dup_indexes}")

    # 4) 清理源码里的重复 op.create_table / op.create_index 块
    src = py.read_text()
    original = src

    for t in dup_tables:
        # 匹配 op.create_table('tname', ...) —— 从 op.create_table 到右括号的整个块
        # 多行匹配：op.create_table('foo',\n  sa.Column(...),\n  ...\n)\n
        pat = re.compile(
            r'[ \t]*op\.create_table\s*\(\s*[\'"]' + re.escape(t) + r'[\'"]\s*,.*?\n\s*\)\s*\n?',
            re.DOTALL
        )
        src = pat.sub("", src)
        # 同时删掉对应的 ForeignKey / UniqueConstraint / PrimaryKey 等在 create_table 里面的
        # （上面的 DOTALL 已经把整个 create_table 块吃掉，包括内部所有 sa.Column 和约束）

    for idx in dup_indexes:
        pat = re.compile(
            r'[ \t]*op\.create_index\s*\(\s*[\'"]' + re.escape(idx) + r'[\'"]\s*,.*?\n\s*\)\s*\n?',
            re.DOTALL
        )
        src = pat.sub("", src)

    if src != original:
        # 清理空行连续
        src = re.sub(r'\n{3,}', '\n\n', src)
        py.write_text(src)
        print(f"  ✓ cleaned {py.name}")

    accum_tables |= (tables - dup_tables)
    accum_indexes |= (indexes - dup_indexes)

print(f"\naccumulated tables : {sorted(accum_tables)}")
print(f"accumulated indexes: {sorted(accum_indexes)}")
print("\n=== clean done ===")
PYEOF
```

## 验证

clean 完必须在 **fresh DB** 上验证：

```bash
# SQLite 示例
rm -f sqlopt.db sqlopt.db-journal
python -m alembic upgrade head
# 预期：3 条 INFO 日志，无任何 ERROR/table already exists
```

如果是 PostgreSQL：
```bash
dropdb -U postgres your_db && createdb -U postgres your_db
DATABASE_URL=postgresql://postgres:...@localhost/your_db python -m alembic upgrade head
```

## 预防建议

1. **不要**在已有非空 DB 上跑 autogenerate 作为首次 migration。autogenerate 是给 "DB 空、按 ORM 建" 场景的。
2. 每次 autogenerate 之后**立刻**跑上面的 clean 脚本，形成流水线：`autogenerate → clean → 人工 review diff → commit`。
3. 在 CI 里加一步 fresh DB upgrade：`rm -f db.sqlite3 && alembic upgrade head`，失败即阻断。

## 已知局限

- 只清理 `op.create_table` / `op.create_index`。如果用了 `op.add_column` 加一个已存在的列，同样会炸（但那通常是 migration 链本身的设计 bug，不是 autogenerate 的锅）。
- 不处理 `batch_alter_table` 这种嵌套 block。如果你用了 batch，需要手动 review。
