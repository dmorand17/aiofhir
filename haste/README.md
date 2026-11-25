# Haste Health tooling

Scripts that seed a Haste Health project and sign in to it for the Phase 3 smoke
test. Nothing here ships in the `aiofhir` package. Run them from the repo root
with `just seed` and `just smoke`, or `uv run python -m haste.seed` and
`uv run python -m haste.smoke`.

| Path | Purpose |
|---|---|
| `seed.py`, `seeding.py` | Seed entry point and the idempotent seed resources |
| `smoke.py`, `login.py` | Browser sign-in, every record helper, one forced refresh |
| `bundles/` | Hand-posted transactions: the admin seed client, and a seed cleanup |
| `synthea-data/` | Practitioner and hospital bundles the seed loads (`SYNTHEA_DIR`) |
| `.env.example` | Variables the scripts read. Copy them into the repo-root `.env`. |

Unit tests are in `tests/haste/`. The opt-in live check is `tests/live/test_haste.py`.

## Seeding

`haste/seed.py` (`just seed`) needs an access token. When `HASTE_TOKEN` is unset, the script signs in and writes that token to `.env`.

Prerequisite: a `ClientApplication` already exists on the project. The seed script creates the `aiofhir-smoke` client, so it cannot sign in as that client to create itself. `bundles/create-seed-client.json` is a transaction that creates that client as `aiofhir-seed`. Post the bundle to the FHIR base.

That existing client must:

- include redirect URI `http://localhost:8765/callback`
- allow the seed login scope `user/*.cruds openid fhirUser`, or the scope you set in `SCOPE`

Set `BASE` to the FHIR base and `CLIENT_ID` to that client. Set `CLIENT_SECRET` when the client is confidential. `.env` is gitignored and written with mode 600. The admin token stays in memory unless `SAVE_HASTE_TOKEN=1`.

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
3. writes a read-only `AccessPolicyV2` and assigns it to that Membership.

If the admin token cannot write the system project, the seed stops with a
message: set `SYSTEM_TOKEN` to a token for the system project, or invite the
user in the Haste admin console and set `SEED_USER_ID`. Without either
variable, no Membership is written (the seed no longer links your admin login
to the seed patient).

```bash
SEED_USER_EMAIL=you+patient@example.com just seed
```

## Smoke test

`haste/smoke.py` (`just smoke`) signs in with the `aiofhir-smoke` client the seed
created (`SMOKE_CLIENT_ID`, `SMOKE_CLIENT_SECRET`), not the admin `CLIENT_ID`.
Sign in **as the patient user** in a private window; an admin session has no
patient and the token comes back without one. `SMOKE_NO_BROWSER=1` prints the
URL instead of opening it.

```bash
just smoke
```
