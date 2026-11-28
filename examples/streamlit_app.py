"""A Streamlit demo of aiofhir: SMART sign-in, then every record helper.

Run it with `just demo` (Streamlit on port 8765). It reads the repo-root .env:

- BASE: the FHIR base URL.
- SMOKE_CLIENT_ID (default aiofhir-smoke) and SMOKE_CLIENT_SECRET: a SMART client
  whose redirect URIs include DEMO_REDIRECT_URI. `just seed` creates this client
  on Haste with http://localhost:8765/callback, the default here.
- DEMO_REDIRECT_URI (optional): where the server sends the browser back.
- AIOFHIR_PROFILE (optional): generic, haste, epic or cerner. Defaults to haste for
  a Haste URL, otherwise generic.

What it shows:

- `state` and the PKCE verifier are kept on the server, keyed by `state`, and used
  once. Signing in leaves the app, so the browser comes back in a new Streamlit
  session; a per-session store would lose them.
- Tokens live only in this browser session's server-side state, in memory. They
  are never displayed. This is fine for a local demo; an agent in production
  should use `RemoteRecord` and the `aiofhir[server]` backend instead.
- Each helper's `Summary` is shown twice: formatted for a person in their time
  zone, and as `to_text()`, exactly what a model would read.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import available_timezones

import streamlit as st
from dotenv import load_dotenv

from aiofhir import RECORD_HELPERS, AuthExpired, FhirClient, PatientRecord, Record
from aiofhir.display import format_summary
from aiofhir.errors import AiofhirError, FhirError
from aiofhir.profiles import CernerProfile, EpicProfile, GenericProfile, HasteProfile
from aiofhir.profiles.base import ServerProfile
from aiofhir.smart import RefreshingToken, SmartClient, TokenSet, verify_state
from aiofhir.summary import Summary

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
STATE_TTL_SECONDS = 600.0
PROFILES: dict[str, type[ServerProfile]] = {
    "generic": GenericProfile,
    "haste": HasteProfile,
    "epic": EpicProfile,
    "cerner": CernerProfile,
}


@dataclass(frozen=True)
class Pending:
    """A sign-in waiting for its callback."""

    state: str
    verifier: str
    expires: float


def profile_name(base: str, configured: str | None) -> str:
    if configured:
        return configured
    return "haste" if urlsplit(base).hostname == "api.haste.health" else "generic"


def smart_client() -> SmartClient:
    base = os.environ.get("BASE", "").strip()
    if not base:
        st.error("Set BASE in the repo-root .env to a FHIR base URL.")
        st.stop()
    name = profile_name(base, os.environ.get("AIOFHIR_PROFILE", "").strip() or None)
    if name not in PROFILES:
        st.error(f"AIOFHIR_PROFILE must be one of {', '.join(PROFILES)}.")
        st.stop()
    return SmartClient(
        base,
        client_id=os.environ.get("SMOKE_CLIENT_ID", "").strip() or "aiofhir-smoke",
        redirect_uri=os.environ.get("DEMO_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI,
        client_secret=os.environ.get("SMOKE_CLIENT_SECRET", "").strip() or None,
        profile=PROFILES[name](),
    )


def _new_pending_store() -> dict[str, Pending]:
    return {}


# Process-wide, server-side store of sign-ins waiting for a callback. Streamlit
# reruns this script on every interaction, so a module global would not last.
pending_sign_ins: Callable[[], dict[str, Pending]] = st.cache_resource(_new_pending_store)


async def load_record(record: Record) -> dict[str, Summary]:
    """Every helper, one after another, through the `Record` protocol."""
    summaries: dict[str, Summary] = {}
    for name in RECORD_HELPERS:
        summaries[name] = await getattr(record, name)()
    return summaries


async def read_patient_record(
    smart: SmartClient, tokens: TokenSet
) -> tuple[dict[str, Any] | None, dict[str, Summary]]:
    """The Patient resource and every helper's summary."""

    async def keep(refreshed: TokenSet) -> None:
        st.session_state["tokens"] = refreshed  # a rotated refresh token must not be lost

    holder = RefreshingToken(smart, tokens, on_refresh=keep)
    patient_id = tokens.patient or ""
    async with FhirClient(smart.base, holder, profile=smart.profile) as fhir:
        record = PatientRecord(fhir, patient_id=patient_id)
        # search_raw forces `_id` to the token's patient: the app cannot ask for anyone else.
        found = await record.search_raw("Patient")
        patient = found.resources[0] if found.resources else None
        return patient, await load_record(record)


async def try_another_patient(
    smart: SmartClient, tokens: TokenSet, other_id: str
) -> list[dict[str, Any]]:
    """Ask for another patient's data three ways. Each attempt should be refused."""
    holder = RefreshingToken(smart, tokens)
    rows: list[dict[str, Any]] = []
    async with FhirClient(smart.base, holder, profile=smart.profile) as fhir:
        record = PatientRecord(fhir, patient_id=tokens.patient or "")

        # 1. The helpers never take a patient from the caller; nothing is sent.
        try:
            await record.search_raw("Patient", {"_id": other_id})
            rows.append(_row("search_raw(Patient, _id=...)", "aiofhir", False, "the search ran"))
        except ValueError as exc:
            rows.append(_row("search_raw(Patient, _id=...)", "aiofhir", True, str(exc)))

        # 2. The raw client has no such check, so the server's token scope decides.
        try:
            await fhir.read("Patient", other_id)
            rows.append(_row(f"read Patient/{other_id}", "FHIR server", False, "LEAK: returned"))
        except FhirError as exc:
            rows.append(_row(f"read Patient/{other_id}", "FHIR server", True, f"HTTP {exc.status}"))

        # 3. A search naming the other patient: the server confines it to the token's patient.
        try:
            found = await fhir.search("Observation", {"patient": other_id})
            theirs = [r for r in found.resources if _names_patient(r, other_id)]
            detail = (
                f"LEAK: {len(theirs)} of theirs" if theirs else f"{len(found.resources)} results"
            )
            rows.append(_row("search Observation?patient=...", "FHIR server", not theirs, detail))
        except FhirError as exc:
            rows.append(
                _row("search Observation?patient=...", "FHIR server", True, f"HTTP {exc.status}")
            )
    return rows


def _row(attempt: str, layer: str, blocked: bool, detail: str) -> dict[str, Any]:
    return {"attempt": attempt, "layer": layer, "blocked": blocked, "detail": detail}


def _names_patient(resource: dict[str, Any], patient_id: str) -> bool:
    subject = resource.get("subject")
    reference = subject.get("reference") if isinstance(subject, dict) else None
    return isinstance(reference, str) and reference.endswith(f"Patient/{patient_id}")


def patient_name(patient: dict[str, Any]) -> str:
    for name in patient.get("name") or []:
        if not isinstance(name, dict):
            continue
        if isinstance(name.get("text"), str) and name["text"]:
            return str(name["text"])
        given = " ".join(str(part) for part in name.get("given") or [])
        full = f"{given} {name.get('family') or ''}".strip()
        if full:
            return full
    return "Unnamed patient"


def items_table(summary: Summary) -> list[dict[str, Any]]:
    return [
        {
            "text": item.text,
            "date": item.at.isoformat() if item.at is not None else "",
            "citation": str(item.citation),
        }
        for item in summary.items
    ]


# Pages


def home() -> None:
    st.title("aiofhir demo")
    smart = smart_client()
    tokens: TokenSet | None = st.session_state.get("tokens")
    if tokens is None:
        sign_in(smart)
    else:
        show_record(smart, tokens)


def sign_in(smart: SmartClient) -> None:
    st.write(
        f"Sign in at **{smart.base}** as a patient. The app discovers the server, "
        "derives the scope from its SMART configuration, and uses PKCE."
    )
    if st.button("Start sign-in", type="primary"):
        try:
            request = asyncio.run(smart.authorization_request())
        except AiofhirError as exc:
            st.error(f"Could not start sign-in: {exc}")
            return
        store = pending_sign_ins()
        now = time.monotonic()
        for state in [s for s, p in store.items() if p.expires <= now]:
            del store[state]
        store[request.state] = Pending(
            request.state, request.code_verifier, now + STATE_TTL_SECONDS
        )
        st.session_state["authorize_url"] = request.url
    url = st.session_state.get("authorize_url")
    if url:
        st.link_button("Continue to the sign-in page", url)
        st.caption(
            "Use a private window if you are signed in to the server as an admin: "
            "Haste gives patient access only to a `member` user linked to a Patient."
        )


def callback() -> None:
    """The redirect target. Checks state, exchanges the code once, then goes home."""
    params = st.query_params
    state, code, error = params.get("state", ""), params.get("code", ""), params.get("error", "")
    pending = pending_sign_ins().pop(state, None)  # single use, even if the rest fails
    if pending is None or pending.expires <= time.monotonic():
        st.error("This sign-in link has expired or was already used. Start again.")
        st.page_link(HOME, label="Back to the demo")
        return
    if error or not code:
        st.error(f"Sign-in was not completed: {error or 'no code returned'}.")
        st.page_link(HOME, label="Back to the demo")
        return
    smart = smart_client()
    try:
        verify_state(expected=pending.state, received=state)
        tokens = asyncio.run(smart.exchange_code(code, pending.verifier))
    except AiofhirError as exc:
        st.error(f"Sign-in failed: {exc}")
        st.page_link(HOME, label="Back to the demo")
        return
    if not tokens.patient:
        st.error("The token has no patient. Sign in as a patient user.")
        st.page_link(HOME, label="Back to the demo")
        return
    st.session_state["tokens"] = tokens
    st.session_state.pop("summaries", None)
    st.session_state.pop("patient", None)
    st.query_params.clear()
    st.switch_page(HOME)


def show_record(smart: SmartClient, tokens: TokenSet) -> None:
    with st.sidebar:
        st.subheader("Session")
        st.write(f"Patient: `{tokens.patient}`")
        st.write(f"Token expires: {tokens.expires_at:%Y-%m-%d %H:%M} UTC")
        st.caption(f"Scope: {tokens.scope}")
        zone = st.selectbox(
            "Your time zone",
            sorted(available_timezones()),
            index=sorted(available_timezones()).index("America/New_York"),
        )
        if st.button("Sign out"):
            for key in ("tokens", "patient", "summaries", "probe", "authorize_url"):
                st.session_state.pop(key, None)
            st.rerun()

    show_other_patient_probe(smart)

    if st.button("Load the record", type="primary") or "summaries" in st.session_state:
        if "summaries" not in st.session_state or "patient" not in st.session_state:
            try:
                with st.spinner("Reading the record..."):
                    patient, summaries_read = asyncio.run(
                        read_patient_record(smart, st.session_state["tokens"])
                    )
                st.session_state["patient"] = patient
                st.session_state["summaries"] = summaries_read
            except AuthExpired as exc:
                st.session_state.pop("tokens", None)
                st.error(str(exc))
                return
            except AiofhirError as exc:
                st.error(f"Reading the record failed: {exc}")
                return
        show_patient(st.session_state.get("patient"))
        summaries: dict[str, Summary] = st.session_state["summaries"]
        for tab, name in zip(st.tabs(list(summaries)), summaries, strict=True):
            with tab:
                show_summary(summaries[name], zone)


def show_other_patient_probe(smart: SmartClient) -> None:
    with st.expander("Try to open another patient"):
        st.write(
            "Your token belongs to one patient. Enter another patient's id (for "
            "example from the server's admin console) to see each layer refuse."
        )
        other = st.text_input("Another patient's id").strip()
        if st.button("Try to open this patient", disabled=not other):
            try:
                st.session_state["probe"] = asyncio.run(
                    try_another_patient(smart, st.session_state["tokens"], other)
                )
            except ValueError as exc:
                st.error(f"Not a valid FHIR id: {exc}")
            except AiofhirError as exc:
                st.error(f"The attempt failed: {exc}")
        for row in st.session_state.get("probe", []):
            icon = "✅ blocked" if row["blocked"] else "❌ NOT blocked"
            st.write(f"{icon} by **{row['layer']}**: `{row['attempt']}` ({row['detail']})")


def show_patient(patient: dict[str, Any] | None) -> None:
    if patient is None:
        st.warning("The server returned no Patient resource for this token.")
        return
    st.subheader(patient_name(patient))
    gender, born, resource = st.columns(3)
    gender.metric("Gender", str(patient.get("gender") or "unknown"))
    born.metric("Birth date", str(patient.get("birthDate") or "unknown"))
    resource.metric("Resource", f"Patient/{patient.get('id', '')}")
    with st.expander("The Patient resource (FHIR JSON)"):
        st.json(patient)


def show_summary(summary: Summary, zone: str) -> None:
    if not summary.items:
        st.info("Nothing found.")
    person, agent = st.columns(2)
    with person:
        st.markdown("**For a person** (`format_summary`)")
        st.code(format_summary(summary, tz=zone), language=None)
    with agent:
        st.markdown("**For a model** (`Summary.to_text`)")
        st.code(summary.to_text(), language=None)
    if summary.items:
        st.dataframe(items_table(summary), hide_index=True)
    for warning in summary.warnings:
        st.warning(warning)
    if summary.truncated:
        st.warning("Paging stopped at the page limit; there may be more.")


HOME = st.Page(home, title="aiofhir demo", url_path="home", default=True)
CALLBACK = st.Page(callback, title="Signing in", url_path="callback", visibility="hidden")
st.navigation([HOME, CALLBACK], position="hidden").run()
