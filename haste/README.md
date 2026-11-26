# Haste Health tooling

Scripts that seed a Haste Health project and sign in to it for the Phase 3 smoke
test. Nothing here ships in the `aiofhir` package. Run them from the repo root
with `just seed` and `just smoke`, or `uv run python -m haste.seed` and
`uv run python -m haste.smoke`.

| Path | Purpose |
|---|---|
| `seed.py`, `seeding.py` | Seed entry point and the idempotent seed resources |
| `smoke.py`, `login.py` | Browser sign-in, every record helper, one forced refresh |
| `check_policy.py`, `policy_check.py` | Live check of the patient access policy and the admin seed client |
| `policy_model.py` | Offline model of Haste's rule engine, used by `tests/haste/test_policy.py` |
| `bundles/` | Hand-posted transactions: the admin seed client, and a seed cleanup |
| `synthea-data/` | Practitioner and hospital bundles the seed loads (`SYNTHEA_DIR`) |
| `.env.example` | Variables the scripts read. Copy it to the repo-root `.env`, the only `.env` they load; the seed writes new values there too. A leftover `haste/.env` is ignored, with a warning. |

Unit tests are in `tests/haste/`. The opt-in live check is `tests/live/test_haste.py`.

## Seeding

`haste/seed.py` (`just seed`) needs an access token. When `HASTE_TOKEN` is unset, the script signs in and writes that token to `.env`.

Prerequisite: a `ClientApplication` already exists on the project. The seed script creates the `aiofhir-smoke` client, so it cannot sign in as that client to create itself. `bundles/create-seed-client.json` is a transaction that creates that client as `aiofhir-seed`. Post the bundle to the FHIR base.

That existing client must:

- include redirect URI `http://localhost:8765/callback`
- allow the seed login scope `user/*.cruds openid fhirUser`, or the scope you set in `SCOPE`

Set `BASE` to the FHIR base and `CLIENT_ID` to that client. Set `CLIENT_SECRET` when the client is confidential. `.env` is gitignored and written with mode 600. The admin token stays in memory unless `SAVE_HASTE_TOKEN=1`.

### The admin seed client is locked after the first run

A public client allowed `user/*.cruds` lets **any** project member sign in and
get a user-level token, including the patient user. So at the end of every run
the seed makes `aiofhir-seed` confidential: when the client has no secret, it
writes a new one to `.env` as `CLIENT_SECRET` (mode 600, before changing the
client) and sets it on the client, and it drops the `refresh_token` grant.
Haste refuses a token exchange or refresh whose secret does not match, so
nobody without `.env` can use the client. Later `just seed` runs sign in with
that secret. If `CLIENT_ID` names some other client, the seed leaves it alone
and says so.

To remove the client entirely, run `just seed-cleanup` (it deletes
`ClientApplication/aiofhir-seed`), then delete `CLIENT_SECRET` from `.env`. To
seed again later, post `bundles/create-seed-client.json` once more; the next
`just seed` locks it again.

### The patient user

Haste keeps `User` resources in the tenant's `system` project
(`/w/{tenant}/system/api/v1/fhir/User`), not in the project's FHIR base. The
`Membership` that links a User to a `Patient` lives in the project. A User an
admin creates for someone else is an **invitation**: the person must accept the
email before they can sign in.

Set `SEED_USER_EMAIL` to an address you can read (for example a `+patient`
alias). The seed then:

1. finds that User in the system project, or invites it (`role: member`);
2. creates or relinks its `Membership` to the seed Patient;
3. writes the `aiofhir-seed-patient-read` `AccessPolicyV2` and assigns it to
   that Membership.

The policy is pinned to the seed Patient id. Haste evaluates it on every
request the member makes, whatever the token's SMART scope, so it does not rely
on the client asking for `patient/` scopes. It grants only:

- `GET metadata`;
- `GET Patient/{id}` and `GET Patient?_id={id}` for the seed patient;
- reads of `Observation`, `Condition`, `MedicationRequest`, `AllergyIntolerance`
  and `Appointment` whose stored `subject`, `patient` or `participant.actor`
  is that Patient;
- `GET Medication/{id}` (reference data the medication helper resolves);
- type searches of those five types with one unmodified, unchained
  `patient={id}` (or `Patient/{id}`) and no `_has`.

Everything else is denied: other patients, searches without the patient,
system search, every `_history`, compartment URLs, operations, batch and
transaction, and all writes. Includes are filtered by Haste with the same read
rules. `just check-policy` verifies it against the server (below).

If the admin token cannot write the system project, the seed stops with a
message: set `SYSTEM_TOKEN` to a token for the system project, or invite the
user in the Haste admin console and set `SEED_USER_ID`. Without either
variable, no Membership is written (the seed no longer links your admin login
to the seed patient).

```bash
SEED_USER_EMAIL=you+patient@example.com just seed
```

On the hosted tenant the admin token cannot read or write the `system` project
(401). Invite the user in the Haste admin console with the **`member`** role,
accept the email, and set `SEED_USER_ID` to that User's id. Haste issues
`patient/*` scopes and the `patient` launch context only to `member` users. For
an `owner` it silently drops them, and the token comes back without a patient.

## Smoke test

`haste/smoke.py` (`just smoke`) signs in with the `aiofhir-smoke` client the seed
created (`SMOKE_CLIENT_ID`, `SMOKE_CLIENT_SECRET`), not the admin `CLIENT_ID`.
Sign in **as the patient user** in a private window; an admin session has no
patient and the token comes back without one. `SMOKE_NO_BROWSER=1` prints the
URL instead of opening it.

```bash
just smoke
```

## Live tests

`tests/live/test_haste.py` needs a token that is bound to the seeded patient.
`SAVE_TOKENS=1 just smoke` writes that `TokenSet` to `haste/.tokens.json`. The
file is gitignored and mode 600, and it is rewritten after each refresh so the
rotated refresh token is kept. Then run:

```bash
AIOFHIR_LIVE=1 just test tests/live
```

The live tests check that:

- the token response names a patient;
- every helper item is cited and belongs to that patient;
- the seeded cancelled appointment is filtered out;
- paging with `_count=1` follows `next` links and stops.

Delete `haste/.tokens.json` when you are done.

## Checking the patient access policy

```bash
just check-policy             # no patient sign-in, nothing written
just check-policy-as-patient  # also signs in as the patient with user/*.rs
```

`just check-policy` needs `BASE`, `PATIENT_ID`, `SEED_USER_ID` and an admin
token (`HASTE_TOKEN`, or a browser sign-in with `CLIENT_ID`/`CLIENT_SECRET`). It

1. asks the token endpoint for an `aiofhir-seed` token without a secret, which
   must fail with `invalid_client`;
2. runs about 45 requests through `AccessPolicyV2/aiofhir-seed-patient-read/$evaluate-policy`
   as the patient User. Haste only evaluates them, so the write cases are safe.

`just check-policy-as-patient` then asks you to sign in **as the patient user**
in a private window through `aiofhir-seed` with `user/*.rs`, the widest read
token the patient could get with the secret, and sends every GET case for
real. A denied case must come back 401/403, or hold nothing but the patient's
own data. `SAVE_TOKENS=1` keeps that token in `haste/.user-tokens.json`
(gitignored, mode 600, no refresh token) for
`tests/live/test_haste_policy.py`.

Access tokens carry the policy *version* they were issued with. After a seed
changes the policy, sign in again (delete `haste/.tokens.json` and rerun
`SAVE_TOKENS=1 just smoke`) before testing.
