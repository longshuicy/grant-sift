"""Identity, for a deployment behind Keycloak.

Placeholder in the sense that this app performs no OIDC flow of its own. It
expects to sit behind something that already did: oauth2-proxy,
mod_auth_openidc, or an ingress that validates the Keycloak token and passes
the result down as headers. That is the normal shape for a small internal app
next to an existing Keycloak, and it keeps token handling out of here entirely.

    GRANT_SIFT_AUTH=off     (default) no identity, no gate. Local development.
    GRANT_SIFT_AUTH=proxy   trust identity headers from a verified proxy.
    GRANT_SIFT_AUTH=oidc    not implemented; fails loudly rather than pretending.

THE IMPORTANT PART. In proxy mode the headers are only as trustworthy as the
network path: anyone who can reach this app directly can set
X-Forwarded-Preferred-Username to whatever they like. So proxy mode refuses to
trust them unless the request arrives from an address in
GRANT_SIFT_TRUSTED_PROXIES. Configure that, and bind the app where only the
proxy can reach it.
"""

import ipaddress
import os
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

MODE = os.environ.get("GRANT_SIFT_AUTH", "off").strip().lower()

# oauth2-proxy's defaults. mod_auth_openidc tends to send OIDC_CLAIM_*, so the
# names are configurable rather than assumed.
USER_HEADER = os.environ.get("GRANT_SIFT_AUTH_USER_HEADER", "x-forwarded-preferred-username")
EMAIL_HEADER = os.environ.get("GRANT_SIFT_AUTH_EMAIL_HEADER", "x-forwarded-email")
NAME_HEADER = os.environ.get("GRANT_SIFT_AUTH_NAME_HEADER", "x-forwarded-user")
GROUPS_HEADER = os.environ.get("GRANT_SIFT_AUTH_GROUPS_HEADER", "x-forwarded-groups")

# Optional Keycloak group or role a user must hold. Empty means any
# authenticated user.
REQUIRED_GROUP = os.environ.get("GRANT_SIFT_AUTH_REQUIRED_GROUP", "").strip()

_TRUSTED = [
    t.strip() for t in os.environ.get("GRANT_SIFT_TRUSTED_PROXIES", "").split(",") if t.strip()
]


@dataclass
class Principal:
    """Who is making the request. `anonymous` when auth is off."""

    username: str = "anonymous"
    email: str = ""
    display: str = ""
    groups: list[str] = field(default_factory=list)
    authenticated: bool = False

    @property
    def label(self) -> str:
        """Short string to stamp on a row. Never an email, to keep the
        database from accumulating contact details it has no use for."""
        return self.username if self.authenticated else "anonymous"


ANONYMOUS = Principal()


def _from_trusted_proxy(request: Request) -> bool:
    if not _TRUSTED:
        return False
    host = request.client.host if request.client else ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    for entry in _TRUSTED:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def principal(request: Request) -> Principal:
    """Identify the caller. Never raises; use require_user to gate."""
    if MODE in ("off", "", "none"):
        return ANONYMOUS

    if MODE == "oidc":
        raise HTTPException(
            500,
            "GRANT_SIFT_AUTH=oidc is not implemented. This app does no OIDC "
            "flow of its own. Put oauth2-proxy or mod_auth_openidc in front of "
            "it against your Keycloak realm and use GRANT_SIFT_AUTH=proxy.",
        )

    if MODE != "proxy":
        raise HTTPException(500, f"unknown GRANT_SIFT_AUTH={MODE!r}")

    if not _from_trusted_proxy(request):
        # Refusing is the whole point. Trusting these headers from an
        # unverified peer would let any direct caller name themselves.
        raise HTTPException(
            403,
            "identity headers refused: this request did not come from a "
            "trusted proxy. Set GRANT_SIFT_TRUSTED_PROXIES to the proxy's "
            "address and make sure the app is not directly reachable.",
        )

    h = request.headers
    username = (h.get(USER_HEADER) or "").strip()
    if not username:
        raise HTTPException(
            401,
            f"no identity in {USER_HEADER!r}. The proxy is not passing the "
            "Keycloak username through.",
        )
    groups = [g.strip() for g in (h.get(GROUPS_HEADER) or "").split(",") if g.strip()]
    return Principal(
        username=username,
        email=(h.get(EMAIL_HEADER) or "").strip(),
        display=(h.get(NAME_HEADER) or "").strip() or username,
        groups=groups,
        authenticated=True,
    )


def require_user(request: Request) -> Principal:
    """Gate a write. With auth off this is a no-op returning anonymous, so
    local development behaves exactly as it did before."""
    p = principal(request)
    if MODE in ("off", "", "none"):
        return p
    if REQUIRED_GROUP and REQUIRED_GROUP not in p.groups:
        raise HTTPException(
            403,
            f"requires membership of {REQUIRED_GROUP!r}; you hold {p.groups or 'no groups'}",
        )
    return p


def status() -> dict:
    """What the page needs to render a sign-in state, and what an operator
    needs to see that the gate is actually on."""
    return {
        "mode": MODE,
        "enforced": MODE == "proxy",
        "required_group": REQUIRED_GROUP or None,
        "trusted_proxies_configured": bool(_TRUSTED),
    }
