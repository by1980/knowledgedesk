"""Website connector: reads the pages of a website (company site, help center, price pages)."""
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import Sync

SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js", ".zip", ".mp4", ".mp3", ".ico", ".woff", ".woff2")


def crawl(start_url: str, max_pages: int = 200) -> dict[str, tuple[str, str]]:
    """Return {url: (title, text)} for pages on the same site as start_url."""
    start_url = start_url.strip()
    if not start_url.startswith(("http://", "https://")):
        start_url = "https://" + start_url
    site = urlparse(start_url).netloc
    queue, seen, pages = [start_url], {start_url}, {}
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "KnowledgeDesk/1.0"}) as client:
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            try:
                r = client.get(url)
            except httpx.HTTPError:
                continue
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                link = urldefrag(urljoin(str(r.url), a["href"]))[0]
                p = urlparse(link)
                if p.scheme in ("http", "https") and p.netloc == site and link not in seen \
                        and not p.path.lower().endswith(SKIP_EXT):
                    seen.add(link)
                    queue.append(link)
            title = (soup.title.string or "").strip() if soup.title and soup.title.string else url
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()
            text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
            if len(text) > 50:
                pages[url] = (title, text)
    return pages


def sync(sync: Sync) -> None:
    cfg = sync.connector["config"]
    pages = crawl(cfg["url"], int(cfg.get("max_pages") or 200))
    if not pages:
        raise RuntimeError("No pages could be read from this website")
    for url, (title, text) in pages.items():
        sync.upsert(url, f"Web: {title}", "page.txt", f"{title}\n{url}\n\n{text}".encode(), "web", {"url": url})
    sync.remove_unseen()
