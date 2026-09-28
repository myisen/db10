"""FastAPI routers. One file per domain for clarity."""
from .auxiliary import router as auxiliary_router
from .diagnose import router as diagnose_router
from .execution_plan import router as execution_plan_router
from .health import router as health_router
from .history import router as history_router
from .instances import router as instances_router
from .object_profile import router as object_profile_router
from .sandbox import router as sandbox_router
from .top_sql import router as top_sql_router

__all__ = [
    "health_router",
    "instances_router",
    "top_sql_router",
    "object_profile_router",
    "execution_plan_router",
    "diagnose_router",
    "sandbox_router",
    "history_router",
    "auxiliary_router",
]
