"""Oracle driver wrapper — pooled synchronous connections with version-aware helpers.

Why synchronous?
oracledb's asyncio integration requires v2+ and is still less battle-tested than
the sync path under ThreadPoolExecutor.  FastAPI routes that need Oracle access
run the blocking call via ``asyncio.to_thread`` (see ``OracleConnection``).

Connection model:
- ``OracleConnectionManager`` is a process-wide singleton that owns one pool per
  Oracle instance (keyed by ``instance_id``).
- Each pool is created lazily on first use and cached forever (Oracle pools are
  safe to keep open; the target instance going down surfaces as a connection
  error that callers must handle — we do *not* auto-drop pools).
"""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from loguru import logger

from ..core.config import get_settings
from ..core.exceptions import OracleConnectionError, OraclePermissionError
from ..core.security import decrypt_password

try:
    import oracledb
except ImportError:  # pragma: no cover - only hit on dev machines without dep
    oracledb = None  # type: ignore


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

# Required privileges for the *read-only collection* account.
# We check each one individually so we can report exactly what is missing.
REQUIRED_PRIVILEGES = [
    "SELECT_CATALOG_ROLE",
    # Individual system privileges used by F1/F2/F3:
    "SELECT ANY DICTIONARY",  # fallback when SELECT_CATALOG_ROLE is absent
    "EXECUTE ANY PROCEDURE",  # DBMS_XPLAN, DBMS_STATS
]

# Views whose existence we probe as a proxy for "can read V$ views".
PROBE_VIEWS = [
    "V$VERSION",
    "V$SQLAREA",  # present 10g+
]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OracleVersion:
    """Parsed Oracle version. ``major`` is the marketing number (10, 11, 12, 19, ...)."""

    raw: str
    major: int
    minor: int

    @property
    def is_10g(self) -> bool:
        return self.major == 10

    @property
    def is_11g_plus(self) -> bool:
        return self.major >= 11

    @property
    def is_12c_plus(self) -> bool:
        return self.major >= 12

    @property
    def has_sql_id(self) -> bool:
        return self.is_11g_plus

    @property
    def has_a_rows(self) -> bool:
        """V$SQL_PLAN_STATISTICS_ALL exists 11g+, but A-Rows need STATISTICS_LEVEL=ALL."""
        return self.is_11g_plus

    @property
    def has_sql_profile(self) -> bool:
        return self.is_11g_plus

    @property
    def has_sql_patch(self) -> bool:
        return self.is_12c_plus


def parse_oracle_version(version_string: str) -> OracleVersion:
    """Parse output of ``SELECT * FROM v$version`` first line.

    Expected forms:
      "Oracle Database 10g Release 10.2.0.5.0 - 64bit Production"
      "Oracle Database 11g Express Edition Release 11.2.0.2.0 - 64bit Production"
      "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0 - Production"
    """
    # Grab the first number.two_numbers we see.
    m = re.search(r"(\d+)\.(\d+)", version_string or "")
    if not m:
        # Fallback: treat whole string as unknown version 0.0.
        return OracleVersion(raw=version_string or "unknown", major=0, minor=0)
    return OracleVersion(raw=version_string.strip(), major=int(m.group(1)), minor=int(m.group(2)))


# ---------------------------------------------------------------------------
# Connection wrapper
# ---------------------------------------------------------------------------

class OracleConnection:
    """Thin wrapper over an oracledb Connection that exposes convenience methods
    and carries the parsed Oracle version for callers to branch on."""

    def __init__(self, conn, version: OracleVersion):
        self._conn = conn
        self.version = version

    @property
    def raw(self):
        return self._conn

    def cursor(self):
        return self._conn.cursor()

    def execute(self, sql: str, binds: Optional[dict | list] = None):
        cur = self.cursor()
        try:
            cur.execute(sql, binds or {})
            return cur
        except Exception as e:
            cur.close()
            raise
        # NOTE: caller is responsible for closing the cursor, or using fetch_all below.

    def fetch_all(self, sql: str, binds: Optional[dict | list] = None) -> list[tuple]:
        cur = self.execute(sql, binds)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    def fetch_one(self, sql: str, binds: Optional[dict | list] = None) -> Optional[tuple]:
        cur = self.execute(sql, binds)
        try:
            return cur.fetchone()
        finally:
            cur.close()

    def fetch_val(self, sql: str, binds: Optional[dict | list] = None):
        """Execute and return the first column of the first row (or None)."""
        row = self.fetch_one(sql, binds)
        return row[0] if row else None

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def ping(self) -> bool:
        try:
            self._conn.ping()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Manager (pool-per-instance singleton)
# ---------------------------------------------------------------------------

class OracleConnectionManager:
    """Owns one oracledb ConnectionPool per OracleInstance, keyed by instance_id."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        if oracledb is None:
            raise RuntimeError("oracledb is not installed — run `pip install -r requirements.txt`")
        # Configure Thick mode (required for 10g).
        lib_dir = get_settings().oracle_client_lib_dir
        if lib_dir:
            try:
                oracledb.init_oracle_client(lib_dir=lib_dir)
                logger.info(f"Oracle Thick mode initialised with lib_dir={lib_dir}")
            except Exception as e:
                logger.warning(f"Could not init Thick mode with lib_dir={lib_dir}: {e}")
        self._pools: dict[int, oracledb.ConnectionPool] = {}
        # Cache version + capabilities per instance to avoid re-probing.
        self._capabilities: dict[int, OracleVersion] = {}

    @classmethod
    def get(cls) -> "OracleConnectionManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # -- Pool management ---------------------------------------------------

    def _build_dsn(self, host: str, port: int, service_name: str) -> str:
        return oracledb.makedsn(host, port, service_name=service_name)

    def _create_pool(self, instance_id: int, *, host: str, port: int,
                     service_name: str, user: str, password: str) -> oracledb.ConnectionPool:
        settings = get_settings()
        dsn = self._build_dsn(host, port, service_name)
        logger.info(f"Creating Oracle pool for instance {instance_id}: {user}@{dsn}")
        pool = oracledb.create_pool(
            user=user,
            password=password,
            dsn=dsn,
            min=settings.oracle_pool_min,
            max=settings.oracle_pool_max,
            increment=settings.oracle_pool_increment,
        )
        self._pools[instance_id] = pool
        return pool

    def _get_pool(self, instance_id: int) -> oracledb.ConnectionPool:
        pool = self._pools.get(instance_id)
        if pool is None:
            raise OracleConnectionError(
                f"No pool for instance {instance_id} — create one first via `ensure_pool`."
            )
        return pool

    def ensure_pool(self, instance_id: int, *, host: str, port: int,
                    service_name: str, user: str, password: str,
                    force_recreate: bool = False) -> oracledb.ConnectionPool:
        """Create (or replace) the pool for ``instance_id`` and probe version + permissions."""
        if force_recreate and instance_id in self._pools:
            self._pools[instance_id].close()
            del self._pools[instance_id]
            self._capabilities.pop(instance_id, None)

        if instance_id not in self._pools:
            self._create_pool(
                instance_id,
                host=host,
                port=port,
                service_name=service_name,
                user=user,
                password=password,
            )

        # Probe version (cached after first success).
        if instance_id not in self._capabilities:
            try:
                with self.connection(instance_id) as conn:
                    ver_str = conn.fetch_val(
                        "SELECT BANNER FROM v$version WHERE ROWNUM = 1"
                    ) or ""
                self._capabilities[instance_id] = parse_oracle_version(ver_str)
                logger.info(f"Instance {instance_id} version: {self._capabilities[instance_id]}")
            except Exception as e:
                logger.warning(f"Version probe failed for instance {instance_id}: {e}")
                # Cache an unknown version so we don't retry on every call.
                self._capabilities[instance_id] = parse_oracle_version("")

        return self._pools[instance_id]

    def close_pool(self, instance_id: int) -> None:
        pool = self._pools.pop(instance_id, None)
        self._capabilities.pop(instance_id, None)
        if pool is not None:
            try:
                pool.close()
            except Exception:
                pass

    def close_all(self) -> None:
        for iid in list(self._pools.keys()):
            self.close_pool(iid)

    def get_version(self, instance_id: int) -> Optional[OracleVersion]:
        return self._capabilities.get(instance_id)

    # -- Connection acquisition -------------------------------------------

    @contextmanager
    def connection(self, instance_id: int) -> Iterator[OracleConnection]:
        pool = self._get_pool(instance_id)
        raw = pool.acquire()
        ver = self._capabilities.get(instance_id)
        try:
            yield OracleConnection(raw, ver or OracleVersion("unknown", 0, 0))
        except oracledb.DatabaseError as e:
            # Surface DB errors wrapped with instance context.
            msg = str(e.args[0]) if e.args else str(e)
            if "insufficient privileges" in msg.lower() or "not authorized" in msg.lower():
                raise OraclePermissionError(msg) from e
            raise OracleConnectionError(msg) from e
        finally:
            try:
                pool.release(raw)
            except Exception:
                pass

    # -- One-shot convenience (no pool setup needed) ----------------------

    def test_connection(self, *, host: str, port: int, service_name: str,
                        user: str, password: str) -> OracleConnection:
        """Try a single, non-pooled connection. Returns an OracleConnection on success,
        raises OracleConnectionError / OraclePermissionError on failure."""
        dsn = self._build_dsn(host, port, service_name)
        try:
            raw = oracledb.connect(user=user, password=password, dsn=dsn)
        except oracledb.DatabaseError as e:
            msg = str(e.args[0]) if e.args else str(e)
            if "insufficient privileges" in msg.lower() or "not authorized" in msg.lower():
                raise OraclePermissionError(msg) from e
            raise OracleConnectionError(msg) from e
        except Exception as e:
            raise OracleConnectionError(str(e)) from e

        ver_str = ""
        try:
            cur = raw.cursor()
            cur.execute("SELECT BANNER FROM v$version WHERE ROWNUM = 1")
            row = cur.fetchone()
            if row:
                ver_str = row[0]
            cur.close()
        except Exception:
            pass
        ver = parse_oracle_version(ver_str)
        return OracleConnection(raw, ver)

    @staticmethod
    def check_read_permissions(conn: OracleConnection) -> tuple[list[str], list[str]]:
        """Probe the connection for the required privileges.

        Returns (granted, missing).  Uses role-probe approach (SELECT from
        DBA_SYS_PRIVS + SESSION_ROLES) because some privileges may come via
        a role rather than being granted directly.
        """
        granted: set[str] = set()
        missing: list[str] = []

        # 1) Check roles — SELECT_CATALOG_ROLE.
        try:
            roles = [r[0] for r in conn.fetch_all("SELECT ROLE FROM SESSION_ROLES")]
            if "SELECT_CATALOG_ROLE" in roles:
                granted.add("SELECT_CATALOG_ROLE")
        except Exception:
            pass

        # 2) Check system privileges.
        try:
            privs = {r[0] for r in conn.fetch_all(
                "SELECT PRIVILEGE FROM USER_SYS_PRIVS "
                "UNION SELECT PRIVILEGE FROM SESSION_SYS_PRIVS"
            )}
            # SESSION_SYS_PRIVS is only 12c+; USER_SYS_PRIVS only captures direct grants.
            # Fall back to probing DBA_SYS_PRIVS for role-granted privs if we have SELECT.
            try:
                role_privs = {r[0] for r in conn.fetch_all(
                    "SELECT PRIVILEGE FROM DBA_SYS_PRIVS WHERE GRANTEE = 'SELECT_CATALOG_ROLE'"
                )}
                privs |= role_privs
            except Exception:
                pass

            for p in ["SELECT ANY DICTIONARY", "EXECUTE ANY PROCEDURE"]:
                if p in privs:
                    granted.add(p)
        except Exception:
            pass

        # 3) Probe a V$ view as sanity check.
        can_read_v = False
        for view in PROBE_VIEWS:
            try:
                conn.fetch_val(f"SELECT 1 FROM {view} WHERE ROWNUM = 1")
                can_read_v = True
                break
            except Exception:
                continue
        if can_read_v:
            granted.add("SELECT V$ views")
        else:
            missing.append("SELECT V$ views")

        for req in REQUIRED_PRIVILEGES:
            if req not in granted:
                missing.append(req)

        return sorted(granted), missing
