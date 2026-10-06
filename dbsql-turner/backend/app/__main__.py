"""Convenience launcher — ``python -m app`` starts uvicorn."""
from .core.config import get_settings
import uvicorn

if __name__ == "__main__":
    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=s.debug)
