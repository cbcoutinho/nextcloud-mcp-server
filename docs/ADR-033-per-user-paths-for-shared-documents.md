# ADR-033: Per-user paths for shared documents

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deck:** board 12 card #737 (this change); card #739 (loose prefix match);
  card #740 (folder-ancestor filtering — Phase 3 here)
- **Related:** ADR-027 (rich search filters), ADR-026 (pluggable database
  backend), note 391147 (Qdrant outage 2026-07-19, where the thrash was found)

## Context

A file visible to more than one user has **one** Nextcloud `fileid` and one
`etag` for everyone, and our chunk point IDs are user-agnostic
(`uuid5(...doc_id, chunk_index)` — `vector/payload_keys.py`). So the tenant-wide
dedup (`vector/sharing_state.py::claim_existing_index`) intentionally stores
**one point set per `doc_id`**, shared by every reader via the `acl_principals`
observed-access set. That dedup is correct and must be preserved — it is what
stops N users re-embedding identical content N times.

The problem is that each shared point carries a **single scalar `file_path`**,
but the users mount the same file at *different paths* (a share is mounted under
each recipient's own root). Every user's scan calls `claim_existing_index(...,
current_path=<their path>)`, which calls `reconcile_document_path`, which
rewrites the scalar to the caller's path. Nothing is being renamed, yet the
value flips back and forth once per user per scan pass:

```
Reconciled path for file_<id> after rename/move: '/A/doc.pdf' -> '/B/A/doc.pdf'
Reconciled path for file_<id> after rename/move: '/B/A/doc.pdf' -> '/A/doc.pdf'
```

Observed on blackbox-demo, 2026-07-20, service 0.142.0.

### Impact

1. **Write amplification** — a `set_payload` against Qdrant for every shared
   doc, for every user, on every scan pass (300 s interval). `file_path` is
   doc-level data duplicated across every chunk, so one reconcile rewrites every
   chunk of the document (this tenant: 2,832 docs but ~288k chunks).
2. **Non-deterministic `file_path`** — it reflects whichever user scanned last.
   This silently breaks the `path_prefix` / `path_prefixes` search filters
   (`search/access_filter.py`): a user filtering on their own folder can miss
   documents they can see, or match on a path that is not theirs.
3. **Loose prefix semantics (card #739)** — the filter is
   `MatchText(key="file_path")`, which is token-containment (server Qdrant) or
   substring (embedded), never a left-anchored prefix. `/Reports` matches any
   path containing the `Reports` token at any depth.

## Decision

**Access control stays in Qdrant; per-user *display* metadata moves to the
relational store; path *filtering* becomes user-agnostic folder-ancestor
containment in Qdrant.** Concretely, every predicate that participates in the
Qdrant ANN traversal is resolved to a user-agnostic, index-resolved, corpus-flat
term inside Qdrant (`acl_principals` for visibility, `folder_ancestors` for
scoping). The relational DB holds only **non-security** metadata and is **never
a filter**:

1. a `path_prefix -> folder_id` resolution (one small lookup *before* the query);
2. the per-user *display* path (joined onto the ~10 returned results, *after*
   retrieval).

Both relational reads are bounded and tiny, so a store inconsistency degrades a
**displayed path**, never a permission or a retrieval result. Qdrant is the
system of record; the relational table is a derived cache. That asymmetry is
what makes the split safe (contrast the 2026-07-19 partial-write incident, where
a dual-write *did* affect indexed data).

We deliberately keep hybrid dense+BM25 fusion in Qdrant rather than moving
everything to pgvector — the RRF/DBSF fusion is native to the engine and not
worth rebuilding at current scale.

### Why not just keep the scalar `file_path` as the filter field?

Because the scalar can only ever be correct for **one** user, and path filtering
needs to be correct for **every** reader of a shared file. A shared *folder* has
one canonical Nextcloud `fileid` that is identical for all users who mount it, so
the **ancestor folder-ID chain is user-agnostic for the shared portion** — which
is exactly the term that makes filtered-ANN both correct and flat in corpus size.

## Design

### Phase 1 — Deterministic canonical `file_path` (stop the thrash)

Pin the scalar `file_path`/`title` to the **indexer of record** = the point's
`owner_id`. `reconcile_document_path` is only allowed to rewrite when the caller
*is* the canonical owner (a genuine owner-side rename), or when the stored path
is empty (legacy backfill). A reader who observes the file at a different mount
path no longer overwrites it.

- No schema change, no new store.
- Idempotent scan: repeated passes over an unchanged shared file issue **no**
  `set_payload` and log no `Reconciled path`.
- The scalar becomes deterministic (always the owner's path). Non-owner display
  paths are handled in Phase 2; non-owner *filtering* in Phase 3.

`claim_existing_index` gains the existing point's `owner_id` (already fetched in
the dedup payload — no extra round-trip) and passes it to
`reconcile_document_path`, which becomes a no-op unless
`caller_user_id == owner_id`.

### Phase 2 — Relational per-user path store (display path)

New table `document_paths`, one row per `(doc_type, doc_id, user_id)`:

| column      | type                | note                                  |
|-------------|---------------------|---------------------------------------|
| `doc_type`  | Text (PK)           | e.g. `file`                           |
| `doc_id`    | Text (PK)           | Nextcloud fileid                      |
| `user_id`   | Text (PK)           | the reader                            |
| `file_path` | Text                | this user's mount path                |
| `updated_at`| BigInteger (epoch s)| observability / staleness            |

Portable types only (Text + epoch BigInteger), so the one migration runs on both
self-host SQLite and cloud Postgres via the ADR-026 abstraction — exactly like
`batch_ocr_jobs` (migration 008).

- **Write:** the scanner upserts `(doc_type, doc_id, user_id) -> current_path`
  for every file it sees (dedup hit *or* fresh index). One row per doc per user,
  updated with a single `INSERT ... ON CONFLICT DO UPDATE`. This replaces the
  per-chunk `set_payload` thrash with a single bounded relational write.
  **Write-volume trade-off:** the upsert is currently *unconditional*, so the
  table holds ~Σ(files × readers) rows rewritten each scan pass — including for
  single-owner content, which does not strictly need a row (the owner's path is
  the Qdrant scalar the fallback serves). This is an accepted swap of a small
  idempotent relational upsert for the per-chunk Qdrant write it removes; scoping
  the upsert to genuine cross-user readers (see *Follow-ups*) would make the
  table sparse and is deferred.
- **Read:** after Qdrant returns the top-K, batch-fetch the querying user's rows
  for the returned `doc_id`s and override the displayed `file_path`/`title`.
  Falls back to the Qdrant scalar when no row exists (legacy / not-yet-scanned).
- **Backfill:** none required up front — the table fills as scans run, and the
  Qdrant scalar is the fallback. Qdrant remains the source of truth.

### Phase 3 — Folder-ancestor filtering (card #740)

Add a user-agnostic `folder_ancestors` payload key: the list of ancestor folder
`fileid`s (as strings) of the file's canonical path, KEYWORD-indexed in Qdrant.

- **Resolution:** at index time, resolve each ancestor folder's `fileid` via a
  `PROPFIND` (Depth 0, `oc:fileid` — `WebDAVClient.get_fileid`) walking up the
  file's path. `resolve_folder_ancestors` takes a `cache` dict so shared parent
  folders resolve once. The scanner threads one per-scan-pass cache into the
  lazy backfill (`claim_existing_index`); the fresh-index write runs in the
  queue worker, one `DocumentTask` at a time (decoupled from the scan pass), so
  it resolves per-document — a per-worker TTL cache is the follow-up if the bulk
  initial scan's PROPFIND volume shows up. Ancestors of the *shared* folder
  resolve to the same `fileid`s for every user.
- **Write:** stamp `folder_ancestors` on every **real (non-placeholder) file
  chunk** in the processor, alongside the existing scalar `file_path`.
  Placeholder points are excluded from search (`get_placeholder_filter`), so they
  carry no scope key.
- **Filter:** `path_prefix` is resolved to a `folder_id` (the prefix path's own
  `fileid`, via `resolve_prefix_folder_ids` → `get_fileid`), then the Qdrant
  filter ORs `MatchAny(key="folder_ancestors", any=[folder_id])` with the legacy
  `MatchText(file_path)` branch. The `MatchAny` branch is a **true containment**
  filter (fixes card #739's loose match), applied *inside* HNSW traversal
  (filtered-ANN, the only sound ordering), and user-agnostic; the `MatchText`
  branch is retained as a fallback for points that predate `folder_ancestors`
  (until backfilled) and for free-text path search. **Cost:** resolution is a
  synchronous PROPFIND per distinct prefix on the search path (currently
  uncached — a per-user TTL cache like `list_accessible_owners` is the obvious
  follow-up if it shows up in latency).
- **Backfill:** rather than a separate admin job (which would lack a per-user
  WebDAV client to resolve fileids *as that user*), the backfill is **lazy and
  owner-scoped**, folded into `claim_existing_index`: on a dedup hit for a
  pre-Phase-3 document, when the scanning user is the owner and the key is
  absent, the owner's canonical path is resolved and written once
  (`set_folder_ancestors`). It reuses the payload already fetched for the dedup
  (no extra scroll), never re-embeds, and is owner-only so a reader's mount
  prefix never pollutes the (bounded, user-agnostic) ancestor set. Until an owner
  scan backfills a given doc, search degrades to the `MatchText(file_path)`
  fallback for it. **The live behaviour of the new KEYWORD index + `MatchAny`
  selectivity should still be validated against a real Qdrant** (only meaningfully
  verifiable on a real instance — see the design thread on card #737).

### Measured budget (card #737 comments, throwaway Qdrant v1.18.3)

`MatchAny` filtering is effectively free at doc granularity across the whole
corpus; cost is driven by **list length**, ~1 ms / 1,000 ids. The
`path_prefix -> folder_id` resolution yields a *single* id, so the filter list is
tiny and the measured threshold never binds. The prefilter approach that *would*
blow up (passing a per-user `MatchAny(doc_id, [...])` of every visible doc) is
explicitly rejected in favour of the single-`folder_id` containment term.

## Consequences

- **Positive:** the thrash and its write amplification stop at Phase 1; display
  paths become per-user-correct at Phase 2; path filtering becomes correct for
  every reader and truly left-anchored at Phase 3. Dedup (one point set per
  `doc_id`) is preserved throughout. No re-embedding at any phase.
- **Negative / risk:** Phase 2 introduces a second store; we mitigate by making
  it a *derived, non-security* cache (Qdrant is system of record) so an
  inconsistency can only mis-display a path. Phase 3's backfill touches live
  collection payloads and must be validated on a real Qdrant before running
  against a tenant.
- **Migration:** existing collections need no re-embed. Phase 2's table
  self-populates as scans run. Phase 3's `folder_ancestors` is forward-written
  immediately for new/re-indexed docs and lazily backfilled for old points by the
  owner's next scan (owner-scoped, in `claim_existing_index`); searches degrade
  gracefully to the `file_path` fallback in the interim.

## Deferred follow-ups (accepted, not part of this change)

These are known, non-blocking, and each documented at its call site:

1. **Scope the `document_paths` upsert to genuine cross-user readers.** Today the
   scanner upserts unconditionally for every (user, file) on every scan, so the
   table is not sparse (see the migration's write-volume note). Owner content
   needs no row — the owner-pinned Qdrant scalar is the fallback — so upserting
   only when the scanning user is *not* the document's `owner_id` (a real reader
   of a shared file) would make the table idle for single-owner corpora without
   changing displayed results.
2. **Cache / parallelize the search-time `path_prefix -> folder_id` resolution.**
   `resolve_prefix_folder_ids` issues one synchronous `PROPFIND` per prefix in a
   sequential loop on the search hot path. A per-user TTL cache (like
   `list_accessible_owners`) and/or an `anyio` task group to resolve prefixes
   concurrently would cut worst-case latency; prefix count is typically 1–2, so
   this is low priority.
3. **Share an ancestor-resolution cache on the fresh-index path.** The processor
   resolves `folder_ancestors` per `DocumentTask` (queue worker, decoupled from
   the scan pass), so sibling files under one tree don't share lookups the way
   the scanner-side backfill does. A per-worker TTL cache is the fix if the bulk
   initial scan's PROPFIND volume shows up.

## Future cleanup (version-gated, NOT part of this change)

This change is **purely additive** — it removes no index or payload field. It
does, however, set up two retirements that a **later version** can make, each
discoverable by version (per "gate on versions, never on merge order") and each
with a hard precondition that is unsafe to skip:

1. **Retire the `file_path` TEXT payload index** (`qdrant_client.py`
   `_PAYLOAD_INDEX_FIELDS`). Precondition: every point carries `folder_ancestors`
   (a completed backfill across all collections) **and** the `MatchText(file_path)`
   fallback branch in `build_base_filter_conditions` has been dropped **and**
   free-text path search has been re-homed off `file_path`. Until all three hold,
   dropping the index breaks folder scoping / free-text path search for
   un-backfilled points.
2. **Drop the `MatchText(file_path)` fallback branch** in the folder-scope filter
   (and, optionally, stop writing the scalar `file_path` once per-user display
   paths are sourced entirely from `document_paths` with a guaranteed backfill).
   Precondition: same completed `folder_ancestors` backfill, plus a
   `document_paths` row guaranteed for every (user, file) that can be returned.

Neither is safe today: un-backfilled points would silently drop out of
folder-scoped results, and files with no `document_paths` row would lose their
display path. The trigger to do the cleanup is "a `folder_ancestors` backfill has
run to completion on all live collections" — record that in the `BREAKING CHANGE:`
footer / CHANGELOG of the version that performs the removal, and gate any
consumer on the advertised capability rather than assuming the removal shipped.

## Test strategy (repo test gate)

- **Unit:** `reconcile_document_path` owner-gating (Phase 1); `document_paths`
  upsert/read + fallback (Phase 2); `folder_ancestors` filter construction and
  ancestor resolution (Phase 3).
- **Integration (full-stack Docker + live Qdrant):** the multi-user shared-file
  convergence test — index a file shared A+B at different paths, assert repeated
  scans produce no `Reconciled path` writes, each user's search returns their own
  display path, and each user's `path_prefix` on their own folder matches while
  the other's does not (card #737 acceptance criteria; extends the release-
  convergence test tracked by card #665).
- **Contract:** no cross-service boundary changes shape here; `/api/v1/search`
  response fields are unchanged (per-user path is substituted into the existing
  `file_path`/`title`), so the existing provider verification continues to hold.
