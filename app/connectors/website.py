"""Website connector: reads the pages of a website (company site, help center, price pages)."""
import re
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from .base import Sync

SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js", ".zip", ".mp4", ".mp3", ".ico",
            ".woff", ".woff2", ".pdf", ".xml", ".json", ".avif")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36 KnowledgeDesk",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he,en;q=0.8",
}


class WebsiteError(RuntimeError):
    pass


def _site(host: str) -> str:
    host = host.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def normalize(url: str) -> str:
    """One spelling per page: no #fragment, no tracking parameters, no trailing slash."""
    url = urldefrag(url)[0]
    p = urlparse(url)
    query = "&".join(q for q in p.query.split("&") if q and not q.lower().startswith(("utm_", "fbclid", "gclid")))
    path = p.path.rstrip("/") or ""
    return urlunparse((p.scheme, p.netloc.lower(), path, "", query, ""))


def start_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise WebsiteError("Please enter the website address")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not urlparse(url).netloc or "." not in urlparse(url).netloc:
        raise WebsiteError("That doesn't look like a website address (example: www.yourcompany.com)")
    return url


def _explain(e: Exception, url: str) -> str:
    host = urlparse(url).netloc
    text = str(e)
    if isinstance(e, httpx.ConnectError) and ("Name or service not known" in text or "nodename" in text
                                               or "getaddrinfo" in text or "resolve" in text.lower()):
        return f"The address {host} doesn't exist. Check the spelling."
    if isinstance(e, httpx.TimeoutException):
        return f"{host} took too long to answer."
    if isinstance(e, httpx.ConnectError):
        return f"Could not connect to {host} ({text[:120]})."
    return f"{host}: {text[:150]}"


def _status_reason(code: int, host: str) -> str:
    if code in (401, 403):
        return f"{host} refused to let us read it (error {code}). It may block automated readers."
    if code == 404:
        return f"That page doesn't exist on {host} (error 404). Check the address."
    if code >= 500:
        return f"{host} had a server error ({code}). Try again later."
    return f"{host} answered with error {code}."


def _page_text(html: str) -> tuple[str, str, BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    links = BeautifulSoup(html, "html.parser")  # keep a copy with navigation for link discovery
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    return title, "\n".join(line for line in lines if line), links


def crawl(url: str, max_pages: int = 200, client: httpx.Client | None = None) -> dict[str, tuple[str, str]]:
    """Return {url: (title, text)} for pages of the website. Raises WebsiteError with a clear reason."""
    first = start_url(url)
    own = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True, headers=HEADERS)
    pages: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    try:
        site = _site(urlparse(first).netloc)
        queue, seen = [first], {normalize(first)}

        def add_link(link: str) -> None:
            link = urldefrag(link)[0]
            p = urlparse(link)
            key = normalize(link)
            if (p.scheme in ("http", "https") and _site(p.netloc) == site and key not in seen
                    and not p.path.lower().endswith(SKIP_EXT)):
                seen.add(key)
                queue.append(link)

        sitemap_checked = False
        while queue and len(pages) < max_pages:
            current = queue.pop(0)
            try:
                r = client.get(current)
            except httpx.HTTPError as e:
                problems.append(_explain(e, current))
                continue
            if r.status_code != 200:
                problems.append(_status_reason(r.status_code, urlparse(current).netloc))
                continue
            final = str(r.url)
            site = _site(urlparse(final).netloc) if not pages else site  # follow a redirect to www. / another domain
            body = r.text
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and not body.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
                continue
            title, text, soup = _page_text(body)
            for a in soup.find_all("a", href=True):
                add_link(urljoin(final, a["href"]))
            key = normalize(final)
            if len(text) >= 20 and key not in pages:
                pages[key] = (title or key, text)
            if not sitemap_checked:  # also use the site's own list of pages, if it has one
                sitemap_checked = True
                try:
                    sm = client.get(urljoin(final, "/sitemap.xml"))
                    if sm.status_code == 200:
                        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm.text)[:1000]:
                            add_link(loc)
                except httpx.HTTPError:
                    pass
        if not pages:
            if problems:
                raise WebsiteError(problems[0])
            raise WebsiteError("The website answered, but its pages have no readable text. It may build its "
                               "pages with JavaScript only, which can't be read yet.")
        return pages
    finally:
        if own:
            client.close()


def check(url: str) -> str:
    """Quick check when the admin adds a website: is the first page reachable and readable?"""
    pages = crawl(url, max_pages=1)
    return next(iter(pages.values()))[0]


def sync(sync: Sync) -> None:
    cfg = sync.connector["config"]
    pages = crawl(cfg["url"], int(cfg.get("max_pages") or 200))
    for url, (title, text) in pages.items():
        sync.upsert(url, f"Web: {title}", "page.txt", f"{title}\n{url}\n\n{text}".encode(), "web", {"url": url})
    sync.remove_unseen()
