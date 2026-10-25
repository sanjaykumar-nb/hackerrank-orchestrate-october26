"""Identity resolution: request email -> subject user_id -> every identifier it ever had.

Historical values come from user_audit_log.csv. Hashes are also computed from every known
email, because older systems may key records by the hash of an address that changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .identifiers import email_hash, norm_email, norm_phone
from .store import ObservationStore


@dataclass
class Identity:
    user_id: str
    name: str
    emails: dict[str, str] = field(default_factory=dict)   # normalized email -> current|historical
    hashes: dict[str, str] = field(default_factory=dict)
    phones: dict[str, str] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)       # audit rows, for citing audit_ids
    matched_on: str = ""                                     # how the request email resolved

    def keys(self) -> dict[str, str]:
        """Every identifier value -> kind, as stored in obs_keys."""
        out = {self.user_id: "user"}
        out.update({e: "email" for e in self.emails})
        out.update({h: "hash" for h in self.hashes})
        out.update({p: "phone" for p in self.phones if len(p) >= 7})
        return out

    def historical_keys(self) -> set[str]:
        return {v for d in (self.emails, self.hashes, self.phones) for v, s in d.items() if s == "historical"}


class IdentityResolver:
    def __init__(self, store: ObservationStore):
        self.current = {r["user_id"]: r for r in store.rows("SELECT * FROM identities")}
        self.history: dict[str, list[dict]] = {}
        for r in store.rows("SELECT * FROM identity_history ORDER BY event_time, audit_id"):
            self.history.setdefault(r["user_id"], []).append(r)
        self.by_email: dict[str, tuple[str, str]] = {}
        for uid, r in self.current.items():
            self.by_email[norm_email(r["email"])] = (uid, "current")
        for uid, rows in self.history.items():
            for r in rows:
                if r["field_name"] == "email":
                    for value in (r["old_value"], r["new_value"]):
                        self.by_email.setdefault(norm_email(value), (uid, "historical"))

    def resolve_email(self, email: str) -> Identity | None:
        hit = self.by_email.get(norm_email(email))
        if not hit:
            return None
        identity = self.for_user(hit[0])
        identity.matched_on = hit[1]
        return identity

    def for_user(self, user_id: str) -> Identity:
        cur = self.current[user_id]
        ident = Identity(user_id=user_id, name=cur["name"], history=self.history.get(user_id, []))
        ident.emails[norm_email(cur["email"])] = "current"
        ident.hashes[cur["email_hash"].lower()] = "current"
        ident.phones[norm_phone(cur["phone"])] = "current"
        for r in ident.history:
            for value in (r["old_value"], r["new_value"]):
                if r["field_name"] == "email":
                    ident.emails.setdefault(norm_email(value), "historical")
                elif r["field_name"] == "email_hash":
                    ident.hashes.setdefault(value.lower(), "historical")
                elif r["field_name"] == "phone":
                    ident.phones.setdefault(norm_phone(value), "historical")
        for e in list(ident.emails):
            ident.hashes.setdefault(email_hash(e), ident.emails[e])
        return ident
