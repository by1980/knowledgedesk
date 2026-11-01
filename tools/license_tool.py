"""Vendor tool: create your signing keys and issue subscription licenses to customers.

Run this on YOUR computer only. Never give the private key to a customer.

    # once: create your keys (writes vendor_private_key.txt and app/vendor_public_key.txt)
    python tools/license_tool.py keygen

    # each month (or year) per customer:
    python tools/license_tool.py issue --customer "Cohen Plumbing Ltd" --expires 2027-01-31 --max-users 25
    # -> writes licenses/cohen-plumbing-ltd_2027-01-31.key ; send it to the customer's admin,
    #    who installs it in Admin > System (or copy it to /data/license.key on their server).
"""
import argparse
import base64
import json
import re
import sys
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "vendor_private_key.txt"
PUBLIC = ROOT / "app" / "vendor_public_key.txt"


def keygen(force: bool) -> None:
    if PRIVATE.exists() and not force:
        sys.exit(f"{PRIVATE} already exists. Use --force to replace it (old licenses will stop working).")
    key = Ed25519PrivateKey.generate()
    raw_priv = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    raw_pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    PRIVATE.write_text(base64.b64encode(raw_priv).decode() + "\n")
    PRIVATE.chmod(0o600)
    PUBLIC.write_text(base64.b64encode(raw_pub).decode() + "\n")
    print(f"Private key: {PRIVATE}  (keep it secret and backed up; it is ignored by git)")
    print(f"Public key:  {PUBLIC}  (built into every installation)")


def issue(customer: str, expires: str, max_users: int, out_dir: Path) -> Path:
    date.fromisoformat(expires)  # validate
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(PRIVATE.read_text().strip()))
    payload = json.dumps({"customer": customer, "expires": expires, "max_users": max_users,
                          "issued": date.today().isoformat()}, separators=(",", ":")).encode()
    sig = key.sign(payload)
    text = base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", customer.lower()).strip("-") or "customer"
    path = out_dir / f"{slug}_{expires}.key"
    path.write_text(text + "\n")
    return path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--force", action="store_true")
    i = sub.add_parser("issue")
    i.add_argument("--customer", required=True)
    i.add_argument("--expires", required=True, help="YYYY-MM-DD")
    i.add_argument("--max-users", type=int, default=0, help="0 = unlimited")
    i.add_argument("--out", type=Path, default=ROOT / "licenses")
    a = p.parse_args()
    if a.cmd == "keygen":
        keygen(a.force)
    else:
        print(issue(a.customer, a.expires, a.max_users, a.out))


if __name__ == "__main__":
    main()
