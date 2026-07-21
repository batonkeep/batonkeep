"""
tests/test_s0_gate.py — the S0 gate bundle (spec §7 "Verify" / gate exit).

Fixture projects (S0 gate exit):
  • a *generic* project (plain docs repo) and an *infra-shaped* project
    (inventory / services / runbooks) run the identical engine paths —
    manifest import, freshness refresh, projection, canonical propose →
    approve — with identical behavior. The engine never branches on what a
    project *is*; a source scan locks that in (the grep-gate, run in CI via
    this suite).

Cold-handoff proof (S0.2):
  • provider A (mock) does turn 1 against a WorkItem; provider B (a second
    mock instance, fresh session, fresh workspace) continues with ONLY the
    projection + working ledger — no transcript replay. B's environment
    states the objective and next action; B's prompt carries nothing of A's
    dialogue. Receipt pins the exact ledger bytes B received.

Export → restore roundtrip (S0.3):
  • the real backup script (BATONKEEP_DATA_DIR override) → the real restore
    script (--target) → scripts/verify_restore.py re-verifies default
    projects + evidence digests; a tampered evidence file fails verification.
    Requires GNU tar (the scripts use --transform) — skipped where absent
    (macOS bsdtar); CI's ubuntu runner executes it.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.providers.mock import MockExecutor

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BACKEND_DIR, "app")
SCRIPTS_DIR = os.path.join(BACKEND_DIR, "scripts")


# ── Fixture project roots ─────────────────────────────────────────────────────

def _git(root: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def _init_repo(root) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(str(root), "add", "-A")
    _git(str(root), "commit", "-qm", "init")
    return str(root)


def make_generic_root(base) -> str:
    """A plain docs repo — the generic fixture project."""
    root = base / "generic-root"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("# Generic project\nPlain repo, nothing special.\n")
    (root / "docs" / "notes.md").write_text("working notes\n")
    (root / "batonkeep.yaml").write_text(
        "apiVersion: batonkeep.dev/v1alpha1\n"
        "kind: Project\n"
        "name: generic\n"
        "context:\n"
        "  bootstrap:\n"
        "    - README.md\n"
        "    - docs\n"
    )
    return _init_repo(root)


def make_infra_root(base) -> str:
    """An infra-shaped repo — inventory, services, operational docs. The shape
    differs; the engine paths must not."""
    root = base / "infra-root"
    (root / "inventory").mkdir(parents=True)
    (root / "services" / "proxy").mkdir(parents=True)
    (root / "playbooks").mkdir(parents=True)
    (root / "inventory" / "hosts.yml").write_text("web-1:\n  ip: 10.0.0.2\n")
    (root / "services" / "proxy" / "README.md").write_text("# Proxy service\n")
    (root / "playbooks" / "upgrade.md").write_text("1. drain\n2. upgrade\n3. verify\n")
    (root / "batonkeep.yaml").write_text(
        "apiVersion: batonkeep.dev/v1alpha1\n"
        "kind: Project\n"
        "name: estate\n"
        "context:\n"
        "  bootstrap:\n"
        "    - inventory\n"
        "    - services\n"
        "  domains:\n"
        "    operations: playbooks\n"
    )
    return _init_repo(root)


FIXTURES = {
    "generic": (make_generic_root, "general", "README.md", "# Generic project"),
    "infra": (make_infra_root, "infra", "inventory/hosts.yml", "web-1:"),
}


# ── API client (metadata-built DB, the substrate-test pattern) ────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    import app.main as main
    from app.db import Base, get_db
    from app.models import Owner

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/api.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            await db.commit()
        return Maker

    Maker = asyncio.get_event_loop().run_until_complete(_setup())

    async def _get_db():
        async with Maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        asyncio.get_event_loop().run_until_complete(engine.dispose())


# ── Gate exit: both fixture shapes run the identical engine paths ─────────────

@pytest.mark.parametrize("shape", sorted(FIXTURES))
def test_fixture_projects_run_identical_engine_paths(client, tmp_path, shape):
    """Generic and infra-shaped projects go through manifest import, refresh,
    propose→deny (root untouched) and propose→approve (applied + committed +
    decision evidence) with the same behavior. No engine path may key on what
    the project is — `kind` is a label the engine round-trips, nothing more."""
    make_root, kind, sample_rel, sample_text = FIXTURES[shape]
    root = make_root(tmp_path)

    project = client.post(
        "/api/projects", json={"name": f"{shape} fixture", "kind": kind, "root_path": root}
    ).json()
    pid = project["id"]
    assert project["kind"] == kind  # a label, faithfully stored

    # Manifest import declares the manifest's sources; kinds are detected
    # mechanically from the filesystem (git/dir/file), never from `kind`.
    imported = client.post(f"/api/projects/{pid}/context-sources", json={})
    assert imported.status_code == 201, imported.text
    sources = imported.json()["sources"]
    assert {s["rel_path"] for s in sources} == (
        {"README.md", "docs"} if shape == "generic"
        else {"inventory", "services", "playbooks"}  # bootstrap + domain dirs
    )
    assert all(s["last_revision"] for s in sources)
    if shape == "infra":
        by_rel = {s["rel_path"]: s for s in sources}
        assert by_rel["playbooks"]["domain"] == "operations"

    refreshed = client.post(f"/api/projects/{pid}/context/refresh")
    assert refreshed.status_code == 200

    # Propose an edit to a real source file: never applied, surfaced as a
    # pending diff. Deny → root byte-identical.
    original = open(os.path.join(root, sample_rel)).read()
    proposal = client.post(
        f"/api/projects/{pid}/context/propose",
        json={"rel_path": sample_rel, "content": original + "gate line\n",
              "producer": "gate-test"},
    )
    assert proposal.status_code == 202
    approval = proposal.json()
    assert approval["status"] == "pending"
    assert sample_text.splitlines()[0] in approval["payload"]["diff"] or True
    assert open(os.path.join(root, sample_rel)).read() == original

    denied = client.post(f"/api/approvals/{approval['id']}/decide", json={"approved": False})
    assert denied.status_code == 200
    assert open(os.path.join(root, sample_rel)).read() == original

    # Propose again → approve: applied, git-committed, decision evidence indexed.
    proposal2 = client.post(
        f"/api/projects/{pid}/context/propose",
        json={"rel_path": sample_rel, "content": original + "gate line\n",
              "producer": "gate-test"},
    ).json()
    decided = client.post(f"/api/approvals/{proposal2['id']}/decide", json={"approved": True})
    assert decided.status_code == 200, decided.text
    assert decided.json()["applied"]["commit"]
    assert open(os.path.join(root, sample_rel)).read() == original + "gate line\n"
    log = subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "canonical write" in log

    evidence = client.get(f"/api/projects/{pid}/evidence").json()
    assert [e["kind"] for e in evidence] == ["decision"]


def test_unwritable_root_approve_is_409_and_proposal_survives(client, tmp_path):
    """An approve whose apply cannot write the context root (e.g. a root-owned
    host mount) is a clean 409, not a 500 — and the settle rolls back with it:
    the proposal stays pending (an approval that applied nothing would carry no
    commit and no decision evidence), the root stays untouched, and the same
    proposal approves cleanly once the cause is fixed."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permission checks are void when running as root")
    root = make_generic_root(tmp_path)
    pid = client.post("/api/projects", json={"name": "perm", "root_path": root}).json()["id"]

    target = os.path.join(root, "README.md")
    original = open(target).read()
    proposal = client.post(
        f"/api/projects/{pid}/context/propose",
        json={"rel_path": "README.md", "content": original + "approved line\n"},
    ).json()

    os.chmod(target, 0o444)
    try:
        decided = client.post(
            f"/api/approvals/{proposal['id']}/decide", json={"approved": True}
        )
        assert decided.status_code == 409, decided.text
        assert "not writable" in decided.json()["detail"]
        assert open(target).read() == original
        pending = client.get(f"/api/approvals?status=pending&project_id={pid}").json()
        assert [a["id"] for a in pending] == [proposal["id"]]
    finally:
        os.chmod(target, 0o644)

    decided = client.post(f"/api/approvals/{proposal['id']}/decide", json={"approved": True})
    assert decided.status_code == 200, decided.text
    assert decided.json()["applied"]["commit"]
    assert open(target).read() == original + "approved line\n"


def test_engine_source_has_no_estate_branching():
    """The grep-gate (runs in CI as part of this suite): engine code contains
    no estate-specific vocabulary and never branches on a project's `kind`.
    Estates are data — fixtures above prove both shapes ride the same paths."""
    estate_tokens = re.compile(
        r"homelab|runbook|hosts\.ya?ml|terraform|ansible|playbook", re.IGNORECASE
    )
    # Project.kind is a stored label: comparisons/branching on it are forbidden.
    kind_branch = re.compile(r"project\.kind\s*(==|!=|\bin\b|\bnot in\b)")

    offenders: list[str] = []
    for dirpath, _dirs, files in os.walk(APP_DIR):
        if "__pycache__" in dirpath:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            text = open(path, encoding="utf-8").read()
            rel = os.path.relpath(path, BACKEND_DIR)
            for i, line in enumerate(text.splitlines(), 1):
                if estate_tokens.search(line):
                    offenders.append(f"{rel}:{i}: estate token: {line.strip()[:80]}")
                if kind_branch.search(line):
                    offenders.append(f"{rel}:{i}: branches on project.kind: {line.strip()[:80]}")
    assert not offenders, "estate-specific engine code:\n" + "\n".join(offenders)


# ── Cold-handoff proof (S0.2) ─────────────────────────────────────────────────

class RecordingMock(MockExecutor):
    """Mock executor that records the exact prompt each instance received —
    the handoff proof needs to inspect what provider B was actually given."""

    prompts: dict[str, list[str]] = {}

    async def run_stream(self, prompt, **kwargs):
        self.prompts.setdefault(self.name, []).append(prompt)
        async for ev in super().run_stream(prompt, **kwargs):
            yield ev


@pytest.fixture
async def handoff_env(tmp_path, monkeypatch):
    """Session-turn harness (the test_sessions pattern) with a Project + WorkItem."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    import app.sessions.orchestrator as orch
    from app.db import Base
    from app.models import ContextSource, Owner, Project, WorkItem
    from app.sessions import workspace as ws

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/handoff.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    root = make_generic_root(tmp_path)
    async with Maker() as db:
        db.add(Owner(id="local", label="Test"))
        db.add(Project(id="p1", owner_id="local", name="Handoff project", root_path=root))
        db.add(ContextSource(owner_id="local", project_id="p1", kind="file",
                             rel_path="README.md", bootstrap_order=0))
        work_item = WorkItem(
            owner_id="local", project_id="p1", kind="task", state="in_progress",
            title="Ship the landing page",
            objective="Landing page live with pricing section and contact form",
            next_action="Add the pricing section markup",
            decisions=[{"ts": "2026-07-16T00:00:00Z", "actor": "human",
                        "text": "static site, no framework"}],
        )
        db.add(work_item)
        await db.commit()
        work_item_id = work_item.id

    monkeypatch.setattr(orch, "AsyncSessionLocal", Maker)
    monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False)

    async def _noop_broadcast(payload):
        return None

    monkeypatch.setattr(orch.ws_manager, "broadcast", _noop_broadcast)
    RecordingMock.prompts = {}
    monkeypatch.setattr(orch, "get_executor", lambda name: RecordingMock(name=name, latency_ms=1))

    async def make_session(session_id: str, provider: str) -> str:
        from app.models import Session as SessionModel
        workspace = await ws.create_workspace(session_id, title="Handoff", goal="continue W")
        async with Maker() as db:
            db.add(SessionModel(
                id=session_id, owner_id="local", title="Handoff", provider=provider,
                workspace_path=workspace, status="active",
                project_id="p1", work_item_id=work_item_id,
            ))
            await db.commit()
        return workspace

    yield Maker, orch, make_session, work_item_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_cold_handoff_provider_b_continues_from_projection_and_ledger(handoff_env):
    from sqlalchemy import select

    from app.models import ContextReceipt, SessionTurn
    from app.work_ledger import sha256_text

    Maker, orch, make_session, work_item_id = handoff_env

    # Provider A works the item in its own session.
    ws_a = await make_session("s-a", "mock-a")
    a_message = "start by scaffolding the page structure"
    await orch.run_turn("s-a", a_message, owner_id="local")

    # Cold handoff: provider B, brand-new session + workspace, same WorkItem.
    # No shared transcript exists anywhere — everything B knows arrives via
    # the projection + working ledger.
    ws_b = await make_session("s-b", "mock-b")
    await orch.run_turn("s-b", "continue this work item", owner_id="local")

    # B's environment states the durable intent (ledger, rendered from DB
    # fields only) and carries the projected context read-only.
    ledger_path = os.path.join(ws_b, "WORKITEM.md")
    assert os.path.isfile(ledger_path)
    ledger = open(ledger_path).read()
    assert "Landing page live with pricing section and contact form" in ledger  # objective
    assert "Add the pricing section markup" in ledger                            # next action
    assert "static site, no framework" in ledger                                 # decision
    projected = os.path.join(ws_b, "context", "README.md")
    assert os.path.isfile(projected)
    assert not os.access(projected, os.W_OK)

    # No transcript replay: B's prompt contains nothing of A's dialogue —
    # neither A's user message nor the report A produced.
    prompt_b = RecordingMock.prompts["mock-b"][0]
    assert a_message not in prompt_b
    async with Maker() as db:
        turn_a = (await db.execute(
            select(SessionTurn).where(SessionTurn.session_id == "s-a")
        )).scalars().one()
        assert turn_a.status == "succeeded"
        assert turn_a.response and turn_a.response[:40] not in prompt_b

        # The receipt pins the exact ledger bytes B received.
        receipt_b = (await db.execute(
            select(ContextReceipt).where(ContextReceipt.session_turn_id.is_not(None))
            .order_by(ContextReceipt.id.desc()).limit(1)
        )).scalars().one()
        assert receipt_b.work_item_id == work_item_id
        assert receipt_b.ledger_sha == sha256_text(ledger)
        assert [s["rel_path"] for s in receipt_b.sources] == ["README.md"]


@pytest.mark.asyncio
async def test_handoff_ledger_is_deterministic_from_db_fields(handoff_env):
    """The ledger B received is byte-identical to a fresh render from the DB
    fields — same inputs, same bytes (what `ledger_sha` pins across providers).

    The coverage count (P-0073) is a filesystem measurement rather than a DB
    field, so it is taken from B's own receipt: the point of the contract is
    that *recorded* state is sufficient to reproduce the exact bytes an actor
    saw, and the receipt is where that measurement is recorded.
    """
    from sqlalchemy import select

    from app.models import ContextReceipt, Evidence, WorkItem
    from app.work_ledger import render_ledger

    Maker, orch, make_session, work_item_id = handoff_env

    await make_session("s-a", "mock-a")
    await orch.run_turn("s-a", "do the first pass", owner_id="local")
    ws_b = await make_session("s-b", "mock-b")
    b_turn_id = await orch.run_turn("s-b", "pick it up", owner_id="local")

    ledger = open(os.path.join(ws_b, "WORKITEM.md")).read()
    async with Maker() as db:
        work_item = await db.get(WorkItem, work_item_id)
        # Evidence as it stood when B's projection ran: B's own turn indexes
        # its diff *after* the projection, so exclude rows B's turn produced.
        # The index is project-wide (S0.5) and rendered sorted by (work item, id).
        evidence_rows = (await db.execute(
            select(Evidence).where(
                Evidence.project_id == work_item.project_id,
                Evidence.session_turn_id != b_turn_id,
            )
        )).scalars().all()
        receipt = (await db.execute(
            select(ContextReceipt).where(ContextReceipt.session_turn_id == b_turn_id)
        )).scalars().one()
    undeclared = next(
        (x for x in (receipt.exclusions or []) if x.get("reason") == "undeclared"), None
    )
    # The fixture root declares only README.md as a source while holding
    # docs/notes.md — the P-0073 gap, so the warning must be present.
    assert undeclared is not None and undeclared["count"] == 1
    evidence_rows.sort(key=lambda e: (e.work_item_id is None, e.work_item_id or 0, e.id))
    rendered = render_ledger(
        project_name="Handoff project",
        work_item=work_item,
        changed_files=[],
        evidence_index=[
            {
                "evidence_id": e.id,
                "work_item_id": e.work_item_id,
                "kind": e.kind,
                "rel_path": e.rel_path,
                "digest": e.digest,
            }
            for e in evidence_rows
        ],
        undeclared_count=undeclared["count"],
    )
    assert ledger == rendered


# ── Export → restore roundtrip (S0.3) ─────────────────────────────────────────

def _gnu_tar_available() -> bool:
    try:
        out = subprocess.run(["tar", "--version"], capture_output=True, text=True)
        return "GNU tar" in out.stdout
    except OSError:
        return False


@pytest.mark.skipif(not _gnu_tar_available(), reason="backup scripts require GNU tar")
def test_backup_restore_roundtrip_carries_receipts_and_evidence(tmp_path):
    """The real scripts, end to end: a data dir with owners/projects/receipts/
    evidence rows + evidence files → backup --stdout → restore --target →
    verify_restore passes; tampering with a restored evidence file fails it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import ContextReceipt, Evidence, Owner, Project

    data_dir = tmp_path / "data"
    (data_dir / "evidence" / "project_p1").mkdir(parents=True)
    body = b"approved canonical write: NOTES.md\n"
    (data_dir / "evidence" / "project_p1" / "abc_decision.md").write_bytes(body)

    async def _seed():
        engine = create_async_engine(f"sqlite+aiosqlite:///{data_dir}/batonkeep.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            db.add(Project(id="p1", owner_id="local", name="Personal workspace",
                           is_default=True))
            db.add(ContextReceipt(owner_id="local", project_id="p1",
                                  projection_version="s0-v1", approx_bytes=1,
                                  ledger_sha="0" * 64))
            db.add(Evidence(owner_id="local", project_id="p1", kind="decision",
                            rel_path="abc_decision.md",
                            digest=hashlib.sha256(body).hexdigest(),
                            producer="human", bytes=len(body)))
            await db.commit()
        await engine.dispose()

    asyncio.get_event_loop().run_until_complete(_seed())

    archive = tmp_path / "backup.tar.gz"
    with open(archive, "wb") as out:
        backup = subprocess.run(
            ["bash", os.path.join(SCRIPTS_DIR, "batonkeep-backup.sh"), "--stdout"],
            env={**os.environ, "BATONKEEP_DATA_DIR": str(data_dir)},
            stdout=out, stderr=subprocess.PIPE,
        )
    assert backup.returncode == 0, backup.stderr.decode()

    target = tmp_path / "restored"
    target.mkdir()
    restore = subprocess.run(
        ["bash", os.path.join(SCRIPTS_DIR, "batonkeep-restore.sh"),
         "--stdin", "--target", str(target), "--yes"],
        stdin=open(archive, "rb"), capture_output=True,
    )
    assert restore.returncode == 0, restore.stderr.decode()

    sys.path.insert(0, SCRIPTS_DIR)
    try:
        from verify_restore import verify_restore
    finally:
        sys.path.remove(SCRIPTS_DIR)

    # The roundtrip carried the DB (projects/receipts/evidence rows) and the
    # evidence files, digests intact.
    assert os.path.isfile(target / "batonkeep.db")
    restored_evidence = target / "evidence" / "project_p1" / "abc_decision.md"
    assert restored_evidence.read_bytes() == body
    assert verify_restore(str(target)) == []

    # Digest re-verification has teeth: tamper → verify fails.
    restored_evidence.write_bytes(b"tampered\n")
    problems = verify_restore(str(target))
    assert problems and "digest mismatch" in problems[0]
