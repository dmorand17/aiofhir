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
- `GET Medication/{id}` (reference data the medication helper resolves).

Everything else is denied: other patients, every search, every `_history`,
compartment URLs, operations, batch and transaction, and all writes.
`just check-policy` verifies it against the server (below).

### Searches: `SEED_ALLOW_SEARCH=1`

Haste's policy engine cannot see search parameters: `%request.parameters` is
always empty in a policy expression (HasteHealth `fhir-client/src/url.rs`,
`ParsedParameters::flatten`). A compartment URL and `$everything` don't expose
the patient id either. So a policy can allow a search *type* or deny it, but it
can't limit a search to one patient.

The record helpers and `just smoke` need searches. `SEED_ALLOW_SEARCH=1 just seed`
adds a rule that allows type searches of `Patient` and the five types above,
whatever the parameters. Then only the token confines them:

- a `patient/` token (`aiofhir-smoke`, the chat bot) is safe. Haste filters
  every search to the patient's compartment;
- a `user/` or `system/` token the patient obtains is not. One way to get one is
  signing in to Haste's built-in `admin-app` client. With such a token, the
  patient can search other patients' records of those six types. Reads by id
  stay pinned.

`just check-policy` reads the stored policy and adjusts. The searches it can't
confine are marked `GAP`, and `check-policy-as-patient` (a `user/*.rs` token)
reports them as leaks. Keep `SEED_ALLOW_SEARCH` set the same way for
`AIOFHIR_LIVE=1 just test tests/live`.

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

## Signing in and troubleshooting

### Which account

| Command | Sign in as |
|---|---|
| `just seed`, `just seed-cleanup`, `just check-policy` | Your own Haste admin account (a `livekit` tenant admin) |
| `just smoke`, `just check-policy-as-patient` | The seed patient user (`SEED_USER_EMAIL` / `SEED_USER_ID`) |

Never sign in to the seed as the patient.

### Use a private window for each account

The scripts open your default browser through Python's `webbrowser`. If that
browser already has a Haste session, Haste reuses it without asking: the
callback arrives in under a second and you never see a sign-in page. If that
session is the patient's, the seed gets a patient token and Haste returns 403
on the first bundle post.

Point `BROWSER` at a private window so each run picks its account. With Chrome
on macOS:

```bash
BROWSER="open -na 'Google Chrome' --args --incognito %s" SAVE_HASTE_TOKEN=1 just seed
```

The same `BROWSER=...` prefix works for `just smoke` and the policy checks.

### Whose token is in `.env`?

This prints the token's claims, not the token itself (run from the repo root):

```bash
uv run python -c "import base64,json,os;from dotenv import dotenv_values as d;t=d('.env')['HASTE_TOKEN'].split('.')[1];c=json.loads(base64.urlsafe_b64decode(t+'=='));print({k:c.get(k) for k in ('sub','fhirUser','scope','patient')})"
```

If `fhirUser` is `Patient/...`, or `patient` is set, it came from a patient
session. Remove the token and sign in again as the admin.

### Errors

| Symptom | Cause | Fix |
|---|---|---|
| `Haste rejected the admin token (401)` | `HASTE_TOKEN` in `.env` has expired | Delete the `HASTE_TOKEN` line and run again to sign in |
| `just smoke` or the live helper tests get 403 on searches | The policy was seeded without searches | `SEED_ALLOW_SEARCH=1 just seed` (read the risk under *Searches*) |
| `bundle post failed (403)` | Signed in as the wrong account (usually the patient) | Delete `HASTE_TOKEN` and run again in a private window as the admin |
| `haste/.env is ignored` warning | Leftover from older versions | Move its values to the repo-root `.env`, then delete it |

Delete `HASTE_TOKEN` from `.env` when you are done seeding.

### A full live run, in order

```bash
SAVE_HASTE_TOKEN=1 just seed                   # admin; add SEED_ALLOW_SEARCH=1 for smoke
just check-policy                              # admin; needs PATIENT_ID and SEED_USER_ID
rm -f haste/.tokens.json && SAVE_TOKENS=1 just smoke   # patient
SAVE_TOKENS=1 just check-policy-as-patient     # patient
AIOFHIR_LIVE=1 just test tests/live
rm -f haste/.tokens.json haste/.user-tokens.json   # then remove HASTE_TOKEN from .env
```
