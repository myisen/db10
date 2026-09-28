from app.main import app

if __name__ == "__main__":
    import uvicorn
    from app.config import get_settings
    s = get_settings()
    uvicorn.run(app, host=s.api_host, port=s.api_port)
