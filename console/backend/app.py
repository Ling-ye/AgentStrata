from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from console.bootstrap import ensure_src_path

ensure_src_path()

from console.backend.routes import (  # noqa: E402
    bots,
    catalog,
    console_self,
    evaluations,
    evals,
    infra,
    overview,
    shared_services,
    tasks,
)
from console.backend.tasks import TaskManager  # noqa: E402
from console.control.evaluations import EvaluationManager  # noqa: E402


@asynccontextmanager
async def _lifespan(application: FastAPI):
    yield
    application.state.evaluations.close()


app = FastAPI(title="AgentStrata Console", version="1.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.tasks = TaskManager()
app.state.evaluations = EvaluationManager()

app.include_router(overview.router)
app.include_router(bots.router)
app.include_router(catalog.router)
app.include_router(console_self.router)
app.include_router(shared_services.router)
app.include_router(tasks.router)
app.include_router(evals.router)
app.include_router(evaluations.router)
app.include_router(infra.router)

_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
