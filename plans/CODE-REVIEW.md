# aiofhir code review — 2026-11-25

Snapshot: `/workspace/aiofhir-review/repo-snapshot/aiofhir`. Branch `cursor/phase-3-haste`, HEAD `5279f4b`, 7 commits, with uncommitted work on top. Doug's Mac and repo were not touched. Probe script: `/workspace/aiofhir-review/probes/probe.py`.

## 1. Implemented vs plan

| Phase | Plan says | Reality |
|---|---|---|
| 0 Repo | done | Done. src layout, MIT, justfile, CI matrix 3.11–3.13, import test (no livekit or fastapi). |
| 1 Core | done | Mostly done: discovery with profile fallback, S256 required, cache; `scopes_from_capabilities`; PKCE, 256-bit state, constant-time `verify_state`; code exchange with basic/post auth; `RefreshingToken` (lock plus double-check, rotation callback, `invalid_grant` → `AuthExpired`); `FhirClient` (retry 429/5xx with Retry-After, 401 → refresh once, OperationOutcome → warnings, same-origin `next`, `max_pages`, semaphore, `iter_search`); `PatientRecord` (allowlist, forced patient, appointment `ge`/`lt`/`_sort`, booked/pending filter); frozen `Summary.to_text(max_chars)` with citations and «data» marking; pure `format_summary`. **But see B1–B3: refresh and summaries have real bugs.** `tests/fake_fhir.py` and the per-vendor `fixtures/{epic,cerner}` from the layout don't exist; the configurations are written inline in the tests. |
| 2 Profiles | done | Done as specified: generic, haste (tenant/project discovery fallback), epic (drops `_sort`), cerner stub (adds `lt9999…`). `HasteProfile` has no `adapt_search`, which is fine until `_sort` is verified. `profiles/base.py:45 discovery_fallback()` is unused dead code. |
| 3 Live Haste | in progress, unchecked | Uncommitted: `seed.py` and `scripts/seed_haste.py` (client app, patient, 5 appointments, membership, Synthea-style practitioner and hospital bundles), `demo.py` and `examples/smoke_haste.py` (browser login, helpers, forced refresh, token-leak check), `log.py` redaction, `scrub.py`, `tests/live/test_haste.py`, and one fixture. **Gaps against the plan:** `build_chart()` (meds, allergy, condition, lab) is defined and unit-tested but **never called by `apply_seed`**, so the live record has no medications or labs. There is no past appointment and no AccessPolicyV2. The live test uses a raw token and a `PATIENT_ID` env var, not a token-derived patient, and it doesn't check paging termination. No recorded live fixtures yet. |
| 4–8 | open | Not started. `RemoteRecord`, the `[server]` extra and hooks are absent. `log.py` is an early slice of the Phase 7 "request hooks with PHI redaction" item. |

Plan drift: Phase 3 text says *local docker Haste (`my-health`/`lk-demo`)*, while Open questions and `examples/.env.example` use the *hosted* `api.haste.health/w/livekit/synthea-sandbox`. Pick one.

## 2. Test, lint and type results (box, uv 0.13.8)

- Working tree, Python 3.13.5: `uv sync --locked --extra dev` OK. `pytest --ignore=tests/live`: **80 passed in 0.84s**. Full `pytest`: 80 passed, 1 skipped (live). `ruff check .`: **All checks passed!** `ruff format --check .`: **43 files already formatted**. `mypy` (strict): **Success: no issues found in 41 source files**.
- Same suite on Python 3.11: 80 passed, 1 skipped.
- Committed HEAD only (clean worktree): 55 passed; ruff clean; mypy "no issues found in 29 source files".

Green checks don't catch the bugs below. The probes in `probes/probe.py` reproduce them.

## 3. Findings (prioritized)

### High
**B1. Refresh breaks with `launch/patient`, and patient context is lost on every refresh.** `smart/client.py:95-102`, `:135`, `tokens.py:104`. `exchange_refresh` re-runs `_token_set(body, scope, …)` with the original scope. The Haste and Epic scopes contain `launch/patient`, and refresh responses usually omit `patient`, so a refresh **raises `TokenError("Token response did not include a patient")`**. Probe P1 reproduces it. This hits every 401 and every pre-expiry refresh, plus `run_smoke`'s forced refresh. Without `launch/patient`, the refreshed `TokenSet` has `patient=None`, `encounter=None` and `fhir_user=None`, and `on_refresh` persists that, so a reloaded session has no patient. *Fix:* give `exchange_refresh` the previous `TokenSet` (or have `RefreshingToken` merge), keep `patient`/`encounter`/`need_patient_banner`/`id_token`/`fhir_user` when the response omits them, skip the launch/patient check on the refresh grant, and raise `AuthExpired` if a refresh returns a *different* patient. Add a test where the refresh body has no `patient`.

**B2. Summaries never contain dates, so "when is my next appointment" can't be answered.** `record.py:62` and `:75`, plus `summary.py:55`. `Item.at` is never set by any helper; only tests set it. `to_text()` doesn't print `at` even when it is set. Probe P3: `'upcoming_appointments\n- «Annual physical» (Appointment/a1)'`. *Fix:* set `at=_parse_instant(item["start"])` for appointments and `effective[DateTime|Period.start]` for labs (`authoredOn` for medications). Have `_line` render `at` as an ISO date or time, not local time; that keeps `display` the only tz-aware formatter. Labs also need `value[x]` and units, and medications should carry `dosageInstruction[0].text`, or the agent can only list names.

**B3. Missing `expires_in` locks the session out.** `smart/client.py:137-138`. A missing `expires_in` gives `seconds=0`, so `expires_at=now` and `RefreshingToken.get()` always refreshes. With no refresh token, **the first `get()` raises `AuthExpired`** (probe P2). A non-numeric string such as `"3600s"` raises an uncaught `ValueError`. *Fix:* default to a conservative value (300 s), or `None` meaning unknown, and refresh only on 401. Parse defensively.

### Medium (security and PHI)
**S1. The token leaks to another tenant on the same host.** `client.py:124` and `:251-253`. `_same_origin` compares only scheme and host. On multi-tenant Haste (`api.haste.health/w/{tenant}/{project}`), a `next` link to `/w/other/...` gets the bearer token (probe P5). *Fix:* require `nxt` to start with `self._base + "/"`, i.e. the same origin *and* path prefix.

**S2. Path and query injection in `read()` bypasses the allowlist.** `client.py:73` and `profiles/base.py:39`. `resource_id` goes straight into the URL, and httpx collapses dot segments: `read("Medication", "../Account/1")` → `/r4/Account/1`. `resolve_medication` takes `rsplit("/")[-1]` of a server-supplied reference, so `Medication/m1/_history/2` reads id `2` (probe P7), and `…/Medication/x?_format=xml` passes `x?_format=xml` as the id. *Fix:* validate ids against FHIR `[A-Za-z0-9\-.]{1,64}` and resource types against `^[A-Z][A-Za-z]+$`. Parse references properly: relative `Type/id[/_history/v]`, and refuse absolute URLs on a foreign base. This matters more once `RemoteRecord` gets LLM-supplied arguments.

**S3. `search_raw` forces `patient`, but other params can widen or escape it.** `record.py:40-45`. Callers can pass `_revinclude`, `_include`, `_has`, `subject=…` and chained or modifier keys such as `patient:missing`, `patient.identifier` and `subject:Patient` (probe P4 shows them passed through). `_revinclude=Provenance:target` and `_include=*` pull in non-allowlisted resource types. *Fix:* use a per-resource param allowlist, or at least reject keys that start with `_include`, `_revinclude`, `_has`, `_id`, `patient`, `subject` or `_contained`. Also filter returned resources to the requested type (plus `Medication` for MedicationRequest).

**S4. Allowlist entries that can't be patient-scoped.** `record.py:15` and `:18`. `search_raw("Patient")` sends `Patient?patient=p1` (probe P4), which isn't a valid Patient search param; servers ignore it or error, and an ignored param means *all patients* on a user-scoped token. `Medication` isn't in the Patient compartment, so `Medication?patient=` is the same problem. *Fix:* map `Patient` → `read("Patient", patient_id)` or `_id=`. Drop `Medication` from the `search_raw` allowlist and keep reads only through `resolve_medication`.

**S5. Redaction is narrower than the docs claim.** `log.py:15` and `client.py:152`. Only the exact keys `patient` and `subject` are hidden. `/Patient/{id}` in paths, `subject:Patient`, `patient.identifier`, `_id`, `identifier` (MRN) and `name`/`birthdate` show in DEBUG logs (probe P6). `seed.py` logs `User/{id}` and `Patient/{id}` at INFO (`:240`, `:273`, `:277`, `:284`). *Fix:* redact on base-key match (`key.split(':')[0].split('.')[0]`), and add `_id`, `identifier`, `name`, `family`, `given`, `birthdate`, `phone`, `email`, `address`. Replace path ids after the resource type with `[id]`. Use `%s` lazy logging instead of f-strings: ruff `G004`, add `"G"` to the ruff select.

**S6. Write-capable admin token persisted in plaintext.** `scripts/seed_haste.py:75` writes `HASTE_TOKEN` for `user/*.cruds` to `.env`. It is gitignored, but long-lived and write-capable. *Fix:* keep it in memory per run, or at least don't request `offline_access` for the seed login and `chmod 600` the file. `.env` is also loaded from `examples/.env`, so check that path is gitignored too; the root `.gitignore`'s `.env` pattern does match nested files.

### Medium (async and concurrency)
**C1. Concurrent 401s each trigger a refresh.** `tokens.py:88-91`. `refresh()` has no double-check: 5 concurrent `refresh()` calls → 5 token-endpoint calls (probe P8). With rotating refresh tokens this burns the rotation, and some servers revoke the token family on reuse. *Fix:* `refresh(stale: str | None)`. Under the lock, if `self._tokens.access_token != stale`, return the current token. `FhirClient._send` passes the token it used.

**C2. New `httpx.AsyncClient` per discovery and token call.** `smart/client.py:105` and `:119`. No pooling, no explicit timeout, and the token request has no timeout or retry policy. *Fix:* accept an injected `httpx.AsyncClient` (shared with `FhirClient`), or own one on `SmartClient` with an `aclose()`, and set `timeout=`.

**C3. Discovery cache.** `discovery.py:14`. The cache is process-global, keyed by base only (profile ignored), never expires, and concurrent first calls aren't deduped. A non-JSON 200 raises `ValueError`, not `DiscoveryError` (`:181`). *Fix:* use a TTL, key by `(base, profile.name)`, add a per-key `asyncio.Lock` or a cached task, and wrap `response.json()`.

**C4. `active_medications` does serial N+1 reads, and one failure fails the whole summary.** `record.py:71-75`. *Fix:* `asyncio.gather` the reads (the semaphore already caps them), cache by Medication id, catch `FhirError` per item and add a warning instead.

**C5.** `FhirClient.__aenter__` (`client.py:64`) overwrites an existing open client, which leaks the previous one. Guard against re-entry.

### Low and API design
- `RefreshingToken(smart: Any)` at `tokens.py:68`: define a `RefreshExchanger` Protocol.
- `TokenSet.from_dict`: a naive `expires_at` crashes the comparison in `_expiring`. Coerce to UTC.
- `_basic()` at `smart/client.py:179`: RFC 6749 §2.3.1 requires form-urlencoding the id and secret before base64.
- `scopes_from_capabilities` can return a scope with **no resource scopes** when `scopes_supported` lists none of `READ_RESOURCES` (`scopes.py:60`). Raise `DiscoveryError`.
- `_token_body` (`smart/client.py:184-193`) drops the OAuth `error` code. Include the code (not the description) in `TokenError` for debugging.
- `Summary.to_text`: if `max_chars` is smaller than kind plus the "(+N more)" suffix, the output exceeds the cap. Clamp it.
- `upcoming_appointments`: an appointment without `start` sorts last as `"9999"`, which is fine. Consider `participant.actor` checks as a second client-side layer, like the patient check in the plan.
- `recent_labs` sorts only by `effectiveDateTime`. Handle `effectivePeriod`, `effectiveInstant` and `issued`.
- `seed._ensure_information` polls the index 10 times with **no sleep** (`seed.py:203`). Add backoff. `_total()` returns 0 when the server omits `total` (`:423`), which makes appointment seeding non-idempotent. Use `_first()` or `_summary=count`.
- `demo._read_callback` ignores `error`/`error_description` on the redirect. Surface `access_denied` clearly.
- README says the seed client must allow `user/*.cruds`, but `examples/create-seed-client.json` grants `user/*.* openid profile …`. Check whether Haste treats `*.*` as covering `.cruds`, and make them match.

### Open scope question (Haste `patient/*.rs` vs `user/*.rs`)
The code handles both from discovery: `permission-patient` → `patient/*.rs launch/patient …`, and a user-only server → `user/*.rs`. Every Haste "fixture" in the tests is hand-written with `permission-patient` plus `patient/*.rs`; nothing recorded from the live server is committed yet. Recommendation: run `just discover` against the hosted base, commit the scrubbed `.well-known` JSON as `tests/fixtures/haste/smart-configuration.json`, and add a test that derives the scope from it. Keep `patient/*.rs` if that capability is advertised. If Haste ends up user-scoped, document that the AccessPolicyV2 (still missing from the seed) is the real control, and note that `launch/patient` is then requested only if it's listed. **B1 is a blocker for the patient-scope path regardless.**

### Seed and demo placement
`src/aiofhir/seed.py` (Haste admin writes, Membership, ClientApplication) and `src/aiofhir/demo.py` (webbrowser, a local HTTP server, `.env` writes, `SEED_LOGIN_SCOPE="user/*.cruds"`) ship in the wheel. That conflicts with "read-only" and the plan's layout, which puts seed and smoke under `scripts/` and `examples/`. *Move* them to `scripts/_seed_lib.py` and `examples/_demo.py`, or to a `tests/support` package, and keep them out of the published package. Also exclude them in hatch: `[tool.hatch.build.targets.wheel] packages=["src/aiofhir"]` plus an exclude. `log.py` (redaction) and `scrub.py` belong in the library. `configure_cli_logging` is borderline but fine.

## 4. What to commit, and how to split it
1. **Commit A, logging and redaction** (good to go after S5): `log.py`, the logger lines in client, record, profiles and smart, `tests/test_log.py`, the `examples/discover.py` change, `scrub.py` and `tests/test_scrub.py`.
2. **Commit B, bug fixes** (new, before Phase 3): B1, B2, B3, C1, S1, S2, S3 and S4, each with a test.
3. **Commit C, Phase 3 seed and smoke** after moving seed and demo out of `src/`: wire `build_chart` into `apply_seed`, add a past appointment and the AccessPolicyV2, then `scripts/seed_haste.py`, `examples/smoke_haste.py`, the justfile targets, the pyproject mypy `scripts` entry, `tests/test_seed*.py`, `tests/test_smoke.py`, `synthea-data/`, `README.md` and `examples/create-seed-client.json`.
4. **Commit D, live tests**: `tests/live/test_haste.py`, once it gets its patient from a token, checks every resource's patient for all helpers, and asserts paging termination.
5. The `plans/PLAN-merged.md` status edit can ride with C.
6. **Do not commit** `examples/delete-livekit-demo-seed.json`. It is a batch of *conditional DELETEs* on `identifier=https://livekit.io/demo/seed|` across ~15 types, a system unrelated to aiofhir's `https://aiofhir.dev/seed`. It is easy to fire at the wrong project. Keep it out of the repo, or put it behind a confirm script.

## 5. Secrets and PHI scan
- No `.env` in the snapshot. No JWTs, private keys or real secrets in the tree or the git history (`git log --all -p` scanned). Hits: test placeholders (`my-secret`, `access-secret`, `old-token`) and public sandbox defaults in the plans (Haste docker `myuser@health.org`/`testing_password`, Epic `epicepic1`, public MyChart test users). That's acceptable, but consider pointing to docs instead of inlining passwords.
- `synthea-data/` is **not real Synthea output**. It holds 2 hand-made batch bundles (Organization ×1, Location ×2, Practitioner ×2, PractitionerRole ×2) named "aiofhir Seed …" / "Dr Seed". No patients, no PHI.
- `tests/fixtures/haste/patient.json` is synthetic ("Pat Example"). `examples/*.json` contain no PHI.
- The real PHI risk is runtime: S5 (logs) and the smoke script printing `patient: {id}` and record text to stdout, which is fine for a sandbox.

## Addendum: fixes (2026-11-25, 3:10 PM EST)
Patches are in `/workspace/aiofhir-review/patches/` (0001 bugs B1–B3 and C1; 0002 security S1–S6 plus logging; 0003 seed and smoke). `seed-smoke-fix.patch` is the same as 0003. They apply in order with `git apply` onto Doug's uncommitted working tree as of 2:50 PM EST. Results: 139 passed on Python 3.11.18 and 3.13.5; ruff and format clean; mypy strict clean (43 files).
