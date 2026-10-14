import asyncio
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI

from app.api import memory as memory_api
from app.api.middleware import install_api_middleware
from app.api.approvals import create_approval_router
from app.core.database import engine
from app.api.tasks import create_task_router
from app.services.task_recovery import run_recovery_loop, schedule_startup_recovery


def _schedule_startup_recovery() -> BackgroundTasks:
    return schedule_startup_recovery(lambda: engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.database import create_db_and_tables

    create_db_and_tables(engine)
    recovery_tasks = _schedule_startup_recovery()
    if recovery_tasks.tasks:
        app.state.workflow_recovery_task = asyncio.create_task(recovery_tasks())
    recovery_loop = asyncio.create_task(run_recovery_loop(lambda: engine))
    app.state.workflow_recovery_loop = recovery_loop
    try:
        yield
    finally:
        recovery_loop.cancel()
        try:
            await recovery_loop
        except asyncio.CancelledError:
            pass
        from app.tools.browser import close_all_browser_sessions

        close_all_browser_sessions()


app = FastAPI(
    title="CentrAlign AI Operator",
    description="Plan, execute, and verify company tasks.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(memory_api.router)
app.include_router(create_task_router(lambda: engine))
app.include_router(create_approval_router(lambda: engine))
install_api_middleware(app)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "CentrAlign AI Operator"}
