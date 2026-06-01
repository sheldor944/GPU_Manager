from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .database import Base, engine
from .routers import admin, auth_routes, dashboard, reservations
from .scheduler import start_scheduler


def _run_migrations() -> None:
    """Apply schema changes that SQLAlchemy's create_all can't handle (column additions)."""
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA table_info(gpus)"))
        existing_cols = {row[1] for row in result}
        if "visibility" not in existing_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN visibility VARCHAR NOT NULL DEFAULT 'PUBLIC'"))
            conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    scheduler = start_scheduler()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Lab GPU Manager", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, https_only=False, same_site="lax")

app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(reservations.router)
app.include_router(admin.router)


@app.get("/healthz")
def health():
    return {"ok": True}
