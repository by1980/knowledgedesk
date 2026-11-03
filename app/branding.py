"""Company branding: name, website and logo (found automatically on the company's website)."""
import io
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import settings
from .db import conn

log = logging.getLogger("app")
MAX_LOGO_BYTES = 2 * 1024 * 1024


def get() -> dict:
    with conn() as c:
        rows = c.execute("SELECT key, value FROM meta WHERE key LIKE 'brand_%%'").fetchall()
    b = {r["key"][6:]: r["value"] for r in rows}
    return {"name": b.get("name") or settings.company_name, "website": b.get("website", ""),
            "logo": b.get("logo", ""), "logo_tone": b.get("logo_tone", "dark"), "logo_version": b.get("logo_version", "")}


def save(**values: str) -> None:
    with conn() as c:
        for k, v in values.items():
            c.execute("INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                      (f"brand_{k}", v))


def logo_path() -> Path | None:
    name = get()["logo"]
    p = settings.data_dir / "brand" / name if name else None
    return p if p and p.exists() else None


def _tone(data: bytes, ext: str) -> str:
    """'light' for a white/light logo on a transparent background (needs darkening on light pages)."""
    if ext == ".svg":
        text = data.decode(errors="ignore").lower()
        light = text.count("#fff") + text.count("white")
        return "light" if light and light >= text.count("fill=") else "dark"
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(data)).convert("RGBA")
        im.thumbnail((200, 200))
        px = [p for p in im.getdata() if p[3] > 40]
        if not px or len(px) > 0.9 * im.width * im.height:
            return "dark"  # no transparency: it carries its own background
        lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b, _ in px) / len(px)
        return "light" if lum > 200 else "dark"
    except Exception:  # noqa: BLE001
        return "dark"


def store_logo(data: bytes, ext: str) -> None:
    ext = ext.lower() if ext.lower() in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico") else ".png"
    folder = settings.data_dir / "brand"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("logo.*"):
        old.unlink(missing_ok=True)
    (folder / f"logo{ext}").write_bytes(data)
    import hashlib

    save(logo=f"logo{ext}", logo_tone=_tone(data, ext), logo_version=hashlib.sha256(data).hexdigest()[:10])


def find_logo(website: str) -> bool:
    """Look for the logo on the website's home page: the image in the header's home link, any image
    named 'logo', then the site icon. Returns True if one was saved."""
    from .connectors.website import HEADERS, start_url

    url = start_url(website)
    with httpx.Client(timeout=20, follow_redirects=True, headers=HEADERS) as h:
        page = h.get(url)
        soup = BeautifulSoup(page.text, "html.parser")
        base = str(page.url)
        home = {"/", "", base, f"{urlparse(base).scheme}://{urlparse(base).netloc}/"}
        candidates = []
        for a in soup.find_all("a", href=True):
            if a["href"] in home or urljoin(base, a["href"]).rstrip("/") == base.rstrip("/"):
                candidates += [img.get("src") for img in a.find_all("img") if img.get("src")]
        for img in soup.find_all("img"):
            blob = " ".join([img.get("src") or "", img.get("alt") or "", " ".join(img.get("class") or []), img.get("id") or ""]).lower()
            if "logo" in blob and img.get("src"):
                candidates.append(img["src"])
        for rel in ("apple-touch-icon", "icon", "shortcut icon"):
            for link in soup.find_all("link", href=True):
                if rel in " ".join(link.get("rel") or []).lower():
                    candidates.append(link["href"])
        candidates.append("/favicon.ico")
        for src in dict.fromkeys(candidates):
            if src.startswith("data:"):
                continue
            try:
                r = h.get(urljoin(base, src))
            except httpx.HTTPError:
                continue
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and ctype.startswith("image/") and 0 < len(r.content) <= MAX_LOGO_BYTES:
                ext = Path(urlparse(str(r.url)).path).suffix or ("." + ctype.split("/")[1].split("+")[0])
                store_logo(r.content, ext)
                return True
    return False


def ensure_defaults() -> None:
    """First start after an update: use the connected website (if any) and fetch its logo once."""
    b = get()
    if b["website"]:
        return
    with conn() as c:
        row = c.execute("SELECT config->>'url' AS url FROM connectors WHERE type='website' ORDER BY id LIMIT 1").fetchone()
    if not row or not row["url"]:
        return
    save(website=row["url"])
    try:
        find_logo(row["url"])
    except Exception:  # noqa: BLE001 - a missing logo is not a problem
        log.info("Couldn't fetch the logo from %s", row["url"])
