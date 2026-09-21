from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from console.bootstrap import ensure_src_path

ensure_src_path()

from console.backend.routes import (  # noqa: E402
    architecture,
    bots,
    catalog,
    console_self,
    evaluations,
    evals,
    infra,
    harness,
    interactions,
    overview,
    shared_services,
    tasks,
)
from chatcopilot.evals.service import EvaluationServiceClient  # noqa: E402
from console.backend.tasks import TaskManager  # noqa: E402
from console.backend.harness_runtime import create_controller  # noqa: E402


@asynccontextmanager
async def lifespan(application: FastAPI):
    if getattr(application.state, "harness", None) is None:
        try:
            application.state.harness = create_controller()
        except Exception:
            # Optional Harness failure must not disable unrelated Console pages.
            logging.getLogger(__name__).error("Harness control initialization failed")
    yield


app = FastAPI(title="AgentStrata Console", version="1.0", lifespan=lifespan)

@app.middleware("http")
async def private_api_responses(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.tasks = TaskManager()
app.state.evaluations = EvaluationServiceClient()

app.include_router(overview.router)
app.include_router(architecture.router)
app.include_router(bots.router)
app.include_router(catalog.router)
app.include_router(console_self.router)
app.include_router(shared_services.router)
app.include_router(tasks.router)
app.include_router(evals.router)
app.include_router(evaluations.router)
app.include_router(infra.router)
app.include_router(harness.router)
app.include_router(interactions.router)

_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
