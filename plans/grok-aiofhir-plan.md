# aiofhir: build plan (library only)

## 1. Goal and scope
`aiofhir` is a thin **async FHIR R4 and SMART on FHIR client** for Python. It is built on **httpx** and has **no LiveKit dependency**. It's meant to be reused across demos and services.
- **Why httpx async:** LiveKit agents, both 1.x and 2.0, run on asyncio. A blocking HTTP client would stall the event loop during FHIR calls and token refreshes. httpx gives one async client with connection pooling, timeouts, and a mockable transport (respx), so tests need no network.
- **In scope:**
  - **SMART login (`aiofhir.smart`):** discovery, PKCE, state, code exchange, tokens and refresh.
  - **FHIR access:** a FHIR client with paging and retries.
  - **Patient-scoped read helpers.**
  - **Formatting:** compact summaries and text output.
  - **Vendor profiles:** for Haste Health, Epic, Oracle Health (Cerner) and a generic server.
  - **An optional backend-for-frontend server** that holds tokens.
  - **Tests:** unit tests and a per-vendor conformance suite.
- **Non-goals:**
  - The Agents 2.0 demo.
  - LiveKit tools or adapters, which live in each demo.
  - Any UI.
  - Writes, booking, and the EHR launch flow (later).
  - Bulk data.

## 2. Design principles
- **Patient scoping.** The patient id comes only from the token response (`TokenSet.patient`). `PatientRecord` always adds `patient=` itself and never takes it as an argument. Raw search goes through a resource allowlist and always gets the forced patient parameter.
- **Tokens stay server-side by default.**
  - A backend-for-frontend (BFF) holds a `RefreshingToken` for each `session_id`.
  - Callers such as agents get a `RemoteRecord(session_id)`, which has the same methods as `PatientRecord` and never sees a token.
  - Running a `PatientRecord` in-process is documented as for local demos and scripts only.
- **Server-agnostic first.** Behavior comes from `.well-known/smart-configuration`. The differences between servers are kept in small `VendorProfile`s.
- **Explicit security contracts.**
  - The caller stores `state` and `code_verifier` on the server, uses them once, and checks state in constant time with `verify_state`. This is documented in the README.
  - `fhir_user` is read from the id_token **without verification**, and the docs say so.
  - Tokens never appear in `repr()` or in logs.
- **Pure display.** `Summary` is frozen. Timezone and display formatting live in `aiofhir.display` and return strings. They never change the summary.
- **Fail clearly.** `AuthExpired` carries a fixed message for the user to reconnect. FHIR errors keep their OperationOutcome.

## 3. Package layout
```
aiofhir/                          # repo: github.com/dmorand17/aiofhir or LiveKit-Field org (TBD)
  pyproject.toml                  # deps: httpx. extras [server]: fastapi, cryptography.
                                  # dev: pytest, pytest-asyncio, respx, freezegun, ruff, mypy
  src/aiofhir/
    __init__.py                   # FhirClient, PatientRecord, RemoteRecord, Summary, errors
    errors.py                     # AiofhirError, DiscoveryError, StateMismatch, TokenError, AuthExpired, FhirError
    smart/
      __init__.py                 # SmartClient, AuthorizationRequest, TokenSet, RefreshingToken, verify_state, scopes
      discovery.py                # SmartConfiguration, discover(base, profile)
      pkce.py                     # make_verifier(), challenge_s256()
      scopes.py                   # patient_read_scopes(resources, style), detect_style(cfg)
      client.py                   # SmartClient, AuthorizationRequest, verify_state
      tokens.py                   # TokenSet, RefreshingToken
    vendors/
      __init__.py                 # VendorProfile, detect_profile, HASTE, EPIC, CERNER, GENERIC
      base.py  haste.py  epic.py  cerner.py  generic.py
    client.py                     # FhirClient
    record.py                     # PatientRecord, ALLOWED_RESOURCES
    summary.py                    # Summary, Item, Citation, to_text()
    display.py                    # local_time(), format_summary()
    remote.py                     # RemoteRecord
    server/app.py                 # optional BFF (FastAPI), extra [server]
  tests/
    fixtures/{haste,epic,cerner}/ # recorded, de-identified smart-config + Bundles
    fake_fhir.py                  # respx router: smart-config, /token, paged search, 401/429/5xx scripting
    test_m*.py                    # one file per milestone
    conformance/                  # live, opt-in: pytest -m conformance --vendor haste|epic|cerner
  scripts/seed_haste.py
  examples/smoke_haste.py
  README.md
```

## 4. Public API sketch
```python
from aiofhir import FhirClient, PatientRecord, AuthExpired
from aiofhir.smart import SmartClient, RefreshingToken, TokenSet, verify_state
from aiofhir.display import format_summary

smart = SmartClient(fhir_base_url, client_id, redirect_uri, scope, client_secret=None, profile=None)
req = await smart.authorization_request()        # AuthorizationRequest(url, state, code_verifier)
# caller saves req.state + req.code_verifier server-side (single-use), redirects to req.url; on callback:
verify_state(expected=saved.state, received=params["state"])   # hmac.compare_digest; raises StateMismatch
tokens = await smart.exchange_code(code, saved.code_verifier)  # TokenSet
tokens.patient          # FHIR Patient id from the token response
tokens.fhir_user        # UNVERIFIED (id_token not validated)
TokenSet.from_dict(tokens.to_dict())

rt = RefreshingToken(smart, tokens, on_refresh=save_tokens)    # asyncio.Lock; persists rotated refresh token
async with FhirClient(fhir_base_url, provider=rt, profile=smart.profile) as fhir:
    rec = PatientRecord(fhir, patient_id=tokens.patient)
    s = await rec.upcoming_appointments(days=90)
    # also: active_medications(), allergies(), conditions(), recent_labs(days=90)
    print(format_summary(s, tz="America/New_York"))  # display only; s is unchanged
    llm_text = s.to_text(max_chars=4000)             # size-capped, keeps citations

# Server-side default: callers use the same methods without tokens
from aiofhir import RemoteRecord
rec = RemoteRecord(bff_url, service_token, session_id=sid)
try:
    s = await rec.active_medications()
except AuthExpired as e:
    msg = str(e)   # "Your health record connection expired. Please reconnect to continue."
```
- **FhirClient:**
  - `read(type, id)` and `search(type, params, max_pages=10)`.
  - Retries: two retries on 429/502/503/504, with exponential backoff and jitter, honoring `Retry-After`.
  - Auth: on a 401 it refreshes once and retries once, then raises `AuthExpired`.
  - It drops OperationOutcome entries from bundles and keeps them as `warnings`.
  - It follows only same-origin `next` links.

## 5. VendorProfile
```python
class VendorProfile(Protocol):
    name: str                                  # "haste" | "epic" | "cerner" | "generic"
    scope_style: Literal["v1", "v2", "auto"]   # .read vs .rs
    send_aud: bool
    requires_confidential_for_refresh: bool
    supports_sort: bool
    max_count: int | None
    def matches(self, base: str, cfg: SmartConfiguration) -> bool: ...
    def discovery_paths(self, base: str) -> list[str]: ...
    def adapt_search(self, resource: str, params: dict[str, list[str]]) -> dict[str, list[str]]: ...
    async def resolve_medication(self, fhir: "FhirClient", mr: dict) -> dict: ...
```
| | Haste Health | Epic | Oracle Health (Cerner) | generic |
|---|---|---|---|---|
| Discovery | `{base}/.well-known/smart-configuration`, then fallback `/w/{t}/{p}/api/v1/.well-known/smart-configuration` | standard | standard (per-tenant base) | standard |
| Scope style | v2 `.rs`; `patient/` read-only | per app registration (v1 or v2) | v1 or v2 | from `capabilities` |
| Refresh | `offline_access`; refresh token may rotate | **confidential client required** | `offline_access` / `online_access` | from config |
| `aud` | send | send (orgs may require it) | send | send |
| Patient context | User linked to Patient via `Membership`; no picker | MyChart login | patient-persona authorize URL | token `patient` |
| Appointment | `ge` + `lt` + `_sort=date` (verify) | `patient` required; no `_sort`, sort on client | **`date=ge…&date=lt…` both required**; sorted ascending | as given |
| Medications | inline concept (Synthea) | `medicationReference` → read `Medication` | mostly inline (verify) | resolve if a reference |
| Observation | standard | sort on client | `_count` ≤ 200; `code` ignored with `category` | standard |
| Endpoints | config (tenant/project URL) | Brands Bundle | per-tenant service root | config |

Sandboxes:
- **Haste:** local docker-compose.
- **Epic:** `fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4`. MyChart users `fhircamila`, `fhirderrick` and `fhirdesiree`, password `epicepic1`. No appointment data is listed for them.
- **Cerner:** patient endpoint `fhir-myrecord.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d`.

## 6. Milestones
| # | Milestone | Acceptance criteria | Tests |
|---|---|---|---|
| M0 | Scaffold | `src/` layout, uv/hatch, ruff + mypy strict + pytest-asyncio CI; `import aiofhir` pulls in only httpx and stdlib | CI green; a test asserts `livekit` and `fastapi` aren't in `sys.modules` after `import aiofhir` |
| M1 | Errors + VendorProfile skeleton | error hierarchy; the profile protocol; `generic`, `haste`, `epic` and `cerner` defined; `detect_profile(base, cfg)` | `adapt_search` tests: Cerner adds an `lt` bound to Appointment, Epic drops `_sort`, generic passes params through |
| M2 | SMART discovery (`aiofhir.smart.discovery`) | tries `{base}/.well-known/smart-configuration`, then the profile's fallbacks (Haste); parses endpoints, capabilities, PKCE methods; requires `S256`; cached per base | fixtures: Haste primary 404 → fallback 200; Epic and Cerner configs; all paths 404 → `DiscoveryError`; no S256 → error |
| M3 | Scope helpers | `patient_read_scopes(resources, style)` → `.read` (v1) or `.rs` (v2), plus `openid fhirUser launch/patient offline_access`; `detect_style(cfg)`; profile override | table tests for v1 and v2, de-duplication, override |
| M4 | PKCE auth request + state | `authorization_request()` → `url, state, code_verifier`; 256-bit state; verifier 43–128 chars; S256 challenge; `aud` when the profile sends it; `verify_state()` is constant-time; the single-use contract is documented | URL query parsing; RFC 7636 test vector; mismatched or empty state → `StateMismatch`; a reference store shows a reused state is rejected |
| M5 | Code exchange + TokenSet | `exchange_code(code, verifier)` uses basic or post client auth per the config; `TokenSet(access_token, refresh_token, expires_at, scope, patient, id_token, fhir_user)`; `to_dict`/`from_dict`; `fhir_user` unverified and documented | fake `/token` responses for each vendor; missing `patient` → `TokenError` with the Haste Membership hint; round-trip; the token isn't in `repr()` |
| M6 | RefreshingToken | refreshes 60 s before expiry; `asyncio.Lock` with a double-check, so concurrent callers make one refresh; rotated refresh token stored and `on_refresh` awaited; `invalid_grant` or no refresh token → `AuthExpired`; warns on Epic without a secret | 20 concurrent `get()` → exactly 1 `/token` call; rotation persisted; freezegun covers expiry; `invalid_grant` → `AuthExpired` |
| M7 | FhirClient | `read`, `search(max_pages=10)`, retries, backoff, `Retry-After`, refresh on 401, OperationOutcome filtering, same-origin paging, `adapt_search` applied | the fake server scripts 3 pages (cap at 10), 429→200, 503×3 → `FhirError`, 401→refresh→200, 401→401 → `AuthExpired`, a mixed bundle, a cross-origin `next` that's refused |
| M8 | PatientRecord helpers | patient forced; `ALLOWED_RESOURCES` on `search_raw()`; `upcoming_appointments(days=90)` uses `date=ge{now}&date=lt{now+days}&_sort=date` with a client-side booked/pending filter and sort; `active_medications()` uses `status=active` and resolves references; `allergies()` active; `conditions()` problem-list and active; `recent_labs(days=90)` uses `category=laboratory&date=ge…`, newest first | recorded fixtures for each vendor; passing a `patient` argument raises `TypeError`; `search_raw("Account")` is rejected; cancelled and proposed appointments are dropped; reference resolution; an empty bundle gives an empty Summary |
| M9 | Summary, to_text, display | frozen `Summary(kind, items, citations, truncated, warnings)`; `to_text(max_chars=4000)` cuts on item boundaries with "(+N more)" and keeps `ResourceType/id` citations; free text wrapped as data `«…»`; pure `local_time()` and `format_summary()` | snapshots; size-cap test; Summary unchanged after formatting; timezone test (UTC → "Tue Dec 8, 2026, 2:30 PM EST") |
| M10 | RemoteRecord + optional BFF (`aiofhir.server`) | `/auth/start`, `/auth/callback` (single-use state), `/v1/record/{method}` with a service bearer and a method allowlist; Fernet-encrypted store (memory store in dev); `RemoteRecord` mirrors `PatientRecord` and maps 401 → `AuthExpired` | httpx `ASGITransport` end to end with fake FHIR; unknown session → `AuthExpired`; unknown method → 404; caplog shows no token in logs or responses |
| M11 | Haste seed + smoke test | `scripts/seed_haste.py` and `examples/smoke_haste.py` pass against local Haste (§7) | manual checklist plus `pytest -m conformance --vendor haste` |
| M12 | Docs + recorded fixtures | README: quickstart, the "tokens server-side" section, the state-handling warning, the `fhir_user` caveat, the vendor table; fixture recorder with a scrubber; Epic and Cerner conformance cases (live, opt-in) | README examples run in CI; fixtures pass the scrubber check |

## 7. Haste Health local smoke test + seed script
1. **Start Haste.**
   ```
   curl -O https://raw.githubusercontent.com/HasteHealth/HasteHealth/refs/heads/main/docker-compose.yml
   docker-compose up
   ```
   The API runs on :3000 and the admin app on :3001. Sign in to tenant `my-health` as `myuser@health.org` / `testing_password` and create the project `lk-demo`.
2. **Seed the data.** `python scripts/seed_haste.py --base http://localhost:3000/w/my-health/lk-demo/api/v1/fhir --synthea ~/synthea/fhir`. The script is idempotent and wraps the `haste-health` CLI:
   1. `api batch` the hospitalInformation and practitionerInformation files.
   2. Poll `search-type Practitioner _count=0` until the count matches.
   3. `api transaction` each patient bundle, then pick 2–3 personas: one with many medications, one with labs and one with allergies.
   4. Synthea has no future visits, so add `Appointment`s for each persona: booked at +3, +14 and +45 days, plus one `pending` and one `cancelled`.
   5. Create a `User` and a `Membership` linking it to `Patient/{id}`, plus a read-only AccessPolicyV2.
   6. Create the `ClientApplication` with `grantType: ["authorization_code", "refresh_token"]`, `redirectUri: ["http://localhost:8765/callback"]`, `scope: "openid fhirUser launch/patient offline_access patient/*.rs"`, and a secret. It prints the client id and the secret.
3. **Run the smoke test.** `python examples/smoke_haste.py --base … --client-id … --secret …`. It:
   1. opens the browser and catches the callback on :8765;
   2. checks the state and exchanges the code;
   3. prints the patient id;
   4. prints `format_summary()` for each helper;
   5. forces one refresh and checks the rotated refresh token was saved;
   6. checks the `to_text()` size cap.
4. **Pass criteria:**
   - Discovery works, through the fallback if needed.
   - Appointments show the 3 booked and the 1 pending, sorted by date, without the cancelled one.
   - Every item has a citation.
   - The refresh works.
   - No token appears in any output.

## 8. Effort (library only)
| Work | Effort |
|---|---|
| M0–M3 scaffold, errors, profiles, discovery, scopes | 1 d |
| M4–M6 auth request/state, exchange, TokenSet, RefreshingToken | 1.5 d |
| M7 FhirClient | 1 d |
| M8–M9 PatientRecord, Summary/to_text/display | 1.5 d |
| M10 RemoteRecord + BFF server | 1 d |
| M11 seed script + Haste smoke test | 1 d |
| M12 docs + recorded fixtures | 0.5 d |
| **Subtotal: working against Haste** | **~7.5 d** |
| Epic and Cerner profile completion + sandbox conformance runs | 1.5 d |
| **Total library** | **~9 d** |

## 9. Open questions
- **Haste base URL:** hosted (BAA, rate limits) or self-hosted? Which tenant and project?
- **Haste permissions:** can I create Users, Memberships and the ClientApplication? I'd need an admin or CLI credential for seeding.
- **Haste support:** does it handle `_sort`, `date` with two bounds, and `status` on Appointment? M11 will settle this.
- **Repo:** `github.com/dmorand17/aiofhir` or the LiveKit-Field org?
- **License:** Apache-2.0 suggested. Publish to PyPI as `aiofhir`? The name was free when checked on Nov 25, 2026.
- **BFF defaults:** a service-token scheme (a static bearer for demos, or a signed JWT), encryption key management, and the session TTL.
- **Cerner:** sandbox patient credentials and code.cerner.com registration. Are medications inline?
- **Epic:** each organization has to activate the app. The sandbox has no appointment data. Refresh needs a confidential client.

Sources:
- **Haste Health:**
  - SMART on FHIR: https://haste.health/docs/auth/authentication/smart_on_fhir
  - Client registration: https://haste.health/docs/auth/authentication/client_registration
  - Refresh tokens: https://haste.health/docs/auth/authentication/grant_types/refresh_token
  - Membership: https://haste.health/docs/auth/authorization/membership
  - Load sample data: https://haste.health/docs/getting_started/load_sample_data
  - Quick start: https://haste.health/docs/getting_started/quick_start
  - Code: https://github.com/HasteHealth/HasteHealth (PR #833)
- **Epic:** https://fhir.epic.com/Documentation?docId=oauth2 and https://fhir.epic.com/Documentation?docId=testpatients
- **Cerner:** https://fhir-myrecord.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/.well-known/smart-configuration and https://docs.oracle.com/en/industries/health/millennium-platform-apis/mfrap/
- **Standards:** SMART App Launch v2 (https://hl7.org/fhir/smart-app-launch/), RFC 7636, FHIR R4 (https://hl7.org/FHIR/R4/)

## 10. Next: integrations
The Agents 2.0 chat demo, and any LiveKit tools built on `RemoteRecord`, get their own follow-on plan once aiofhir reaches M11. See the Phase 2 section of `plan.md`.
