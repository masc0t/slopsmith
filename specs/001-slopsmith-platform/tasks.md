---
description: "Retrospective task list for the Slopsmith platform"
---

# Tasks: Slopsmith Platform

**Input**: [`spec.md`](./spec.md), [`plan.md`](./plan.md), [`clarify.md`](./clarify.md)

> **Retrospective.** This is not a greenfield build plan; the platform
> already exists. Tasks are framed as **DONE** (existing functionality,
> with file references for traceability) and **OPEN** (gaps, known TODOs
> from `CLAUDE.md` / `CHANGELOG.md`, future improvements). Tasks are
> grouped by user story (US1–US8 from `spec.md`).

## Format: `[ID] [P?] [Story] [Status] Description`

- **[P]**: Task touches a different file than its siblings and can run in parallel
- **[Story]**: Maps to `spec.md` user stories US1–US8
- **[DONE]**: Already shipped — file references show where
- **[OPEN]**: Not done; rationale for inclusion follows

---

## Phase 0: Foundation (Shared Infrastructure) — DONE

These are the cross-cutting pieces every user story depends on.

- [x] T001 [DONE] FastAPI app with `CorrelationIdMiddleware` + `_demo_mode_guard` — `server.py:14-225`
- [x] T002 [DONE] `MetadataDB` SQLite wrapper with `threading.Lock` — `server.py:226`
- [x] T003 [DONE] [P] Structured logging bootstrap (LOG_LEVEL/LOG_FORMAT/LOG_FILE env vars + JSON formatter) — `lib/logging_setup.py`, `CHANGELOG.md` "Structured logging bootstrap"
- [x] T004 [DONE] [P] `pyproject.toml` sets `pythonpath = [".", "lib"]`, `testpaths = ["tests"]` — flat-import convention
- [x] T005 [DONE] [P] Dockerfile + docker-compose.yml with vgmstream / FFmpeg / FluidSynth / RsCli baked in
- [x] T006 [DONE] CI workflow runs pytest on Python 3.12 against every push/PR to `main` — `.github/workflows/tests.yml`
- [x] T007 [DONE] [P] `VERSION` file + `/api/version` endpoint + auto-bump workflow — `server.py:1340`, `.github/workflows/sync-version.yml`
- [x] T008 [DONE] Migrated `print()` / `traceback.print_exc()` calls in `server.py` and `lib/` to `logging` — commit `3b08488` (PR #159); regression guarded by `.github/workflows/tests.yml`
- [ ] T009 [OPEN] Document `MetadataDB` schema migration policy in `CLAUDE.md` (constitution mandates additive/idempotent migrations but the in-startup migration logic is not explicitly described)

**Checkpoint**: Foundation in place — every user story below sits on top of this.

---

## Phase 1: User Story 1 — Browse a personal CDLC library at scale (P1)

**Goal**: a guitarist can point Slopsmith at their DLC folder and browse it
end-to-end without the in-game UI.

### DONE

- [x] T020 [US1] In-memory PSARC scan via `read_psarc_entries` — `lib/psarc.py`
- [x] T021 [US1] Metadata extraction fast path (`_extract_meta_fast`) — `server.py:690`
- [x] T022 [US1] Sloppak metadata extraction (`_extract_meta_sloppak`) — `server.py:781`
- [x] T023 [US1] Non-blocking background scan with progress queue — `server.py:914,867,895`
- [x] T024 [US1] 8-thread parallel scan (per README "Scalability")
- [x] T025 [US1] [P] `/api/library` paginated/filterable/searchable endpoint — `server.py:1456`
- [x] T026 [US1] [P] `/api/library/artists` artist+album tree endpoint — `server.py:1478`
- [x] T027 [US1] [P] `/api/library/stats` for letter bar A–Z — `server.py:1499`
- [x] T028 [US1] [P] `/api/library/tuning-names` ordered by musical distance from E Standard — `server.py:1518` (slopsmith#22)
- [x] T029 [US1] Filters drawer (arrangements / stems / lyrics / tunings) with active-filter chips — slopsmith#129, #69
- [x] T030 [US1] Year sort (newest/oldest), tuning sort by musical distance — slopsmith#22, #128
- [x] T031 [US1] [P] Favourites toggle + persistence — `server.py:1550`
- [x] T032 [US1] [P] Edit metadata (title/artist/album/album-art) from the library
- [x] T033 [US1] Retune to E Standard from the library card (covered fully in US6)
- [x] T034 [US1] Server-side pagination at scale (README claims 80k+ songs)

### OPEN

- [ ] T040 [US1] [OPEN] Document tested scaling envelope (memory, scan time per N songs) — currently empirical; `[NEEDS CLARIFICATION]` in spec FR-038/SC-001
- [ ] T041 [US1] [OPEN] Background rescan migration for songs scanned before the `stem_ids` / `tuning_name` / `tuning_sort_key` columns existed — `CHANGELOG.md` "Migration notes" mentions Full Rescan recovery, but a one-shot lazy backfill would be friendlier
- [ ] T042 [US1] [OPEN] Surface plugin-load failures in the library UI (today they only appear in logs and the diagnostics bundle)

**Checkpoint**: US1 is shippable on its own — the library browser is a complete product without US2.

---

## Phase 2: User Story 2 — Play a song on the note highway (P1)

**Goal**: click a song, see it play, switch arrangements, change speed and viz.

### DONE

- [x] T050 [US2] `/ws/highway/{filename}?arrangement=N` WebSocket handler — `server.py:2906`
- [x] T051 [US2] WebSocket message protocol: loading → song_info → beats → sections → anchors → chord_templates → lyrics → tone_changes? → notes → chords → phrases? → ready (per `CLAUDE.md` "WebSocket Protocol Reference")
- [x] T052 [US2] PSARC unpack on play (`unpack_psarc`) with disk cache — `lib/psarc.py`
- [x] T053 [US2] SNG → XML conversion via bundled RsCli for official disc DLC
- [x] T054 [US2] [P] `createHighway()` factory in `static/highway.js` (canvas, rAF, ctx)
- [x] T055 [US2] [P] Note rendering: fret-positioned notes, string colors, sustains, chord brackets, chord name labels
- [x] T056 [US2] [P] Technique rendering: bends (1/2, full, 1-1/2, 2), unison bends, slides, HO/PO/T, palm mutes, tremolo, accents, harmonics, pinch harmonics
- [x] T057 [US2] [P] Lyrics overlay (phrase-based, multi-row, karaoke highlighting)
- [x] T058 [US2] Dynamic anchor zoom (looks ahead at upcoming notes)
- [x] T059 [US2] Arrangement switcher mid-session (`song_info.arrangements`)
- [x] T060 [US2] Speed control (0.25×–1.50×) and volume control
- [x] T061 [US2] [P] Built-in 2D highway renderer (default) — `static/highway.js`
- [x] T062 [US2] [P] Built-in 3D highway renderer plugin — `plugins/highway_3d/`
- [x] T063 [US2] `setRenderer` contract — `init/draw/resize/destroy`, `contextType` declaration, canvas swap via `cloneNode(false)` + `replaceWith` (slopsmith#36)
- [x] T064 [US2] `highway:canvas-replaced` event for plugins holding stale canvas refs
- [x] T065 [US2] `matchesArrangement(songInfo)` predicate + Auto mode in viz picker
- [x] T066 [US2] Public `highway.get*` getters (notes/chords/beats/songInfo/lefty/inverted/stringCount/chordTemplates) for overlay plugins
- [x] T067 [US2] [P] Keyboard shortcuts: Space/←/→/Esc/[/]/? + plugin-registered shortcuts via `window.registerShortcut`
- [x] T068 [US2] [P] Phrases multi-difficulty slider (slopsmith#48) — chunked `phrases` message
- [x] T069 [US2] Tone changes streaming (when present in source chart)

### OPEN

- [ ] T080 [US2] [OPEN] Publish a target browser matrix (Chromium/Firefox/Safari versions, WebGL2 minimums, Web MIDI requirements) — currently `[NEEDS CLARIFICATION]` in spec FR-037
- [ ] T081 [US2] [OPEN] Document the published WebSocket-connection ceiling and any uvicorn tuning needed to reach it — currently `[NEEDS CLARIFICATION]` in spec FR-039
- [ ] T082 [US2] [OPEN] Test renderer-swap edge cases when a plugin holds onto the canvas via `addEventListener` directly (`CLAUDE.md` warns about this; no automated test today)
- [ ] T083 [US2] [OPEN] Performance benchmark for highway @ 60 fps across renderer types and `stringCount` ≥ 7

**Checkpoint**: US1 + US2 together = a fully usable Slopsmith. Everything below is incremental value.

---

## Phase 3: User Story 3 — Practice with loops and metronome (P2)

### DONE

- [x] T090 [US3] [P] `GET/POST/DELETE /api/loops` — `server.py:1561,1570,1592`
- [x] T091 [US3] A-B looping in highway with named saved loops (persisted by `(filename, name)`)
- [x] T092 [US3] 4-count tempo-matched click before each loop repetition
- [x] T093 [US3] Smooth rewind effect on loop end
- [x] T094 [US3] [P] Metronome plugin — `plugins/metronome/` (audible click + visual beat flash beyond the built-in click-in)
- [x] T095 [US3] Step Mode plugin — `plugins/stepmode/` (Rocksmith-1-style step-through, integrates with Note Detection)

### OPEN

- [ ] T100 [US3] [OPEN] Loop sharing/export — currently scoped to one user's `meta.db`; no documented bundle or QR-code path

---

## Phase 4: User Story 4 — Import + scan new CDLC (P2)

### DONE

- [x] T110 [US4] Periodic background rescan timer — `_periodic_rescan` `server.py:1331`
- [x] T111 [US4] [P] User-triggered rescan endpoint — `POST /api/rescan` `server.py:1411`
- [x] T112 [US4] [P] Full rescan endpoint (rebuild from scratch) — `POST /api/rescan/full` `server.py:1421`
- [x] T113 [US4] SSE progress stream for scans — `/api/startup-status/stream` `server.py:1366`
- [x] T114 [US4] Per-file warning on corrupt PSARC; batch continues
- [x] T115 [US4] [P] Plugin: Sloppak Converter (PSARC → sloppak with Demucs stem split) — `plugins/sloppak_converter/`, `lib/sloppak_convert.py`
- [x] T116 [US4] [P] Plugin: RS1 Extractor — `plugins/` (extracts RS1 compatibility songs into individual CDLCs)
- [x] T117 [US4] [P] Plugin: Base Game Extractor — extracts on-disc songs from songs.psarc
- [x] T118 [US4] [P] Plugin: Tab Import — drag-and-drop GP files into CDLCs (uses `lib/gp2rs.py` + `lib/gp2midi.py`)
- [x] T119 [US4] [P] Plugin: Create from Tab — Ultimate Guitar search → GP → CDLC

### OPEN

- [ ] T130 [US4] [OPEN] Demucs stem split is currently opt-in via the Sloppak Converter plugin; consider whether a "split on import" option in core (still opt-in) would simplify workflows
- [ ] T131 [US4] [OPEN] Scan progress shows file count but not bytes — large libraries with mostly-tiny files can finish before the user perceives motion; consider per-second metric

---

## Phase 5: User Story 5 — Extend Slopsmith via plugins (P2)

### DONE

- [x] T140 [US5] Plugin discovery + manifest parsing — `plugins/__init__.py`
- [x] T141 [US5] [P] `requirements.txt` install on first load
- [x] T142 [US5] [P] `routes.py setup(app, context)` contract
- [x] T143 [US5] [P] `context["load_sibling"]` namespaced sibling import (slopsmith#33) — bijective id encoding (`_5f_`, `_2e_`)
- [x] T144 [US5] [P] `context["log"]` namespaced logger per plugin
- [x] T145 [US5] Frontend: `screen.html` injection + `screen.js` global-scope load
- [x] T146 [US5] Frontend: `settings.html` injection into Settings page (collapsible per-plugin panel)
- [x] T147 [US5] [P] `window.slopsmith` event emitter for cross-plugin comms
- [x] T148 [US5] [P] `window.slopsmith.audio.registerFader` for plugin-owned audio (slopsmith#87)
- [x] T149 [US5] [P] `window.registerShortcut` + `window.createShortcutPanel` (panel-scoped shortcuts for splitscreen)
- [x] T150 [US5] [P] `setRenderer` viz contract (covered in US2 too) — fully exposed via `window.slopsmithViz_<id>`
- [x] T151 [US5] [P] Update Manager plugin — installs/updates/uninstalls other plugins + slopsmith core
- [x] T152 [US5] Startup warning when two plugins ship same-named top-level modules (`.py` files OR package directories) — graceful fallback during transition

### OPEN

- [ ] T160 [US5] [OPEN] Per-plugin venv to eliminate cross-plugin dependency conflicts — see clarify.md "How are plugin requirements resolved" (deferred for deployment simplicity; revisit if conflicts become common)
- [ ] T161 [US5] [OPEN] Plugin manifest schema validation (today rejection rules are scattered: relpath checks, `..` rejection, etc.) — consolidate into a single JSON Schema document under `docs/`
- [ ] T162 [US5] [OPEN] Document the Auto-mode evaluation contract (`matchesArrangement` precedence, registration-order tiebreaker, `viz:reverted` behaviour) in `docs/` so plugin authors don't have to read `CLAUDE.md` to find it

---

## Phase 6: User Story 6 — Retune to E Standard (P3)

### DONE

- [x] T170 [US6] `/ws/retune` WebSocket — `server.py:2576`
- [x] T171 [US6] Retune pipeline (rubberband pitch shift + SNG/XML rewrite + new PSARC packaging) — `lib/retune.py`
- [x] T172 [US6] Sloppak retune rejection (`server.py:2598`)
- [x] T173 [US6] [P] Retune button on library card

### OPEN

- [ ] T180 [US6] [OPEN] Retune for sloppak (rewrite stems + arrangements without breaking note timing) — currently rejected; would need a separate pipeline that respects stem alignment
- [ ] T181 [US6] [OPEN] Bulk retune (queue + progress) — today is one-at-a-time

---

## Phase 7: User Story 7 — Export and re-import settings (P3)

### DONE

- [x] T190 [US7] `GET /api/settings` / `POST /api/settings` — `server.py:1633,1639`
- [x] T191 [US7] [P] `GET /api/settings/export` — `server.py:2087` (slopsmith#113)
- [x] T192 [US7] [P] `POST /api/settings/import` — `server.py:2125` (two-phase: validate-all → atomic write-all)
- [x] T193 [US7] [P] `settings.server_files` allowlist in plugin manifests (relpath, no `..`, no abs paths, no backslashes)
- [x] T194 [US7] [P] Path-traversal / schema-mismatch / decode-failure hard refusal
- [x] T195 [US7] [P] Plugin-state mismatch handling (warning + skip, not abort)
- [x] T196 [US7] [P] Atomic temp+rename file writes — `_atomic_write_file` `server.py:2042`

### OPEN

- [ ] T200 [US7] [OPEN] User-visible warning surfacing — today warnings are returned in the response JSON; the UI should aggregate and display them rather than silently logging
- [ ] T201 [US7] [OPEN] Bundle versioning / cross-version forward-compat tests (export from 0.2.7 → import on 0.2.4 etc.)

---

## Phase 8: User Story 8 — Export diagnostics (P3)

### DONE

- [x] T210 [US8] [P] `POST /api/diagnostics/export` — `server.py:2475`
- [x] T211 [US8] [P] `GET /api/diagnostics/preview` — `server.py:2528`
- [x] T212 [US8] [P] `GET /api/diagnostics/hardware` — exposed via the same module
- [x] T213 [US8] [P] Bundle composer + manifest schema dispatch — `lib/diagnostics_bundle.py`
- [x] T214 [US8] [P] PII redaction (DLC paths, song filenames, IPv4/IPv6, bearer tokens, query-string `key=`/`token=`) — `lib/diagnostics_redact.py`
- [x] T215 [US8] [P] Hardware probe (CPU/RAM/GPU + container/Electron/bare detection) — `lib/diagnostics_hardware.py`
- [x] T216 [US8] [P] Plugin inventory with git SHA + remote URL via `.git/HEAD` (works without `git` binary)
- [x] T217 [US8] [P] Browser console transcript via `static/diagnostics.js` (500-entry ring buffer, all levels + window.onerror + unhandledrejection)
- [x] T218 [US8] [P] `window.slopsmith.diagnostics.contribute(plugin_id, payload)` API
- [x] T219 [US8] Per-plugin `diagnostics.server_files` + `diagnostics.callable` opt-in
- [x] T220 [US8] [P] `docs/diagnostics-bundle-spec.md` reference

### OPEN

- [ ] T230 [US8] [OPEN] Browser hardware (WebGL/WebGPU adapter info) is collected but not yet schema-versioned the same way — make sure every JSON file in the bundle carries a versioned `schema` field
- [ ] T231 [US8] [OPEN] CLI helper to crack open and pretty-print a bundle for maintainers — today opening it is `unzip` + `jq`

---

## Phase 9: Polish, Cross-Cutting Concerns

- [x] T240 [DONE] [P] Playwright browser tests for keyboard shortcuts + UI — `tests/browser/`
- [x] T241 [DONE] [P] pytest coverage for `lib/song.py` and `lib/tunings.py`
- [ ] T242 [OPEN] [P] pytest coverage for pure helpers in `lib/sloppak_convert.py` (README explicitly calls this out as a natural follow-up target)
- [ ] T243 [OPEN] [P] pytest coverage for tempo/tick math in `lib/gp2rs.py` (README explicitly calls this out)
- [ ] T244 [OPEN] [P] Document the published browser support matrix (closes T080)
- [x] T245 [DONE] [P] Audit of `print()` calls across `server.py` and `lib/` complete — zero residual calls (commit `3b08488` / PR #159); CI grep guard added in `.github/workflows/tests.yml`
- [ ] T246 [OPEN] [P] CONTRIBUTING.md (the spec-kit `constitution.md` exists but a contributor-focused, plugin-author-friendly entry point would shorten the on-ramp)

---

## Dependencies & Execution Order

### Phase dependencies (retrospective)

- Phase 0 (Foundation) was built first, then phases 1–9 layered on top in
  roughly priority order. The retrospective view: Phase 0 → US1 → US2 → US3 → US4 → US5 → US6 → US7 → US8 → Phase 9.
- Today, OPEN tasks across phases are largely independent and can be
  picked up in any order. The only hard prerequisite is that
  T008/T245 (`print` migration) should land before any new `print` calls
  start a fresh debt cycle.

### User-story dependencies

- US1 + US2 are P1 and form the MVP. They are independent: US1 is "library
  works without ever opening a song"; US2 is "any song works given a
  filename, regardless of how it was browsed to."
- US3 (loops) extends US2's player. Cannot be tested without US2.
- US4 (rescan/import) extends US1 and is independent of US2.
- US5 (plugins) is foundational once shipped — every other story past
  US2 has plugin escape hatches, but core US1+US2 work without any
  plugins installed.
- US6 (retune) writes to the user's DLC folder; depends on US1's library
  read path but is otherwise independent.
- US7 + US8 are tooling on top of everything else; both depend on Phase
  0 (config_dir, logging) but not on the other user stories.

### Parallel opportunities

- Every `[P]`-marked OPEN task touches a different file and can be
  picked up in parallel.
- Plugin authors work fully in parallel with core — the `setup(app,
  context)` contract is the seam.

---

## Notes

- This task list is **descriptive**, not prescriptive. The DONE tasks
  document what shipped; the OPEN tasks are gaps the analyzer
  identified. Future contributors should treat the OPEN list as a
  prioritized backlog, but ordering is suggestive only.
- Whenever a user-visible behaviour changes, update both `CLAUDE.md`
  (for AI agents and humans alike) and the relevant `[Unreleased]`
  section of `CHANGELOG.md`.
- The constitution at `.specify/memory/constitution.md` is the
  veto for any task that would reshape platform invariants — see
  `analyze.md` for the consistency check.
