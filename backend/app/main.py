"""FastAPI application: REST API under /api/v1, OpenAPI docs at /docs."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .api import auth, catalog, customers, delivery, events, imports, jobs, recommendations, training
from .config import get_settings
from .logging_conf import configure_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(get_settings().log_level)
    yield
    db.close_pool()


app = FastAPI(
    title="Card Connect Offer Recommendation API",
    version="1.0.0",
    description="Personalised, eligibility-checked top-3 offer recommendations from approved catalog offers. "
                "Authenticate with POST /api/v1/auth/login and send `Authorization: Bearer <token>`.",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origin_list, allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])

for r in (auth, recommendations, jobs, imports, catalog, training, events, customers, delivery):
    app.include_router(r.router, prefix="/api/v1")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errs = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"], "type": e["type"]}
            for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": {"message": "Request validation failed", "errors": errs}})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health", tags=["health"], summary="Liveness")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"], summary="Readiness: database reachable and schema migrated")
def ready():
    try:
        with db.connection() as conn:
            version = db.scalar(conn, "SELECT MAX(version) FROM schema_migrations")
        return {"status": "ready", "schema_version": version}
    except Exception as exc:  # noqa: BLE001
        log.warning("Readiness check failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable", "reason": type(exc).__name__})
