from __future__ import annotations

import sqlite3

import hmac
import os
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, HTTPException, Request

from .clock import parse_time
from .engine import DuplicateRequestConflict, MemoryEngine
from .models import AddRequest, AddResponse, SearchRequest, SearchResponse
from .providers import ProviderUnavailable, providers_from_environment
from .settings import Settings, load_environment
from .storage import MemoryStore, SQLiteStore


def environment_engine():
    load_environment()
    backend = os.getenv("EMBERGRAPH_BACKEND", "sqlite")
    if backend not in {"sqlite", "memory"}:
        raise RuntimeError("backend_must_be_sqlite_or_memory")
    store = SQLiteStore(os.getenv("EMBERGRAPH_DB_PATH", ".local-data/embergraph.sqlite3")) if backend == "sqlite" else MemoryStore()
    try:
        model, embedder, reranker, judger = providers_from_environment()
        return MemoryEngine(store, model, embedder, reranker, Settings.from_environment(), judger)
    except Exception:
        store.close()
        raise


def create_app(engine=None, api_key=None):
    load_environment()
    @asynccontextmanager
    async def lifespan(app):
        yield
        if app.state.engine:
            app.state.engine.close()

    app = FastAPI(title="EmberGraph", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    key = api_key if api_key is not None else os.getenv("MEMORY_API_KEY")
    mutex = Lock()

    def active():
        if app.state.engine is None:
            with mutex:
                if app.state.engine is None:
                    try:
                        app.state.engine = environment_engine()
                    except (RuntimeError, ValueError, OSError):
                        raise HTTPException(status_code=503, detail="Service configuration or database requires initialization") from None
        return app.state.engine

    def authorize(request):
        if not key:
            return
        scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
        supplied = [request.headers.get("x-api-key", "")]
        if scheme.casefold() in {"bearer", "token"}:
            supplied.append(bearer)
        if not any(hmac.compare_digest(value, key) for value in supplied):
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "embergraph", "initialized": app.state.engine is not None}

    @app.get("/ready")
    def ready(http_request: Request):
        authorize(http_request)
        active()
        return {"status": "ready"}

    @app.post("/add", response_model=AddResponse)
    def add(request: AddRequest, http_request: Request):
        authorize(http_request)
        try:
            return active().add(request)
        except DuplicateRequestConflict:
            raise HTTPException(status_code=409, detail="request_id is already associated with another payload") from None
        except ProviderUnavailable:
            raise HTTPException(status_code=503, detail="Add paused with a retained checkpoint; retry the same request") from None
        except sqlite3.DatabaseError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 255
            busy = code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or "locked" in str(error).casefold()
            raise HTTPException(status_code=503 if busy else 500,
                detail="Storage busy; retry the same request" if busy else "Storage operation failed") from None

    @app.post("/search", response_model=SearchResponse)
    def search(request: SearchRequest, http_request: Request):
        authorize(http_request)
        clock = http_request.headers.get("x-memory-reference-time") or http_request.headers.get("x-traceweave-reference-time")
        if clock:
            try:
                parse_time(clock)
            except ValueError:
                raise HTTPException(status_code=422, detail="Reference time must use ISO format") from None
        try:
            return active().search(request, clock)
        except ProviderUnavailable:
            raise HTTPException(status_code=503, detail="Owner processing busy; retry search") from None
        except sqlite3.DatabaseError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 255
            busy = code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or "locked" in str(error).casefold()
            raise HTTPException(status_code=503 if busy else 500,
                detail="Storage busy; retry search" if busy else "Storage operation failed") from None

    return app


app = create_app()
