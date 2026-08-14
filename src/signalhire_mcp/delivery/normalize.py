"""Raw SignalHire callback -> a vendor-neutral profile.

Normalising once, here, is what keeps adapters from each growing their own copy
of "which contact subType counts as the primary email". The previous system had
that logic inline in a Vercel route; a second consumer would have meant a second
copy that drifted.

The raw payload is still kept verbatim on the event. Normalisation is lossy by
design — it picks a primary email, flattens locations — and being able to
reprocess from the original is what makes a normalisation bug fixable without
re-billing the reveal.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Contact(BaseModel):
    type: str
    value: str
    sub_type: str = ""
    rating: str = ""


class Employment(BaseModel):
    title: str = ""
    company: str = ""
    start_date: str = ""
    end_date: str = ""
    is_current: bool = False
    summary: str = ""


class Education(BaseModel):
    university: str = ""
    faculty: str = ""
    degrees: list[str] = Field(default_factory=list)
    started_year: int | None = None
    ended_year: int | None = None


class RevealedProfile(BaseModel):
    """One successfully revealed person, in a shape no vendor owns."""

    #: The identifier we submitted, echoed back by SignalHire. This is the join
    #: key to whatever record asked for the reveal — a LinkedIn URL, an email,
    #: or a UID — and matters more than SignalHire's own uid, because the
    #: caller only ever knew the thing they sent.
    item: str
    status: str
    uid: str = ""
    full_name: str = ""
    headline: str = ""
    summary: str = ""
    photo_url: str = ""
    location: str = ""
    linkedin_url: str = ""
    current_company: str = ""

    contacts: list[Contact] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    primary_email: str = ""
    primary_phone: str = ""

    skills: list[str] = Field(default_factory=list)
    experience: list[Employment] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


def normalize_payload(payload: Any) -> list[RevealedProfile]:
    """Turn a callback body into profiles, tolerating anything.

    Never raises. A malformed payload yields fewer profiles, not an exception —
    this runs inside the delivery worker, and an exception here would park an
    event that a slightly more forgiving parse could have delivered.
    """
    if not isinstance(payload, list):
        return []
    profiles: list[RevealedProfile] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            profiles.append(_one(item))
        except Exception:  # noqa: BLE001 - see docstring
            continue
    return profiles


def _one(item: dict[str, Any]) -> RevealedProfile:
    status = str(item.get("status") or "unknown")
    sent = str(item.get("item") or "")
    candidate = item.get("candidate")

    if not isinstance(candidate, dict):
        # failed / credits_are_over / timeout_exceeded / duplicate_query all
        # arrive without a candidate. They are still worth delivering: a
        # downstream system needs to know the reveal resolved to nothing, or it
        # will keep retrying a person SignalHire does not have.
        return RevealedProfile(item=sent, status=status)

    contacts = [
        Contact(
            type=str(c.get("type") or ""),
            value=str(c.get("value") or ""),
            sub_type=str(c.get("subType") or ""),
            rating=str(c.get("rating") or ""),
        )
        for c in _as_list(candidate.get("contacts"))
        if isinstance(c, dict) and c.get("value")
    ]
    emails = [c.value for c in contacts if c.type == "email"]
    phones = [c.value for c in contacts if c.type == "phone"]

    experience = [
        Employment(
            title=str(e.get("position") or ""),
            company=str(e.get("company") or ""),
            start_date=str(e.get("started") or ""),
            end_date=str(e.get("ended") or ""),
            is_current=bool(e.get("current")),
            summary=str(e.get("summary") or ""),
        )
        for e in _as_list(candidate.get("experience"))
        if isinstance(e, dict) and (e.get("company") or e.get("position"))
    ]

    education = [
        Education(
            university=str(e.get("university") or ""),
            faculty=str(e.get("faculty") or ""),
            degrees=[str(d) for d in _as_list(e.get("degree"))],
            started_year=_as_int(e.get("startedYear")),
            ended_year=_as_int(e.get("endedYear")),
        )
        for e in _as_list(candidate.get("education"))
        if isinstance(e, dict)
    ]

    social = _as_list(candidate.get("social"))
    linkedin = next(
        (str(s.get("link")) for s in social if isinstance(s, dict) and s.get("type") == "li"),
        "",
    )
    # Fall back to the submitted identifier when it was itself a LinkedIn URL.
    if not linkedin and "linkedin.com" in sent:
        linkedin = sent

    locations = _as_list(candidate.get("locations"))
    location = ""
    if locations and isinstance(locations[0], dict):
        location = str(locations[0].get("name") or "")

    photo = candidate.get("photo")
    photo_url = str(photo.get("url")) if isinstance(photo, dict) and photo.get("url") else ""

    return RevealedProfile(
        item=sent,
        status=status,
        uid=str(candidate.get("uid") or ""),
        full_name=str(candidate.get("fullName") or ""),
        headline=str(candidate.get("headLine") or ""),
        summary=str(candidate.get("summary") or ""),
        photo_url=photo_url,
        location=location,
        linkedin_url=linkedin,
        current_company=next((e.company for e in experience if e.is_current), ""),
        contacts=contacts,
        emails=emails,
        phones=phones,
        primary_email=_primary(contacts, "email", ("personal", "work")),
        primary_phone=_primary(contacts, "phone", ("work_phone", "mobile", "work")),
        skills=[str(s) for s in _as_list(candidate.get("skills"))],
        experience=experience,
        education=education,
    )


def _primary(contacts: list[Contact], kind: str, preference: tuple[str, ...]) -> str:
    """Pick the best contact of a kind, preferring known-good subtypes.

    Preference order is explicit rather than "first one wins" because
    SignalHire returns work and personal addresses in no guaranteed order, and
    which one is wanted differs by kind: a personal email reaches a candidate
    who has left the company, whereas a work phone is the one that gets
    answered.
    """
    typed = [c for c in contacts if c.type == kind and c.value]
    for wanted in preference:
        for contact in typed:
            if wanted in contact.sub_type:
                return contact.value
    return typed[0].value if typed else ""


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
