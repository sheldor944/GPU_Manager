from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .database import Base, engine
from .routers import admin, auth_routes, dashboard, reservations
from .routers import api as api_router
from .scheduler import start_scheduler


def _run_migrations() -> None:
    """Apply schema changes that SQLAlchemy's create_all can't handle (column additions)."""
    with engine.connect() as conn:
        # gpus table
        result = conn.execute(text("PRAGMA table_info(gpus)"))
        gpu_cols = {row[1] for row in result}
        if "visibility" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN visibility VARCHAR NOT NULL DEFAULT 'PUBLIC'"))
        if "tags" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN tags VARCHAR"))
        if "max_hours" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN max_hours FLOAT"))
        if "ssh_user" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN ssh_user VARCHAR"))
        if "ssh_port" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN ssh_port INTEGER"))
        if "ssh_password" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN ssh_password VARCHAR"))
        if "gpu_index" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN gpu_index INTEGER NOT NULL DEFAULT 0"))
        if "cpu_model" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN cpu_model VARCHAR"))
        if "ram_gb" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN ram_gb INTEGER"))
        if "connect_instructions" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN connect_instructions TEXT"))
        if "is_remote" not in gpu_cols:
            conn.execute(text("ALTER TABLE gpus ADD COLUMN is_remote BOOLEAN NOT NULL DEFAULT 0"))

        # reservations table
        result = conn.execute(text("PRAGMA table_info(reservations)"))
        res_cols = {row[1] for row in result}
        if "scheduled_start_at" not in res_cols:
            conn.execute(text("ALTER TABLE reservations ADD COLUMN scheduled_start_at DATETIME"))

        # users table
        result = conn.execute(text("PRAGMA table_info(users)"))
        user_cols = {row[1] for row in result}
        for col, default in [
            ("email_on_offer", "1"),
            ("email_on_warning", "1"),
            ("email_on_watch", "1"),
            ("email_on_queue_move", "0"),
        ]:
            if col not in user_cols:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} BOOLEAN NOT NULL DEFAULT {default}"))

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
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(reservations.router)
app.include_router(admin.router)
app.include_router(api_router.router)


@app.get("/healthz")
def health():
    return {"ok": True}
