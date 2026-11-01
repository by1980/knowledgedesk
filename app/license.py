"""Subscription license check.

You (the vendor) sign a small license file with your private key; each installation holds only
your public key. The app reads the license on every check, so renewing the subscription is just
replacing /data/license.key with a newer one (no restart needed).

The license file looks like:  <base64 JSON payload>.<base64 signature>
Payload: {"customer": "...", "expires": "2027-01-31", "max_users": 50}
"""
import base64
import json
from dataclasses import dataclass
from datetime import date, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import settings


@dataclass
class LicenseStatus:
    state: str  # "valid" | "grace" | "expired" | "missing" | "invalid" | "unchecked"
    customer: str = ""
    expires: str = ""
    max_users: int = 0
    message: str = ""

    @property
    def allows_use(self) -> bool:
        return self.state in ("valid", "grace", "unchecked")


def verify(text: str, public_key_b64: str) -> dict:
    payload_b64, sig_b64 = text.strip().split(".")
    payload = base64.urlsafe_b64decode(payload_b64 + "==")
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    key.verify(base64.urlsafe_b64decode(sig_b64 + "=="), payload)  # raises InvalidSignature
    return json.loads(payload)


def status(today: date | None = None) -> LicenseStatus:
    today = today or date.today()
    if not settings.license_public_key:
        # No vendor key configured (development / trial setup): licensing is not enforced.
        return LicenseStatus("unchecked", message="Licensing is not configured.")
    if not settings.license_file.exists():
        return LicenseStatus("missing", message="No license installed. Please contact your provider.")
    try:
        data = verify(settings.license_file.read_text(), settings.license_public_key)
    except (ValueError, InvalidSignature, json.JSONDecodeError):
        return LicenseStatus("invalid", message="The license file is not valid. Please contact your provider.")

    expires = date.fromisoformat(data["expires"])
    info = dict(customer=data.get("customer", ""), expires=data["expires"], max_users=int(data.get("max_users", 0)))
    if today <= expires:
        return LicenseStatus("valid", **info)
    if today <= expires + timedelta(days=settings.license_grace_days):
        left = (expires + timedelta(days=settings.license_grace_days) - today).days
        return LicenseStatus("grace", message=f"Subscription expired on {expires}. The system will stop "
                                              f"answering in {left} days. Please renew.", **info)
    return LicenseStatus("expired", message=f"Subscription expired on {expires}. Please renew to continue.", **info)
