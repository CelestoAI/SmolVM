"""Explicit hostname restrictions, separate from the legacy IP allowlist.

Importing this contract never imports the optional TLS-inspection engine.
"""

import hashlib
import ipaddress
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


class NetworkPolicy(BaseModel):
    """Immutable HTTP/HTTPS destination policy; an empty list denies new egress.

    HTTPS is inspected. Names are exact, not suffix or wildcard matches.
    The VM integration must explicitly opt in; this does not change the legacy
    InternetSettings interface.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    allowed_domains: tuple[str, ...] = Field(max_length=100)

    @field_validator("allowed_domains")
    @classmethod
    def canonical_domains(cls, entries: tuple[str, ...]) -> tuple[str, ...]:
        domains = set()
        for entry in entries:
            if not entry or entry != entry.strip() or any(ord(c) < 33 for c in entry):
                raise ValueError("Use exact domain names without whitespace or URLs.")
            try:
                host = entry.removesuffix(".").encode("idna").decode("ascii").lower()
            except UnicodeError as error:
                raise ValueError("Use valid DNS domain names.") from error
            labels = host.split(".")
            if (
                len(host) > 253
                or len(labels) < 2
                or not any(c.isalpha() for c in labels[-1])
                or any(_LABEL.fullmatch(label) is None for label in labels)
            ):
                raise ValueError("Use exact DNS names, not IP addresses, wildcards, ports or URLs.")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                domains.add(host)
            else:
                raise ValueError("IP addresses are not domain names.")
        return tuple(sorted(domains))

    @property
    def identity(self) -> str:
        """Stable identity of canonical intent, independent of input order."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()
