# Sign-in and state handling

`aiofhir.smart` implements the SMART on FHIR authorization-code flow with PKCE,
token refresh, the EHR launch, and SMART Backend Services.

## Standalone launch

```python
smart = SmartClient(
    fhir_base_url,
    client_id,
    redirect_uri,
    scope=None,
    client_secret=None,
    profile=None,
    private_key=None,
    timeout=10.0,
)
request = await smart.authorization_request()
tokens = await smart.exchange_code(code, request.code_verifier)
```

- Discovery reads `{base}/.well-known/smart-configuration` (a profile can add
  fallback paths) and requires S256 PKCE. Results are cached per base URL.
- The request sends `aud` (the FHIR base), `state`, and an S256 code challenge.
- Client authentication is chosen from the server's
  `token_endpoint_auth_methods_supported`: `client_secret_basic`, then
  `client_secret_post`, or `private_key_jwt` when `private_key=` is set.
- When the scope includes `launch/patient`, a token response without `patient`
  raises `TokenError`.
- Discovery and token requests time out after `timeout` seconds; a transport
  failure at the token endpoint raises `TokenError`.

## State and PKCE: the caller's job

The library creates `state` and the code verifier; storing and checking them is
up to the application:

1. Store `request.state` and `request.code_verifier` on the server, never only in
   the browser.
2. On the callback, call `verify_state(expected=..., received=...)`. It compares
   in constant time and raises `StateMismatch`.
3. Use each pair once: delete it before or right after the code exchange, and
   expire unused pairs (the reference backend uses 10 minutes).


## Scopes

By default the scope is derived from the server's SMART `capabilities` and
`scopes_supported` (see [profiles](profiles.md#scopes)). Pass `scope=` only when
your app registration allows less than the server advertises.

## Tokens

`TokenSet` holds the access token, refresh token, expiry, scope, and launch
context (`patient`, `encounter`, `need_patient_banner`, `fhir_user`). Its
`repr()` shows only the patient and expiry.

- Persist it with `tokens.to_dict()` and restore it with `TokenSet.from_dict()`.
  The library never stores tokens; encrypt them at rest.
- `fhir_user` is read from the id_token **without verifying its signature**. Do
  not use it for authorization.

## Refresh

```python
async def save(tokens: TokenSet) -> None:
    await my_store.put(user_id, tokens.to_dict())


holder = RefreshingToken(smart, tokens, on_refresh=save)
```

- `get()` refreshes 60 seconds before expiry, under a lock, so concurrent callers
  share one refresh.
- `on_refresh` receives each rotated `TokenSet`; save it, or a rotated refresh
  token is lost.
- A refresh keeps the launch context, and refuses a response that names a
  different patient.
- `invalid_grant`, or no refresh token, raises `AuthExpired`, whose message is
  fixed: "Your health record connection expired. Please reconnect to continue."

`FhirClient` refreshes once on a 401 and retries the request once.

## EHR launch

The EHR opens your launch URL with `iss` and `launch`:

```python
from aiofhir.smart import SmartClient, parse_ehr_launch

launch = parse_ehr_launch(request_query_string)
smart = SmartClient.for_ehr_launch(
    launch.iss,
    allowed_issuers=["https://ehr.example.org/fhir/r4"],
    client_id="my-app",
    redirect_uri="https://my.app/callback",
)
request = await smart.authorization_request(launch=launch.launch)
```

- `iss` comes from whoever opens the launch URL. Only an issuer in
  `allowed_issuers` is accepted; otherwise your app would send its credentials and
  the code to a token endpoint the sender chose.
- The request sends `launch` and asks for the `launch` scope instead of
  `launch/patient`. The exchange keeps whatever context the EHR returns and does
  not require a patient; check `tokens.patient` before building a `PatientRecord`.

## Private key JWT

Epic production prefers asymmetric client authentication. Install
`aiofhir[jwt]`, register the public key with the server, and pass the private key:

```python
from aiofhir.smart import ClientAssertionKey, SmartClient

key = ClientAssertionKey(private_key_pem, key_id="my-key-1", algorithm="RS384")  # or ES384
smart = SmartClient(fhir_base_url, client_id, redirect_uri, private_key=key)
```

Each token request carries a fresh assertion (`iss` and `sub` are the client,
`aud` is the token endpoint, a new `jti`, expiry in five minutes). A server that
does not list `private_key_jwt` raises `TokenError` and gets no assertion.

## Backend services (client_credentials)

For seeding and CI, a confidential client can get a token for itself, with no
patient or user:

```python
tokens = await smart.client_credentials()  # system read scopes
tokens = await smart.client_credentials(scope="system/*.cruds")  # anything else
```

The default scope is the `system/` read scopes the server advertises. A client
with neither `client_secret` nor `private_key` raises `TokenError`.
