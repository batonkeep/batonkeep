"""
main.py — FastAPI app: lifespan, REST routes (§10), WebSocket.

P1: Task CRUD + /me/mode + WS skeleton.
P6: Run CRUD + enqueue + cancel + providers health (this revision).
P7+: scheduler, stats, credentials, seed.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers, MutableHeaders

from app import approvals, sandbox
from app.auth import SESSION_COOKIE, issue_session, password_matches, verify_session
from app.config import get_settings
from app.db import get_db, init_db
from app.logging_config import configure_logging, owner_id_var, request_id_var
from app.models import Artifact, Credential, Owner, Run, RunEvent, Session, SessionTurn, Task
from app.schemas import (
    ApprovalDecision,
    AuthStatus,
    CaptureRequest,
    CloudflareConfigIn,
    CloudflareDeployIn,
    CloudflareDeployOut,
    CloudflareStatusOut,
    CockpitOut,
    ConsoleConfig,
    CredentialCreate,
    CredentialOut,
    CustomProviderCreate,
    CustomProviderOut,
    CustomProviderUpdate,
    FileEntryOut,
    GitImportIn,
    ImageModelOut,
    ImportOut,
    LoginRequest,
    ModelPricingOut,
    ModeOut,
    ProviderEnabledUpdate,
    ProviderHealth,
    ProviderLimitsUpdate,
    ProviderModelUpdate,
    ProviderTagsUpdate,
    PublishOut,
    RestoreOut,
    RestoreRequest,
    RunEventOut,
    RunOut,
    SecretStatusOut,
    SessionCreate,
    SessionOut,
    SessionTemplateOut,
    SessionTurnOut,
    SessionUpdate,
    StatsOut,
    SummaryOut,
    TaskCreate,
    TaskOut,
    TaskTemplateOut,
    TaskUpdate,
    TurnCreate,
    UploadOut,
    UsageSummaryOut,
    VersionDiffOut,
    VersionOut,
)
from app.ws import ws_manager

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    logger.info("Starting batonkeep backend (DEPLOYMENT_MODE=%s)", settings.deployment_mode)

    await init_db()

    from app.db import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        owner = await session.get(Owner, settings.owner_id)
        if owner is None:
            session.add(Owner(id=settings.owner_id, label="Local operator"))
            await session.commit()
            logger.info("Seeded owner: %s", settings.owner_id)

    # P7: seed representative tasks (insert-only-if-empty, §14)
    if settings.seed_examples:
        try:
            from app.seed import seed_if_empty
            await seed_if_empty(settings.owner_id)
        except ImportError:
            pass

    # D-0021: reconcile runs stranded by a previous restart before the scheduler
    # (and any re-enqueue) starts, so the run state is honest from boot.
    try:
        from app.orchestrator import reap_orphaned_runs
        await reap_orphaned_runs()
    except Exception:
        logger.exception("startup run-reaper failed")

    # P-0046: drift-guard the exec-env — confirm the built image's exec venv
    # still matches the manifest (guaranteed code-exec toolchain). Non-strict:
    # logs divergence (skipped on unbuilt images where the venv is absent).
    try:
        from app.exec_env import verify as verify_exec_env
        verify_exec_env()
    except Exception:
        logger.exception("exec-env drift check failed")

    # P-0046 slice 4: connect the curated SDK-backed MCP server(s) once and cache
    # their tool lists (the registry's list_tools() is sync; MCP is async). A server
    # that won't start just contributes no tools — never blocks startup.
    try:
        from app.providers.tools.registry import discover_mcp_tools
        await discover_mcp_tools()
    except Exception:
        logger.exception("MCP tool discovery failed")

    # D-0026: load operator-defined custom providers (local/Ollama/open-API) and
    # inject them into the registry so the executor factory can dispatch them.
    try:
        from app.custom_providers import init_custom_providers
        init_custom_providers()
    except Exception:
        logger.exception("custom provider init failed")

    # P7: scheduler wired here
    try:
        from app.scheduler import start_scheduler
        await start_scheduler()
    except ImportError:
        pass

    yield

    # P7: scheduler shutdown
    try:
        from app.scheduler import stop_scheduler
        await stop_scheduler()
    except ImportError:
        pass

    logger.info("Shutdown complete")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="batonkeep",
    description="Cross-provider plan orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

class PrivateNetworkAccessMiddleware:
    """Answer Chrome's Private Network Access (PNA) preflight.

    A page on a public origin (e.g. a hosted control plane) connecting to this
    backend on a private/loopback address (``127.0.0.1``, LAN) triggers a CORS
    preflight carrying ``Access-Control-Request-Private-Network: true``. Chrome
    blocks the request unless the response echoes
    ``Access-Control-Allow-Private-Network: true`` — which Starlette's
    ``CORSMiddleware`` does not add. This wraps the response (HTTP preflight and
    the WebSocket handshake) to add it when the client asks for it.

    Note: newer Chrome ("Local Network Access") may additionally show a one-time
    user permission prompt; this header is necessary but, on those versions, may
    not be sufficient on its own.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # The PNA preflight is an HTTP OPTIONS request (sent before both fetch
        # and the WebSocket handshake), so we only need to handle "http".
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        wants_pna = Headers(scope=scope).get("access-control-request-private-network") == "true"
        if not wants_pna:
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                MutableHeaders(raw=message["headers"])[
                    "Access-Control-Allow-Private-Network"
                ] = "true"
            await send(message)

        await self.app(scope, receive, send_wrapper)


# Paths reachable without an app-auth session: the login/status endpoints
# themselves, and the already-token-authenticated public surfaces (published
# share bundles + the per-session live-preview, each carrying its own path token).
_AUTH_PUBLIC_PREFIXES = ("/api/auth/", "/api/share/")


def _http_path_is_public(path: str) -> bool:
    # Container liveness probe (docker healthcheck) — content-free, no data; must
    # answer 200 even with app-auth on, or the stack never reports healthy.
    if path == "/health":
        return True
    if path.startswith(_AUTH_PUBLIC_PREFIXES):
        return True
    # /api/sessions/{id}/preview/{token}[/...] — own token, loaded in an iframe.
    return path.startswith("/api/sessions/") and "/preview/" in path


def _scope_session_ok(scope) -> bool:
    """Validate the app-auth session cookie straight off the raw ASGI scope."""
    from http.cookies import SimpleCookie

    cookie_header = Headers(scope=scope).get("cookie", "")
    if not cookie_header:
        return False
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:
        return False
    morsel = jar.get(SESSION_COOKIE)
    return verify_session(morsel.value if morsel else None)


class AppAuthMiddleware:
    """Single-operator app-auth gate (D-0023). When APP_PASSWORD is set, every
    HTTP request and websocket needs a valid session cookie, except the login
    endpoints and the token-authenticated public surfaces. Registered *inside*
    CORS so its 401 still carries CORS headers (browsers can read it cross-origin
    in dev). No-op when app-auth is disabled."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not get_settings().app_auth_enabled:
            return await self.app(scope, receive, send)

        if scope["type"] == "http":
            if scope.get("method") == "OPTIONS" or _http_path_is_public(scope.get("path", "")):
                return await self.app(scope, receive, send)
            if _scope_session_ok(scope):
                return await self.app(scope, receive, send)
            body = b'{"detail":"Authentication required"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        # websocket: reject before the handshake completes if unauthenticated.
        if _scope_session_ok(scope):
            return await self.app(scope, receive, send)
        await send({"type": "websocket.close", "code": 1008})


app.add_middleware(AppAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Added LAST so it is the OUTERMOST middleware: it must wrap CORSMiddleware to
# append the PNA header to the preflight response CORS generates and returns.
app.add_middleware(PrivateNetworkAccessMiddleware)


# ── Observability: request correlation + catch-all error handling (D-0021) ──────

@app.middleware("http")
async def correlation_middleware(request, call_next):
    """Assign a request id (honour an inbound X-Request-ID), bind it for logging,
    echo it on the response, and emit a structured access log per request."""
    rid = request.headers.get("x-request-id") or os.urandom(8).hex()
    tok = request_id_var.set(rid)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        # Logged while the contextvar is still bound so the access log is correlated.
        logger.info(
            "request",
            extra={"method": request.method, "path": request.url.path,
                   "status": response.status_code},
        )
        return response
    finally:
        request_id_var.reset(tok)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    """Last-resort handler: log the failure with correlation, return a clean 500.

    (FastAPI handles HTTPException itself; this catches the unexpected.)"""
    from fastapi.responses import JSONResponse

    logger.exception("unhandled exception",
                     extra={"method": request.method, "path": request.url.path})
    rid = request_id_var.get()
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": rid},
        headers={"X-Request-ID": rid} if rid else None,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _owner_id() -> str:
    # Bind for correlation (single-tenant today; the auth layer sets the real owner later).
    owner_id_var.set(settings.owner_id)
    return settings.owner_id


def _task_to_out(task: Task) -> TaskOut:
    return TaskOut.model_validate(task)


def _run_to_out(run: Run) -> RunOut:
    return RunOut.model_validate(run)


# ── /api/tasks ────────────────────────────────────────────────────────────────

@app.get("/api/task-templates", response_model=list[TaskTemplateOut], tags=["tasks"])
async def list_task_templates():
    """Starter task presets offered on a fresh install (seeded disabled; the form
    pre-fills them and nothing persists until the user saves)."""
    from app.tasks import templates as tmpl

    return [
        TaskTemplateOut(
            id=t.id, label=t.label, description=t.description, input=TaskCreate(**t.input)
        )
        for t in tmpl.list_templates()
    ]


@app.get("/api/tasks", response_model=list[TaskOut], tags=["tasks"])
async def list_tasks(
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    result = await db.execute(
        select(Task).where(Task.owner_id == owner_id).order_by(Task.id)
    )
    return [_task_to_out(t) for t in result.scalars().all()]


@app.post("/api/tasks", response_model=TaskOut, status_code=201, tags=["tasks"])
async def create_task(
    body: TaskCreate,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    task = Task(
        owner_id=owner_id,
        name=body.name,
        description=body.description,
        category=body.category,
        prompt_template=body.prompt_template,
        params=body.params,
        schedule_kind=body.schedule_kind,
        schedule_expr=body.schedule_expr,
        timezone=body.timezone,
        want_markdown=body.want_markdown,
        want_json=body.want_json,
        enabled=body.enabled,
        routing=body.routing.model_dump() if body.routing else None,
        exec_policy=body.exec_policy,
        image_model_id=body.image_model_id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    # Register the schedule immediately (parity with update_task). Without this a
    # newly-created scheduled task only starts firing after it's later edited or
    # toggled — and on the next restart it relied on the startup sync.
    try:
        from app.scheduler import scheduler_instance
        await scheduler_instance.sync_task(task)
    except (ImportError, AttributeError):
        pass

    return _task_to_out(task)


@app.get("/api/tasks/{task_id}", response_model=TaskOut, tags=["tasks"])
async def get_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    task = await db.get(Task, task_id)
    if task is None or task.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_out(task)


@app.put("/api/tasks/{task_id}", response_model=TaskOut, tags=["tasks"])
async def update_task(
    task_id: int,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    task = await db.get(Task, task_id)
    if task is None or task.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value)
    # "" sentinel clears the image-gen override back to the provider default.
    if task.image_model_id == "":
        task.image_model_id = None

    await db.commit()
    await db.refresh(task)

    try:
        from app.scheduler import scheduler_instance
        await scheduler_instance.sync_task(task)
    except (ImportError, AttributeError):
        pass

    return _task_to_out(task)


@app.delete("/api/tasks/{task_id}", status_code=204, tags=["tasks"])
async def delete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    task = await db.get(Task, task_id)
    if task is None or task.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.delete(task)
    await db.commit()

    try:
        from app.scheduler import scheduler_instance
        scheduler_instance.remove_task(task_id)
    except (ImportError, AttributeError):
        pass


# ── /api/runs ─────────────────────────────────────────────────────────────────

@app.get("/api/runs", response_model=list[RunOut], tags=["runs"])
async def list_runs(
    task_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    q = select(Run).where(Run.owner_id == owner_id)
    if task_id is not None:
        q = q.where(Run.task_id == task_id)
    if status is not None:
        q = q.where(Run.status == status)
    q = q.order_by(Run.id.desc()).limit(limit)
    result = await db.execute(q)
    return [_run_to_out(r) for r in result.scalars().all()]


@app.get("/api/runs/{run_id}", response_model=RunOut, tags=["runs"])
async def get_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    run = await db.get(Run, run_id)
    if run is None or run.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_to_out(run)


@app.get("/api/runs/{run_id}/events", response_model=list[RunEventOut], tags=["runs"])
async def get_run_events(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    run = await db.get(Run, run_id)
    if run is None or run.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Run not found")
    result = await db.execute(
        select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
    )
    return [RunEventOut.model_validate(ev) for ev in result.scalars().all()]


@app.post("/api/tasks/{task_id}/runs", response_model=RunOut, status_code=202, tags=["runs"])
async def enqueue_run(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Enqueue a run for the task. Returns 202 immediately; run executes async."""
    task = await db.get(Task, task_id)
    if task is None or task.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.orchestrator import enqueue_run as _enqueue
    run = await _enqueue(task_id, trigger="manual")
    return _run_to_out(run)


@app.post("/api/runs/{run_id}/cancel", response_model=RunOut, tags=["runs"])
async def cancel_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    run = await db.get(Run, run_id)
    if run is None or run.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Run not found")

    from app.orchestrator import cancel_run as _cancel
    await _cancel(run_id)
    await db.refresh(run)
    return _run_to_out(run)


@app.post("/api/runs/{run_id}/requeue", response_model=RunOut, status_code=202, tags=["runs"])
async def requeue_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Clear a deferral and run now: enqueue a fresh run for the same task (§10)."""
    run = await db.get(Run, run_id)
    if run is None or run.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Run not found")

    # Supersede the deferred run so the sweep won't also re-enqueue it.
    if run.status == "deferred":
        run.status = "cancelled"
        run.error = "superseded by manual requeue"
        await db.commit()

    from app.orchestrator import enqueue_run as _enqueue
    new_run = await _enqueue(run.task_id, trigger="manual")
    return _run_to_out(new_run)


@app.get("/api/runs/{run_id}/output", tags=["runs"])
async def get_run_output(
    run_id: int,
    format: str = "md",
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Download a run's rendered Markdown or JSON output (§10)."""
    run = await db.get(Run, run_id)
    if run is None or run.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Run not found")

    if format == "json":
        path, media = run.json_path, "application/json"
    elif format == "md":
        path, media = run.markdown_path, "text/markdown"
    else:
        raise HTTPException(status_code=400, detail="format must be 'md' or 'json'")

    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"No {format} output for this run")

    filename = f"run_{run_id}.{format}"
    return FileResponse(path, media_type=media, filename=filename)


# ── /api/sessions (M1.1: build sessions + workspace) ─────────────────────────

@app.get("/api/session-templates", response_model=list[SessionTemplateOut], tags=["sessions"])
async def list_session_templates():
    """Task types offered as starter cards (P-0010 / D-0011)."""
    from app.sessions import templates as tmpl

    return [
        SessionTemplateOut(id=t.id, label=t.label, description=t.description)
        for t in tmpl.list_templates()
    ]


@app.post("/api/sessions", response_model=SessionOut, status_code=201, tags=["sessions"])
async def create_session(
    body: SessionCreate,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Create a build session with a sandboxed, git-init'd workspace (M1.1).

    An optional `template` (D-0011) seeds a task-type goal + guidance into SESSION.md.
    """
    import secrets
    import uuid

    from app.sessions import templates as tmpl
    from app.sessions import workspace as ws

    tpl = tmpl.get_template(body.template) if body.template else None
    if body.template and tpl is None:
        raise HTTPException(status_code=400, detail=f"unknown template {body.template!r}")

    session_id = uuid.uuid4().hex
    default_title = tpl.label if tpl else "Untitled session"
    title = (body.title or default_title).strip()[:256]
    goal = body.goal or (tpl.goal if tpl else "")
    workspace_path = await ws.create_workspace(
        session_id, title=title, goal=goal, guidance=tpl.guidance if tpl else "",
    )

    session = Session(
        id=session_id,
        owner_id=owner_id,
        title=title,
        provider=body.provider,
        workspace_path=workspace_path,
        preview_token=secrets.token_urlsafe(24),
        status="active",
        confidential=body.confidential,
        image_model_id=body.image_model_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return SessionOut.model_validate(session)


@app.get("/api/sessions", response_model=list[SessionOut], tags=["sessions"])
async def list_sessions(
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    result = await db.execute(
        select(Session).where(Session.owner_id == owner_id).order_by(Session.created_at.desc())
    )
    sessions = result.scalars().all()
    ids = [s.id for s in sessions]

    # Content signals (used by the UI to scale delete confirmation): turn counts +
    # which sessions have a live publish. Two grouped queries, not per-row.
    turn_counts: dict[str, int] = {}
    published: set[str] = set()
    if ids:
        rows = await db.execute(
            select(SessionTurn.session_id, func.count())
            .where(SessionTurn.session_id.in_(ids))
            .group_by(SessionTurn.session_id)
        )
        turn_counts = {sid: n for sid, n in rows.all()}
        pub_rows = await db.execute(
            select(Artifact.session_id).where(
                Artifact.session_id.in_(ids), Artifact.published.is_(True)
            )
        )
        published = {sid for (sid,) in pub_rows.all()}

    out: list[SessionOut] = []
    for s in sessions:
        item = SessionOut.model_validate(s)
        item.turn_count = turn_counts.get(s.id, 0)
        item.published = s.id in published
        out.append(item)
    return out


@app.get("/api/sessions/{session_id}", response_model=SessionOut, tags=["sessions"])
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionOut.model_validate(session)


@app.patch("/api/sessions/{session_id}", response_model=SessionOut, tags=["sessions"])
async def update_session(
    session_id: str,
    body: SessionUpdate,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Rename a session or toggle its confidential (local-only) pin."""
    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title must not be empty")
        session.title = title[:256]
    if body.confidential is not None:
        session.confidential = body.confidential
    if body.exec_policy is not None:
        session.exec_policy = body.exec_policy
    if body.image_model_id is not None:
        # "" sentinel clears the override back to the provider default.
        session.image_model_id = body.image_model_id or None
    await db.commit()
    await db.refresh(session)
    return SessionOut.model_validate(session)


@app.post("/api/sessions/{session_id}/approvals/{request_id}", status_code=204, tags=["sessions"])
async def resolve_approval(
    session_id: str,
    request_id: str,
    body: ApprovalDecision,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Resolve a pending code-exec approval (P-0046 slice 3b). The session's turn is
    blocked awaiting this decision; approving lets the snippet run, denying feeds a
    refusal back to the agent."""
    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if not approvals.resolve(request_id, body.approved):
        raise HTTPException(status_code=404, detail="No pending approval for that id")


@app.delete("/api/sessions/{session_id}", status_code=204, tags=["sessions"])
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Delete a session: removes any published bundle, the sandboxed workspace
    directory, and the row (turns cascade). Unknown/foreign id → 404."""
    import shutil

    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")

    # No cascade from Session → Artifact, so tear down any publish bundle + row first.
    artifact = await _get_artifact(db, session_id)
    if artifact is not None:
        from app.sessions import publish as pub

        pub.remove_bundle(artifact.share_token, artifact.path)
        await db.delete(artifact)

    # Remove the per-session workspace from disk (best-effort; DB is source of truth).
    ws_path = session.workspace_path
    if ws_path and os.path.isdir(ws_path):
        shutil.rmtree(ws_path, ignore_errors=True)

    await db.delete(session)
    await db.commit()


@app.get("/api/sessions/{session_id}/turns", response_model=list[SessionTurnOut], tags=["sessions"])
async def list_session_turns(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    from app.sessions.preview import rewrite_workspace_file_links

    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")
    result = await db.execute(
        select(SessionTurn).where(SessionTurn.session_id == session_id).order_by(SessionTurn.seq)
    )
    out: list[SessionTurnOut] = []
    for t in result.scalars().all():
        item = SessionTurnOut.model_validate(t)
        # Rewrite file:// links on read too, so turns persisted before this feature
        # (idempotent for already-rewritten text) also render clickable artifacts.
        if item.response:
            item.response = rewrite_workspace_file_links(
                item.response, session_id, session.workspace_path
            )
        out.append(item)
    return out


@app.post("/api/sessions/{session_id}/turns", response_model=SessionTurnOut,
          status_code=202, tags=["sessions"])
async def create_session_turn(
    session_id: str,
    body: TurnCreate,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Send a message to the session's agent.

    Returns 202 immediately after creating the turn record and dispatching the
    agent as a background task. Events stream live over /ws; the caller should
    use the WebSocket feed (session.turn.update / session.event) to track
    completion rather than polling this endpoint.

    This avoids gateway timeouts for long-running agent turns (previously the
    endpoint awaited run_turn synchronously, so any turn > ~60 s would trip a
    504 at the proxy even though the agent kept running in the background).
    """
    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    from app.sessions.orchestrator import SessionError, create_turn_record, run_turn_background
    try:
        turn_id, turn_out = await create_turn_record(
            session_id, body.message, provider=body.provider, owner_id=owner_id,
        )
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Fire and forget — the orchestrator streams events over WS and persists the
    # turn result. We do NOT await this; the 202 response goes back immediately.
    import asyncio
    asyncio.ensure_future(run_turn_background(turn_id, session_id, owner_id=owner_id))

    return turn_out


@app.post("/api/sessions/{session_id}/capture",
          response_model=SessionTurnOut | None, tags=["sessions"])
async def capture_session_terminal(
    session_id: str,
    body: CaptureRequest,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Capture the web-TTY terminal lane's workspace edits as artifacts (D-0017
    thread 2). Commits the workspace and records it as a turn so the changed-files
    card surfaces the session's output. Returns the new turn, or null (200) if the
    workspace was unchanged — no empty turn is created.
    """
    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")

    from app.sessions.orchestrator import SessionError, capture_terminal_snapshot
    try:
        turn_id = await capture_terminal_snapshot(
            session_id, provider=body.instance, owner_id=owner_id
        )
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if turn_id is None:
        return None
    turn = await db.get(SessionTurn, turn_id)
    return SessionTurnOut.model_validate(turn)


@app.get("/api/sessions/{session_id}/summary", response_model=SummaryOut, tags=["sessions"])
async def get_session_summary(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """The session ledger's current auto-maintained summary (D-0017 thread 1)."""
    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")
    from app.sessions import workspace as ws_mod
    return SummaryOut(summary=ws_mod.read_summary(session.workspace_path) or None)


@app.post("/api/sessions/{session_id}/summary", response_model=SummaryOut, tags=["sessions"])
async def refresh_session_summary(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Refresh the ledger summary on demand (D-0017 thread 1). Forces a summarization
    even if the auto cadence is off, but still honours the confidential→local rule.
    Returns the new summary, or null if it was skipped (no eligible model, etc.).
    """
    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")
    from app.sessions.ledger import summarize_session
    text = await summarize_session(session_id, owner_id=owner_id, force=True)
    return SummaryOut(summary=text)


@app.post("/api/sessions/{session_id}/uploads", response_model=UploadOut,
          status_code=201, tags=["sessions"])
async def upload_session_assets(
    session_id: str,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Drop files into a session (M1.5, D-0010). Files land as real workspace files
    (images → assets/, data → data/) so the agent can reference them by name on the
    next turn (filesystem-as-context). The whole upload is committed as one version
    (Undo/History). Enforces the env-configurable size + extension allowlist;
    nothing leaves the backend.
    """
    from app.sessions import uploads
    from app.sessions import workspace as ws

    session = await _owned_session(session_id, owner_id, db)
    if not files:
        raise HTTPException(status_code=400, detail="no files provided")

    saved: list[str] = []
    try:
        for f in files:
            relpath = uploads.save_upload(session.workspace_path, f.filename or "", f.file)
            saved.append(relpath)
    except uploads.UploadError as exc:
        # Roll back any files written before the failure so the upload is atomic.
        for relpath in saved:
            uploads._remove_quiet(ws.safe_join(session.workspace_path, relpath))
        raise HTTPException(status_code=exc.status, detail=exc.detail)

    names = ", ".join(saved)
    commit_sha = await ws.commit_paths(session.workspace_path, message=f"upload: {names}")
    session.updated_at = datetime.now(UTC)
    await db.commit()
    return UploadOut(paths=saved, commit_sha=commit_sha)


@app.post("/api/sessions/{session_id}/import", response_model=ImportOut,
          status_code=201, tags=["sessions"])
async def import_session_archive(
    session_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Import an existing site into the session by extracting a .zip or .tar(.gz/.bz2/.xz)
    into the workspace root, preserving directory structure (unlike upload-in, which
    buckets by type). The agent continues from it (filesystem-as-context); the import
    is committed as one version. A `.git/` in the archive is dropped — the session
    keeps its own engine-owned history. Nothing leaves the backend.
    """
    import tempfile

    from app.sessions import imports
    from app.sessions import workspace as ws

    session = await _owned_session(session_id, owner_id, db)

    # Stream the upload to a temp file so the archive libs can sniff + random-access it.
    tmp = tempfile.NamedTemporaryFile(prefix="cf-import-", delete=False)
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)
        tmp.close()
        try:
            paths = imports.extract_archive(session.workspace_path, tmp.name)
        except imports.ImportArchiveError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass

    commit_sha = await ws.commit_paths(
        session.workspace_path,
        message=f"import: {len(paths)} files from {file.filename or 'archive'}",
    )
    session.updated_at = datetime.now(UTC)
    await db.commit()
    return ImportOut(paths=paths, count=len(paths), commit_sha=commit_sha)


@app.post("/api/sessions/{session_id}/import/git", response_model=ImportOut,
          status_code=201, tags=["sessions"])
async def import_session_git(
    session_id: str,
    body: GitImportIn,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Import a site by shallow-cloning a public https git URL into the workspace
    (structure preserved; the repo's .git is dropped — the session keeps its own
    history). SSRF-guarded (public hosts only); private repos fail fast (no creds).
    """
    from app.sessions import imports
    from app.sessions import workspace as ws

    session = await _owned_session(session_id, owner_id, db)
    try:
        paths = await imports.clone_repo(session.workspace_path, body.url, body.branch)
    except imports.ImportArchiveError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)

    commit_sha = await ws.commit_paths(
        session.workspace_path, message=f"import: {len(paths)} files from {body.url}"
    )
    session.updated_at = datetime.now(UTC)
    await db.commit()
    return ImportOut(paths=paths, count=len(paths), commit_sha=commit_sha)


@app.get("/api/sessions/{session_id}/preview/{token}/{path:path}", tags=["sessions"])
@app.get("/api/sessions/{session_id}/preview/{token}", tags=["sessions"])
async def session_preview(
    session_id: str,
    token: str,
    path: str = "",
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Serve a file from the session workspace for the in-UI live preview (M1.2).

    The preview token is a **path segment** (not a query param) so that the agent's
    relative asset links (`href="style.css"`) resolve under the same authenticated
    base and carry the token automatically — sub-assets load without cookies or
    HTML rewriting. The workspace is never reachable without session auth, and
    paths are confined to the session's own workspace.
    """
    from app.sessions.preview import (
        PreviewError,
        check_token,
        resolve_preview_file,
        rewrite_html_root_paths,
    )

    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        check_token(session.preview_token, token)
        file_path, media = resolve_preview_file(session.workspace_path, path)
    except PreviewError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)

    headers = {"Cache-Control": "no-store"}
    # no-store: preview reflects the latest turn's edits, never a cached version.
    if media.startswith("text/html"):
        # Bundler-built pages reference assets root-absolutely; pin them to the
        # token-carrying preview base so they resolve.
        with open(file_path, encoding="utf-8", errors="replace") as f:
            html = f.read()
        base = f"/api/sessions/{session_id}/preview/{token}"
        return Response(rewrite_html_root_paths(html, base), media_type=media, headers=headers)
    return FileResponse(file_path, media_type=media, headers=headers)


# ── Session file browser (P-0016 b) ──────────────────────────────────────────
# A session is more than a website: agents generate scripts/data the preview pane
# (index.html only) can't surface. These routes list and serve individual
# workspace files so any artifact is inspectable. Owner-scoped like the other
# session routes; the raw route serves the exact file (no index.html fallback).

@app.get("/api/sessions/{session_id}/files", response_model=list[FileEntryOut], tags=["sessions"])
async def list_session_files(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """List the session workspace's files (path/size/mtime), excluding internals."""
    from app.sessions import workspace as ws

    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return [FileEntryOut(**e) for e in ws.list_files_meta(session.workspace_path)]


@app.get("/api/sessions/{session_id}/files/raw/{path:path}", tags=["sessions"])
async def get_session_file(
    session_id: str,
    path: str,
    download: bool = False,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Serve one workspace file verbatim for view/download (P-0016 b). No index.html
    fallback (so a .py/.csv/.json comes back as-is). `?download=1` forces a save
    dialog; otherwise the browser renders inline. Path-traversal safe and confined
    to this session's own workspace.
    """
    from app.sessions.preview import PreviewError, resolve_workspace_file

    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        file_path, media = resolve_workspace_file(session.workspace_path, path)
    except PreviewError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)

    headers = {"Cache-Control": "no-store"}
    if download:
        name = os.path.basename(file_path)
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(file_path, media_type=media, headers=headers)


# ── Versioning: Undo/History (M1.3) ──────────────────────────────────────────
# Per-turn workspace commits surfaced as versions. "git" is never named to the
# user — this is the Undo/History of the build. Everything is owner_id-scoped:
# every route loads the session under the caller's owner_id first.

async def _owned_session(session_id: str, owner_id: str, db: AsyncSession):
    session = await db.get(Session, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/api/sessions/{session_id}/versions", response_model=list[VersionOut], tags=["sessions"])
async def list_session_versions(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Workspace versions (per-turn commits), newest first — the History list."""
    from app.sessions import workspace as ws

    session = await _owned_session(session_id, owner_id, db)
    versions = await ws.list_versions(session.workspace_path)
    return [VersionOut.model_validate(v) for v in versions]


@app.get("/api/sessions/{session_id}/versions/{commit}/diff",
         response_model=VersionDiffOut, tags=["sessions"])
async def get_session_version_diff(
    session_id: str,
    commit: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """The diff a single version introduced (per-turn diff for the event view)."""
    from app.sessions import workspace as ws

    session = await _owned_session(session_id, owner_id, db)
    diff = await ws.version_diff(session.workspace_path, commit)
    if diff is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return VersionDiffOut.model_validate(diff)


@app.post("/api/sessions/{session_id}/restore", response_model=RestoreOut, tags=["sessions"])
async def restore_session_version(
    session_id: str,
    body: RestoreRequest,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Restore the workspace to an earlier version (Undo/History). Implemented as a
    checkout-restore committed as a new version, so the restore is itself undoable.
    """
    from app.sessions import workspace as ws

    session = await _owned_session(session_id, owner_id, db)
    result = await ws.restore_version(session.workspace_path, body.commit)
    if result is None:
        raise HTTPException(
            status_code=400, detail="Unknown version, or workspace already matches it",
        )
    session.updated_at = datetime.now(UTC)
    await db.commit()
    return RestoreOut.model_validate(result)


# ── Publish + share (M1.4) ───────────────────────────────────────────────────
# Two delivery mechanisms (D-0009): a download pack (zip the static assets) and a
# revocable backend share link (public bundle served at /api/share/{token}). Host
# connectors are out of M1.4 (deferred post-M1). owner-scoped routes load the
# session under the caller first; the public share route is gated only by the
# unguessable token (its capability), never by owner.

def _artifact_to_publish_out(artifact: Artifact | None) -> PublishOut:
    if artifact is None or not artifact.published or not artifact.share_token:
        return PublishOut(published=False)
    file_count = None
    if artifact.path and os.path.isdir(artifact.path):
        file_count = sum(len(files) for _, _, files in os.walk(artifact.path))
    return PublishOut(
        published=True,
        share_token=artifact.share_token,
        share_path=f"/api/share/{artifact.share_token}/",
        version=artifact.version,
        kind=artifact.kind,
        file_count=file_count,
        updated_at=artifact.updated_at,
    )


async def _get_artifact(db: AsyncSession, session_id: str) -> Artifact | None:
    return (await db.execute(
        select(Artifact).where(Artifact.session_id == session_id)
    )).scalar_one_or_none()


@app.get("/api/sessions/{session_id}/publish", response_model=PublishOut, tags=["sessions"])
async def get_publish_state(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Current publish/share state of a session's build (for the UI)."""
    await _owned_session(session_id, owner_id, db)
    return _artifact_to_publish_out(await _get_artifact(db, session_id))


@app.post("/api/sessions/{session_id}/publish", response_model=PublishOut, tags=["sessions"])
async def publish_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Publish the session's current build to a revocable public share link. Snapshots
    the workspace's static assets into a fresh bundle and mints a new share token
    (re-publishing rotates the token + refreshes the snapshot).
    """
    import secrets

    from app.sessions import publish as pub
    from app.sessions import workspace as ws

    session = await _owned_session(session_id, owner_id, db)

    artifact = await _get_artifact(db, session_id)
    # Drop any prior bundle before minting a new token (revoke-then-republish).
    if artifact is not None:
        pub.remove_bundle(artifact.share_token, artifact.path)

    share_token = secrets.token_urlsafe(18)
    bundle_path = pub.build_bundle(session.workspace_path, share_token)
    version = await ws.head_commit(session.workspace_path)

    if artifact is None:
        artifact = Artifact(session_id=session_id, owner_id=owner_id, kind="site")
        db.add(artifact)
    artifact.share_token = share_token
    artifact.published = True
    artifact.path = bundle_path
    artifact.version = version
    await db.commit()
    await db.refresh(artifact)
    return _artifact_to_publish_out(artifact)


@app.delete("/api/sessions/{session_id}/publish", response_model=PublishOut, tags=["sessions"])
async def revoke_publish(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Revoke a session's share link: remove the public bundle and clear the token (→ 404)."""
    from app.sessions import publish as pub

    await _owned_session(session_id, owner_id, db)
    artifact = await _get_artifact(db, session_id)
    if artifact is not None:
        pub.remove_bundle(artifact.share_token, artifact.path)
        artifact.published = False
        artifact.share_token = None
        artifact.path = None
        await db.commit()
    return PublishOut(published=False)


@app.post("/api/sessions/{session_id}/publish/cloudflare", response_model=CloudflareDeployOut,
          tags=["sessions"])
async def publish_session_cloudflare(
    session_id: str,
    body: CloudflareDeployIn = CloudflareDeployIn(),
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Deploy the session's current build to the owner's Cloudflare Pages project
    (D-0009 host connector). Backend-side: the deploy token is read from the
    encrypted credential store and used only by a trusted wrangler subprocess —
    it never enters the agent sandbox.
    """
    from app.sessions import cf_pages

    session = await _owned_session(session_id, owner_id, db)
    config = await cf_pages.get_config(db, owner_id)
    if config is None:
        raise HTTPException(status_code=400, detail="Cloudflare is not configured")
    # Project is per-session: explicit override → remembered project → title default.
    project = (body.project_name or session.cf_project or cf_pages.slug_project(session.title))
    try:
        result = await cf_pages.deploy(session.workspace_path, config, project)
    except cf_pages.CloudflareError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # Remember where this session deploys so later deploys default to the same site.
    session.cf_project = result["project"]
    await db.commit()
    return CloudflareDeployOut(**result)


@app.get("/api/sessions/{session_id}/download", tags=["sessions"])
async def download_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Download the session's build as a zip of its static assets (download pack #1)."""
    from app.sessions import publish as pub

    session = await _owned_session(session_id, owner_id, db)
    data = pub.zip_workspace(session.workspace_path)
    safe_name = (
        "".join(c if c.isalnum() or c in "-_" else "-" for c in session.title)[:48] or "site"
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


@app.get("/api/share/{token}/{path:path}", tags=["share"])
@app.get("/api/share/{token}", tags=["share"])
async def serve_share(
    token: str,
    path: str = "",
    db: AsyncSession = Depends(get_db),
):
    """
    Public share route (M1.4): serve a published bundle by its share token. No owner
    auth — the unguessable token is the capability. Revoked/unknown tokens 404, and
    only the published bundle is reachable (the live workspace is never exposed here).
    """
    from app.sessions.preview import PreviewError, resolve_preview_file, rewrite_html_root_paths

    artifact = (await db.execute(
        select(Artifact).where(Artifact.share_token == token)
    )).scalar_one_or_none()
    if artifact is None or not artifact.published or not artifact.path:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        file_path, media = resolve_preview_file(artifact.path, path)
    except PreviewError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    if media.startswith("text/html"):
        with open(file_path, encoding="utf-8", errors="replace") as f:
            html = f.read()
        return Response(rewrite_html_root_paths(html, f"/api/share/{token}"), media_type=media)
    return FileResponse(file_path, media_type=media)


# ── /api/stats ──────────────────────────────────────────────────────────────────

@app.get("/api/stats", response_model=StatsOut, tags=["meta"])
async def get_stats(
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Dashboard aggregates (§10): runs today, success rate, runs_by_provider, failover, deferred."""  # noqa: E501
    start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    rows = (await db.execute(
        select(Run).where(Run.owner_id == owner_id, Run.created_at >= start_of_day)
    )).scalars().all()

    runs_today = len(rows)
    terminal = [r for r in rows if r.status in ("succeeded", "failed")]
    succeeded = [r for r in rows if r.status == "succeeded"]
    success_rate = (len(succeeded) / len(terminal)) if terminal else 0.0

    durations = [r.duration_ms for r in succeeded if r.duration_ms is not None]
    avg_duration_ms = (sum(durations) / len(durations)) if durations else None

    runs_by_provider: dict[str, int] = {}
    for r in rows:
        if r.provider:
            runs_by_provider[r.provider] = runs_by_provider.get(r.provider, 0) + 1

    # failover_rate: fraction of terminal runs that needed more than one attempt
    failed_over = sum(1 for r in terminal if r.attempts and len(r.attempts) > 1)
    failover_rate = (failed_over / len(terminal)) if terminal else 0.0

    cost_today_usd = sum(r.cost_usd or 0.0 for r in rows)

    # deferred / active counts are global to the owner, not just today
    deferred_now = await db.scalar(
        select(func.count(Run.id)).where(Run.owner_id == owner_id, Run.status == "deferred")
    ) or 0
    active_runs = await db.scalar(
        select(func.count(Run.id)).where(
            Run.owner_id == owner_id,
            Run.status.in_(("queued", "planning", "running")),
        )
    ) or 0

    return StatsOut(
        runs_today=runs_today,
        success_rate=round(success_rate, 4),
        avg_duration_ms=avg_duration_ms,
        runs_by_provider=runs_by_provider,
        failover_rate=round(failover_rate, 4),
        deferred_now=deferred_now,
        cost_today_usd=round(cost_today_usd, 6),
        active_runs=active_runs,
    )


# ── /api/cockpit (operational telemetry — D-0022 Task A, audience A) ──────────────

@app.get("/api/cockpit", response_model=CockpitOut, tags=["meta"])
async def get_cockpit(
    window_days: int = 7,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    The operator cockpit (D-0022): the user's consolidated view of **their own**
    work — spend, run outcomes, latency, failovers, errors-by-class, and build/
    session activity over a trailing window. Local-first and sovereign by
    construction; this data never leaves the deployment and asks no consent
    (that is audience B, gated on managed). Reuses the shipped cost/quota/domain
    telemetry — one model, not a parallel pipe.
    """
    from app.telemetry import operational_cockpit
    return CockpitOut(**await operational_cockpit(db, owner_id, window_days=window_days))


# ── /api/credentials (BYO-key) ───────────────────────────────────────────────────

@app.get("/api/credentials", response_model=list[CredentialOut], tags=["credentials"])
async def list_credentials_route(
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """List stored providers (no plaintext keys ever returned)."""
    result = await db.execute(
        select(Credential).where(Credential.owner_id == owner_id)
    )
    return [CredentialOut.model_validate(c) for c in result.scalars().all()]


@app.post("/api/credentials", response_model=CredentialOut, status_code=201, tags=["credentials"])
async def create_credential(
    body: CredentialCreate,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Store an encrypted BYO API key for a provider (§8)."""
    from app.credentials import store_credential
    cred = await store_credential(db, owner_id, body.provider, body.api_key, label=body.label)
    return CredentialOut.model_validate(cred)


@app.delete("/api/credentials/{provider}", status_code=204, tags=["credentials"])
async def delete_credential_route(
    provider: str,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Delete a stored BYO key."""
    from app.credentials import delete_credential
    deleted = await delete_credential(db, owner_id, provider)
    if not deleted:
        raise HTTPException(status_code=404, detail="Credential not found")


@app.get("/api/usage", response_model=UsageSummaryOut, tags=["meta"])
async def get_usage(
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Owner spend surface (P-0009 #2): today / last-7-day metered cost, per-provider
    breakdown, the configured daily cap, remaining headroom, and whether the budget
    gate is degrading new runs to zero-cost providers. API + log, not a dashboard.
    """
    from app.cost import usage_summary
    return UsageSummaryOut(**await usage_summary(db, owner_id))


@app.get("/api/secrets", response_model=list[SecretStatusOut], tags=["credentials"])
async def secrets_status_route(
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """
    Named secrets-management surface (P-0009 #3): for every key-backed provider,
    report whether its credential resolves from the encrypted store, the deployment
    env, or is missing — with a masked hint + last-used, never any plaintext.
    """
    from app.credentials import secrets_status
    return await secrets_status(db, owner_id)


# ── /api/integrations/cloudflare (host connector, D-0009) ─────────────────────
# Stores the Cloudflare Pages deploy config (token encrypted; account/project as
# config). Used only by the backend-side deploy route — never an agent sandbox.

@app.get("/api/integrations/cloudflare", response_model=CloudflareStatusOut, tags=["integrations"])
async def get_cloudflare_config(
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Connector status for the UI — reports account/project, never the token."""
    from app.sessions import cf_pages

    cfg = await cf_pages.get_config(db, owner_id)
    if cfg is None:
        return CloudflareStatusOut(configured=False)
    return CloudflareStatusOut(configured=True, account_id=cfg["account_id"])


@app.put("/api/integrations/cloudflare", response_model=CloudflareStatusOut, tags=["integrations"])
async def set_cloudflare_config(
    body: CloudflareConfigIn,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Store/replace the Cloudflare Pages connector config (token encrypted at rest)."""
    from app.sessions import cf_pages

    try:
        await cf_pages.set_config(
            db, owner_id, api_token=body.api_token, account_id=body.account_id,
        )
    except cf_pages.CloudflareError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return CloudflareStatusOut(configured=True, account_id=body.account_id.strip())


@app.delete("/api/integrations/cloudflare", status_code=204, tags=["integrations"])
async def delete_cloudflare_config(
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(_owner_id),
):
    """Remove the Cloudflare connector config."""
    from app.sessions import cf_pages

    deleted = await cf_pages.clear_config(db, owner_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Cloudflare is not configured")


# ── /api/custom-providers (D-0026) ───────────────────────────────────────────
# Operator-defined custom local/Ollama/open-API endpoints, mutable from the
# Settings UI without a backend deploy. Kept in /data/custom-providers.json;
# reloaded into the live registry after each CRUD write.

@app.get("/api/custom-providers", response_model=list[CustomProviderOut], tags=["providers"])
async def list_custom_providers():
    """List all custom provider entries (including disabled ones)."""
    from app.custom_providers import list_all_custom_providers
    return [CustomProviderOut.model_validate(vars(cp)) for cp in list_all_custom_providers()]


@app.post("/api/custom-providers", response_model=CustomProviderOut, status_code=201,
          tags=["providers"])
async def create_custom_provider_route(body: CustomProviderCreate):
    """Add a new custom provider endpoint (Ollama, LM Studio, any OpenAI-compat)."""
    from app.custom_providers import CustomProviderError, create_custom_provider
    try:
        cp = create_custom_provider(
            cp_id=body.id,
            label=body.label,
            base_url=body.base_url,
            default_model=body.default_model,
            auth_type=body.auth_type,
            env_key=body.env_key,
            local=body.local,
            extra_models=body.extra_models,
            capability_tags=body.capability_tags,
            cost_in_per_mtok=body.cost_in_per_mtok,
            cost_out_per_mtok=body.cost_out_per_mtok,
        )
    except CustomProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return CustomProviderOut.model_validate(vars(cp))


@app.put("/api/custom-providers/{cp_id}", response_model=CustomProviderOut, tags=["providers"])
async def update_custom_provider_route(cp_id: str, body: CustomProviderUpdate):
    """Update an existing custom provider's config."""
    from app.custom_providers import CustomProviderError, update_custom_provider
    try:
        cp = update_custom_provider(
            cp_id,
            label=body.label,
            base_url=body.base_url,
            default_model=body.default_model,
            auth_type=body.auth_type,
            env_key=body.env_key,
            local=body.local,
            enabled=body.enabled,
            extra_models=body.extra_models,
            capability_tags=body.capability_tags,
            cost_in_per_mtok=body.cost_in_per_mtok,
            cost_out_per_mtok=body.cost_out_per_mtok,
        )
    except CustomProviderError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 422,
                            detail=str(exc))
    return CustomProviderOut.model_validate(vars(cp))


@app.delete("/api/custom-providers/{cp_id}", status_code=204, tags=["providers"])
async def delete_custom_provider_route(cp_id: str):
    """Remove a custom provider endpoint."""
    from app.custom_providers import delete_custom_provider
    if not delete_custom_provider(cp_id):
        raise HTTPException(status_code=404, detail="Custom provider not found")


# ── /api/providers ────────────────────────────────────────────────────────────

@app.get("/api/image-models", response_model=list[ImageModelOut], tags=["providers"])
async def list_image_models_route():
    """The selectable image-generation models (P-0046 slice 6). `available` reflects
    whether the model's home provider currently has a usable credential — the UI
    offers cross-provider models but disables the ones whose provider isn't connected."""
    from app.credentials import resolve_api_key
    from app.providers.image_models import list_image_models
    from app.providers.registry import get_provider_def

    out: list[ImageModelOut] = []
    cred_cache: dict[str, bool] = {}
    for m in list_image_models():
        if m.provider not in cred_cache:
            home = get_provider_def(m.provider)
            if home is None:
                cred_cache[m.provider] = False
            elif home.auth_type == "none":
                cred_cache[m.provider] = True
            else:
                cred_cache[m.provider] = bool(
                    await resolve_api_key(home.name, home.env_key)
                )
        out.append(ImageModelOut(
            id=m.id, label=m.label, provider=m.provider, model=m.model,
            cost_per_image=m.cost_per_image, cost_per_mtok=m.cost_per_mtok,
            available=cred_cache[m.provider],
        ))
    return out


@app.get("/api/providers", response_model=list[ProviderHealth], tags=["providers"])
async def list_providers():
    """
    List all registered providers with their current health state.
    NOTE: est_used_pct is approximate — reliable guarantee is failover on observed limits.
    """
    from app.providers import model_pricing
    from app.providers.registry import (
        effective_capability_tags,
        effective_model,
        effective_pricing,
        get_pricing_override,
        get_provider_def,
        is_instance_connected,
        is_provider_enabled,
        list_instances,
    )
    from app.quota import quota_tracker

    result = []
    for inst in list_instances():
        pdef = get_provider_def(inst.template)
        if pdef is None:
            continue
        health = quota_tracker.get_health(inst.id)
        enabled = is_provider_enabled(inst.id)
        connected = await is_instance_connected(inst)
        model = effective_model(inst, pdef)
        in_rate, out_rate = effective_pricing(pdef, inst.id, model)
        if get_pricing_override(inst.id) is not None:
            pricing_source = "override"
        elif model_pricing.lookup(model) is not None:
            pricing_source = "registry"
        else:
            pricing_source = "template"
        # Healthy only if actually usable (connected/logged-in) AND not cooling.
        # A not-connected instance reports unhealthy with no cooldown → UI shows "offline".
        result.append(ProviderHealth(
            name=inst.id,
            template=inst.template,
            label=inst.label,
            model=model,
            kind=pdef.kind,
            tier=pdef.tier,
            enabled=enabled,
            # A suspended provider reports unhealthy so the UI shows it offline and
            # routing (which also checks is_provider_enabled) skips it.
            healthy=enabled and connected and health.healthy,
            cooldown_until=health.cooldown_until,
            last_reset_seen=health.last_reset_seen,
            est_used_pct=health.est_used_pct,
            usage_seen_at=health.subscription_seen_at,
            mode=pdef.mode,
            capability_tags=effective_capability_tags(pdef),
            cost_in_per_mtok=in_rate,
            cost_out_per_mtok=out_rate,
            pricing_source=pricing_source,
        ))
    return result


@app.post("/api/providers/{provider_name}/limits", tags=["providers"])
async def set_provider_limits(provider_name: str, body: ProviderLimitsUpdate):
    """Operator-declared limit window for approximate headroom display."""
    from app.quota import quota_tracker
    quota_tracker.set_declared_limits(
        provider_name,
        window_seconds=body.window_seconds,
        window_limit=body.window_limit,
    )
    return {"status": "ok"}


@app.post("/api/providers/{provider_name}/reset", tags=["providers"])
async def reset_provider_cooldown(provider_name: str):
    """Manually clear a provider's cooldown (operator escape hatch)."""
    from app.quota import quota_tracker
    quota_tracker.mark_healthy(provider_name)
    logger.info("Operator manually reset cooldown for %s", provider_name)
    return {"status": "ok", "provider": provider_name}


# ── App-level auth (D-0023, resolves P-0026) ───────────────────────────────────

def _set_session_cookie(resp: Response) -> None:
    # httpOnly so JS can't read it; SameSite=Lax for top-level navigation. Not
    # Secure-only — OSS self-hosters often serve over http on a LAN/loopback.
    resp.set_cookie(
        SESSION_COOKIE,
        issue_session(),
        max_age=settings.app_session_ttl_seconds,
        httponly=True,
        samesite="lax",
        path="/",
    )


@app.get("/api/auth/status", response_model=AuthStatus, tags=["auth"])
async def auth_status(request: Request):
    """Whether app-auth is on and whether this request carries a valid session.
    Public — the login gate needs it before a session exists."""
    enabled = settings.app_auth_enabled
    authed = (not enabled) or verify_session(request.cookies.get(SESSION_COOKIE))
    return AuthStatus(auth_enabled=enabled, authenticated=authed)


@app.post("/api/auth/login", response_model=AuthStatus, tags=["auth"])
async def auth_login(body: LoginRequest):
    """Exchange the operator password for a signed session cookie."""
    if not settings.app_auth_enabled:
        return AuthStatus(auth_enabled=False, authenticated=True)
    if not password_matches(body.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    resp = JSONResponse(AuthStatus(auth_enabled=True, authenticated=True).model_dump())
    _set_session_cookie(resp)
    return resp


@app.post("/api/auth/logout", response_model=AuthStatus, tags=["auth"])
async def auth_logout():
    """Clear the session cookie."""
    resp = JSONResponse(
        AuthStatus(auth_enabled=settings.app_auth_enabled, authenticated=False).model_dump()
    )
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ── In-UI console (scoped actions: set model, run auth) ────────────────────────

def _console_token_ok(token: str | None) -> bool:
    """Legacy X-Console-Token check — only consulted when app-auth is off."""
    return bool(token) and token == settings.web_console_token


def _ws_console_authorized(websocket: WebSocket, init: dict) -> bool:
    """Console/web-TTY websocket gate: session cookie when app-auth is on, else
    the legacy init token. Callers must have already checked web_console_available."""
    if settings.app_auth_enabled:
        return verify_session(websocket.cookies.get(SESSION_COOKIE))
    return _console_token_ok(init.get("token"))


def _require_console(request: Request, x_console_token: str | None = Header(default=None)) -> None:
    """Gate console actions: env flag + non-managed, then app-auth session (when
    enabled) else the legacy token. The managed exec-fence is in web_console_available."""
    if not settings.web_console_available:
        raise HTTPException(status_code=404, detail="Console is not enabled")
    if settings.app_auth_enabled:
        if not verify_session(request.cookies.get(SESSION_COOKIE)):
            raise HTTPException(status_code=401, detail="Authentication required")
        return
    if not _console_token_ok(x_console_token):
        raise HTTPException(status_code=403, detail="Invalid console token")


@app.get("/api/console/config", response_model=ConsoleConfig, tags=["console"])
async def console_config():
    """Whether the scoped console is available (token never returned)."""
    return ConsoleConfig(available=settings.web_console_available)


@app.post("/api/providers/{instance_id}/model", tags=["providers"])
async def set_provider_model(
    instance_id: str,
    body: ProviderModelUpdate,
    owner_id: str = Depends(_owner_id),
):
    """Set or clear an API instance's runtime model override (persisted).

    Owner-scoped (not console-gated): choosing an API provider's model id is benign
    config of the same sensitivity as storing its API key (POST /api/credentials,
    also owner-scoped) — and the operator setting it is already authenticated to reach
    Settings (D-0023 folds the console token into the signed-in session). The console
    fence is about *exec-in-container* actions; CLI-plan models are still owned by the
    CLI and rejected here.
    """
    from app.providers.registry import (
        get_instance,
        get_provider_def,
        set_model_override,
        set_pricing_override,
    )
    inst = get_instance(instance_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Unknown provider instance")
    pdef = get_provider_def(inst.template)
    if pdef and pdef.kind == "cli":
        raise HTTPException(
            status_code=400,
            detail="Plan-CLI model is set inside the CLI (use the console), not here.",
        )
    set_model_override(instance_id, (body.model or "").strip() or None)
    # Pricing: clear_pricing drops any override (fall back to the price book);
    # both rates present sets an explicit override; otherwise leave pricing untouched.
    if body.clear_pricing:
        set_pricing_override(instance_id, None)
    elif body.cost_in_per_mtok is not None and body.cost_out_per_mtok is not None:
        if body.cost_in_per_mtok < 0 or body.cost_out_per_mtok < 0:
            raise HTTPException(status_code=400, detail="cost per Mtok cannot be negative")
        set_pricing_override(instance_id, (body.cost_in_per_mtok, body.cost_out_per_mtok))
    logger.info("Set model for %s -> %s (owner=%s)", instance_id, body.model, owner_id)
    return {"status": "ok", "instance": instance_id, "model": body.model or None}


@app.get("/api/model-pricing", response_model=ModelPricingOut, tags=["providers"])
async def get_model_pricing(model: str):
    """Known-model price lookup so the UI can pre-populate $/Mtok rates (or know to
    ask the operator to enter them). Owner-scoped config; no secrets involved."""
    from app.providers import model_pricing
    rates = model_pricing.lookup(model)
    if rates is None:
        return ModelPricingOut(model=model, known=False)
    return ModelPricingOut(
        model=model, known=True, cost_in_per_mtok=rates[0], cost_out_per_mtok=rates[1]
    )


@app.post("/api/providers/{provider_name}/tags", tags=["providers"])
async def set_provider_tags(
    provider_name: str,
    body: ProviderTagsUpdate,
    owner_id: str = Depends(_owner_id),
):
    """Set or clear a built-in provider's routing capability-tags override (P-0044).

    Owner-scoped operator config (not console-gated), like the model override: it
    decides which tasks route to this provider. Custom providers carry their tags in
    their own record (PUT /api/custom-providers/{id}); this is for the built-in catalogue.
    """
    from app.providers.registry import get_provider_def, set_tags_override
    if get_provider_def(provider_name) is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    tags = body.capability_tags or []
    set_tags_override(provider_name, tags or None)
    logger.info("Set tags for %s -> %s (owner=%s)", provider_name, tags, owner_id)
    return {"status": "ok", "provider": provider_name, "capability_tags": tags}


@app.post("/api/providers/{instance_id}/enabled", tags=["providers"])
async def set_provider_enabled_endpoint(
    instance_id: str,
    body: ProviderEnabledUpdate,
    owner_id: str = Depends(_owner_id),
):
    """Suspend or reactivate a provider instance without deleting its auth (operator
    toggle). A disabled provider stays listed but is skipped in routing and reported
    unhealthy. Accepts any known instance id (built-in or custom)."""
    from app.providers.registry import get_instance, set_provider_enabled
    if get_instance(instance_id) is None:
        raise HTTPException(status_code=404, detail="Unknown provider instance")
    set_provider_enabled(instance_id, body.enabled)
    logger.info("Set enabled for %s -> %s (owner=%s)", instance_id, body.enabled, owner_id)
    return {"status": "ok", "provider": instance_id, "enabled": body.enabled}


@app.post("/api/usage/subscription/{instance_id}", status_code=202, tags=["console"])
async def capture_subscription_usage_endpoint(
    instance_id: str,
    background: BackgroundTasks,
    _: None = Depends(_require_console),
):
    """Kick off a plan-CLI /usage capture via the terminal seam (D-0015 #4, P-0009 #2).

    Driving the full-TTY /usage panel is slow (grok's redraw-heavy panel especially),
    so the capture runs in the **background** and this returns 202 immediately — a
    synchronous wait would otherwise trip an upstream gateway timeout (504). The
    result lands on `ProviderHealth.usage_seen_at`/`est_used_pct`; the providers
    list (polled by the UI) reflects it once the capture completes.
    """
    from app.providers.registry import get_instance, get_provider_def
    from app.subscription_usage import capture_subscription_usage
    inst = get_instance(instance_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Unknown provider instance")
    pdef = get_provider_def(inst.template)
    if not (pdef and pdef.kind == "cli"):
        raise HTTPException(
            status_code=400,
            detail="Subscription usage applies to plan-CLI instances only.",
        )
    background.add_task(capture_subscription_usage, instance_id)
    return {"status": "scheduled", "instance": instance_id}


@app.websocket("/ws/console")
async def console_websocket(websocket: WebSocket):
    """
    Token-gated PTY bridge that runs the fixed auth.sh flow for one validated
    target. First client message must be {"token","target"}; thereafter
    {"type":"input","data"} forwards keystrokes. Server emits {"type":"output"|
    "exit"|"error", ...}.
    """
    from app.console import PtyAuthSession, valid_auth_target

    await websocket.accept()
    if not settings.web_console_available:
        await websocket.send_json({"type": "error", "message": "console disabled"})
        await websocket.close()
        return

    try:
        init = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    if not _ws_console_authorized(websocket, init):
        await websocket.send_json({"type": "error", "message": "invalid token"})
        await websocket.close()
        return

    target = str(init.get("target", ""))
    if not valid_auth_target(target):
        await websocket.send_json({"type": "error", "message": f"invalid target: {target}"})
        await websocket.close()
        return

    def _dim(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(init.get(key, default))))
        except (TypeError, ValueError):
            return default

    session = PtyAuthSession(target)
    try:
        await session.start(rows=_dim("rows", 30, 4, 200), cols=_dim("cols", 100, 20, 400))
    except sandbox.SandboxUnavailableError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return
    logger.info("Console auth session started for %s", target)

    async def _pump_output() -> None:
        while True:
            data = await session.read()
            if data is None:
                break
            await websocket.send_json({"type": "output", "data": data.decode("utf-8", "replace")})

    out_task = asyncio.create_task(_pump_output())
    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "input":
                session.write(str(msg.get("data", "")))
            elif mtype == "resize":
                try:
                    session.resize(rows=int(msg["rows"]), cols=int(msg["cols"]))
                except (KeyError, TypeError, ValueError):
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("console ws error: %s", exc)
    finally:
        out_task.cancel()
        code = session.exit_code()
        session.close()
        try:
            await websocket.send_json({"type": "exit", "code": code})
            await websocket.close()
        except Exception:
            pass


@app.websocket("/ws/tty")
async def web_tty_websocket(websocket: WebSocket):
    """
    Token-gated PTY bridge for a human-driven provider CLI session (D-0016 seam #3).

    Unlike /ws/console (fixed auth.sh), this launches the provider's interactive
    TUI in the session's workspace and forwards the user's keystrokes — a human
    drives every turn (no prompt injection). First client message must be
    {"token","session","instance"}; thereafter {"type":"input"|"resize",...}.
    Server emits {"type":"output"|"exit"|"error",...}.
    """
    from app.web_tty import WebTtyError, build_web_tty_session

    await websocket.accept()
    if not settings.web_console_available:
        await websocket.send_json({"type": "error", "message": "web-tty disabled"})
        await websocket.close()
        return

    try:
        init = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    if not _ws_console_authorized(websocket, init):
        await websocket.send_json({"type": "error", "message": "invalid token"})
        await websocket.close()
        return

    try:
        session = build_web_tty_session(
            str(init.get("session", "")), str(init.get("instance", ""))
        )
    except WebTtyError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return

    def _dim(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(init.get(key, default))))
        except (TypeError, ValueError):
            return default

    try:
        await session.start(rows=_dim("rows", 30, 4, 200), cols=_dim("cols", 100, 20, 400))
    except sandbox.SandboxUnavailableError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return

    async def _pump_output() -> None:
        while True:
            data = await session.read()
            if data is None:
                break
            await websocket.send_json({"type": "output", "data": data.decode("utf-8", "replace")})

    out_task = asyncio.create_task(_pump_output())
    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "input":
                session.write(str(msg.get("data", "")))
            elif mtype == "resize":
                try:
                    session.resize(rows=int(msg["rows"]), cols=int(msg["cols"]))
                except (KeyError, TypeError, ValueError):
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("web-tty ws error: %s", exc)
    finally:
        out_task.cancel()
        code = session.exit_code()
        session.close()
        try:
            await websocket.send_json({"type": "exit", "code": code})
            await websocket.close()
        except Exception:
            pass


# ── /me/mode ──────────────────────────────────────────────────────────────────

@app.get("/api/me/mode", response_model=ModeOut, tags=["meta"])
async def get_mode():
    return ModeOut(
        mode=settings.default_cred_mode.value,
        deployment_mode=settings.deployment_mode.value,
        plan_cli_allowed=settings.plan_cli_allowed,
    )


# ── WebSocket /ws ─────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "deployment_mode": settings.deployment_mode}
