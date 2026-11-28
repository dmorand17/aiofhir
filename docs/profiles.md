# Server profiles

A `ServerProfile` holds one server's discovery, search and paging differences,
so calling code has no vendor branches. Switching servers is one argument:

```python
from aiofhir.profiles import CernerProfile, EpicProfile, GenericProfile, HasteProfile

smart = SmartClient(base, client_id, redirect_uri, profile=HasteProfile())
fhir = FhirClient(base, holder, profile=smart.profile)
```

Profiles never choose scopes.

## What each profile changes

| | Generic | Haste Health | Epic | Oracle Health (Cerner) |
| --- | --- | --- | --- | --- |
| Status | default | verified live | **unverified** | **unverified** |
| Discovery | `{base}/.well-known/smart-configuration` | also `/w/{tenant}/{project}/api/v1/.well-known/smart-configuration` | standard | standard |
| Search | as given | as given | drops `_sort` | adds a far `lt` bound to an Appointment search that has only `ge` |
| Paging | `next` links | `_offset`, default page 50 (Haste sends no `next` links) | `next` links | `next` links |

Epic and Oracle Health profiles reflect their published documentation and have
not been run against their sandboxes yet. Expect changes in a later release.

Haste notes, from the live run:

- Patient context goes only to a user with the `member` role whose `Membership`
  links to a Patient. For an `owner`, Haste silently drops `patient/*` scopes.
- A rule-engine access policy fails closed ("No policy has granted access") when
  a rule has both `combineBehavior` and a `condition`.

## Scopes

`scopes_from_capabilities(cfg)` builds the authorize scope from the server's SMART
configuration, the same way for every server:

| Piece | Rule |
| --- | --- |
| Context | `patient` when `permission-patient` is present; `user` only when it is absent and `permission-user` is present |
| Permission | `.rs` when `permission-v2` is present and `scopes_supported` lists a `.rs` scope (or is absent); `.read` otherwise |
| Resources | `{context}/*{permission}` when that wildcard is listed or `scopes_supported` is absent; otherwise a per-resource scope for each type the CapabilityStatement can read or search, when `scopes_supported` lists it |
| Launch | `launch/patient` when listed, or when `launch-standalone` is present in patient context; `launch` for an EHR launch |
| Identity | `openid` and `fhirUser` when `sso-openid-connect` is present and they are listed (or `scopes_supported` is absent) |
| Refresh | `offline_access` when `permission-offline` is present or it is listed; otherwise `online_access` when listed |

A server with neither `permission-patient` nor `permission-user` raises
`DiscoveryError`. `client_credentials()` uses the same permission and resource
rules in the `system` context.

## Writing a profile

Subclass `ServerProfile` and override what differs:

- `discovery_paths(base)`: the SMART configuration URLs to try, in order.
- `adapt_search(resource, params)`: rewrite search parameters on the way out.
- `next_page(params, received)`: parameters for the next page when a response
  has no `next` link, or `None` to stop.
- `resolve_medication(request, read)`: the medication name of a MedicationRequest.
