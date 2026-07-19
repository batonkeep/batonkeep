# Projects: durable work, evidence, and the knowledge loop

A **Project** is batonkeep's durable unit of work. Tasks and build sessions run *under* a
project; what they produce — decisions, evidence, artifacts, provider history — belongs to
the project and outlives any single session or provider. This page covers the knowledge
loop: how finished work becomes evidence, how evidence reaches the next operator, and how
reviewed material is promoted into the project's canonical context.

## The pieces

- **Work items** — durable units of intent under a project (objective, state, decisions,
  next action). A run or session may be bound to one; that binding is what puts the
  `WORKITEM.md` working ledger in the agent's workspace.
- **Context root** — an optional directory (server-managed or bring-your-own) holding the
  project's canonical files, declared in `batonkeep.yaml`. Declared sources are projected
  **read-only** into every run's workspace under `context/`; agents never write them
  directly.
- **Evidence** — the append-only outcome record: task-run reports, session diffs, approval
  decisions, packages. Every row carries a sha256 digest of the exact bytes captured.
  Evidence is created and read; there is no update path.
- **Context receipts** — for every run/turn, a record of exactly what context the actor
  received: source revisions, the ledger hash, the evidence index, materialized inputs, and
  anything excluded (with the reason). Paths and hashes only, never content.

## Capture a workspace package

From a build session's **Files** tab, **Capture package** snapshots the workspace at its
latest committed version into the project's evidence:

- a **`package`** — a zip of the workspace tree with `MANIFEST.json` at its root listing
  every file's sha256, the commit id, and the producer;
- a **`manifest`** — the same JSON as a standalone, viewable evidence row.

Harness files (the session brief, the projected `context/`, provider convention files) and
package-manager directories are excluded — the package is the artifact, not the scaffolding.
Capture is idempotent per workspace version: repackaging an unchanged workspace returns the
existing rows. A workspace with uncommitted changes is refused (run or complete a turn
first — the engine owns the commit boundary).

## Hand work to the next operator (pins)

Each work item has **pinned evidence**: a curated list of inputs the next operator needs.
When an execution is bound to that work item, its pinned evidence is **materialized
read-only** into the workspace under `context/evidence/`, within its own byte budget, and
each file's digest is re-verified at copy time — altered or missing evidence is excluded
and recorded on the receipt rather than silently propagated.

Pin in one step when capturing ("pin to work item" on the capture action, or
`pin_to_work_item_id` on the API), or manage pins later via the work-item API
(`pinned_evidence: [ids]`; an empty list clears them).

The working ledger every bound execution receives lists the **whole project's evidence
index** (origin-tagged per work item, with ids), plus an *Inputs* section pointing at the
materialized pins — so a cold operator can see everything that happened and hold the
artifacts that matter.

## Promote evidence into canonical context

Canonical context changes only through an approved proposal. There are two ways to propose:

- **Inline** — propose text content for a path in the context root (prose/config-sized).
- **By reference** — promote an evidence row (the **propose** action on the Evidence tab,
  or `evidence_id` on the propose API). The content never transits the approval record:
  the evidence digest is pinned when you propose and **re-verified when you approve** — if
  the stored file changed in between, the apply refuses and the proposal stays pending.

Approving applies the write to the context root (and commits it on git roots), re-hashes
the touched source's freshness, and captures the applied diff as `decision` evidence.
Large artifacts (packages) are refused at the canonical door by a size ceiling — promote
the manifest or extracted files; the package itself stays evidence.

## Viewing

Declared context sources, evidence, and proposal diffs all open in the same viewer
(markdown rendered, diffs colorized, text as-is), with raw download links throughout.

## Configuration

| env | default | bounds |
|---|---|---|
| `PACKAGE_MAX_BYTES` | 64 MiB | workspace package ceiling |
| `CONTEXT_EVIDENCE_MAX_BYTES` | 32 MiB | pinned-evidence materialization budget per run |
| `EVIDENCE_INDEX_MAX_ROWS` | 100 | ledger evidence-index cap (newest kept) |
| `EVIDENCE_PIN_MAX` | 32 | pinned items per work item |
| `CANONICAL_MAX_FILE_BYTES` | 2 MiB | by-reference promotion ceiling |
| `CONTEXT_PROJECTION_MAX_BYTES` | 10 MiB | declared-source projection budget per run |
