"""Social media connectors: Facebook Pages + Instagram business accounts (one "Connect with Facebook"),
and YouTube channels (through the Google sign-in).

For each post or video we keep one document with its text, link, date, likes, comments and a few
comments people wrote. For each account we also keep a summary document, rewritten on every check,
that answers the usual questions directly: the latest post, totals for the last 7 and 30 days, the
best post of the month, week-by-week numbers, and follower changes. Nothing is ever posted.
"""
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx

from ..oauth import Api, ApiError, access_token, http
from .base import Sync

GRAPH = "https://graph.facebook.com/v23.0"
YOUTUBE = "https://www.googleapis.com/youtube/v3"
MAX_POSTS = 500           # per account: the most recent ones
REFRESH_DAYS = 90         # posts younger than this get their numbers refreshed on every check
CHECK_EVERY_MINUTES = 60  # numbers don't need to be fresher than this
HISTORY_DAYS = 120        # daily snapshots kept for "followers gained this week" style questions


class SocialError(RuntimeError):
    pass


def _api_off(text: str) -> bool:
    low = text.lower()
    return "has not been used in project" in low or "is disabled" in low or "service_disabled" in low or "accessnotconfigured" in low


def _due(sync: Sync) -> bool:
    """Scheduled checks run at most hourly; "Check for new items" always runs."""
    last = sync.connector["state"].get("checked_at", 0)
    return not sync.scheduled or time.time() - last >= CHECK_EVERY_MINUTES * 60


def _when(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    if len(value) >= 5 and value[-5] in "+-" and value[-3] != ":":  # Graph sends +0000
        value = value[:-2] + ":" + value[-2:]
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _n(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fmt(n: int) -> str:
    return f"{n:,}"


# ------------------------------------------------------------------ Facebook Graph

class Graph:
    """Facebook Graph calls with a given token (the user's token, or a Page's own token)."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def get(self, path_or_url: str, token: str, **params) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{GRAPH}/{path_or_url}"
        for attempt in range(4):
            r = self.client.get(url, params=params or None, headers={"Authorization": f"Bearer {token}"})
            try:
                body = r.json()
            except ValueError:
                body = {}
            err = body.get("error") if isinstance(body, dict) else None
            if r.status_code < 400 and not err:
                return body
            code = (err or {}).get("code")
            if code in (4, 17, 32, 613) and attempt < 3:  # rate limits: wait and try again
                time.sleep(20 * (attempt + 1) * Api.rate_wait_scale)
                continue
            if code == 190 or r.status_code == 401:
                raise SocialError("Facebook access expired or was removed (Facebook asks to sign in again every 60 days). "
                                  "Click Connect with Facebook again.")
            if code == 10 or code == 200:
                raise SocialError("Facebook didn't give permission to read this. Click Connect with Facebook again and "
                                  "approve every Page and Instagram account.")
            raise SocialError(f"Facebook error: {(err or {}).get('message') or r.status_code}")
        raise SocialError("Facebook is busy right now, will retry at the next check")

    def pages(self, path: str, token: str, limit: int, **params):
        """Follow 'next' links until `limit` items were returned."""
        body = self.get(path, token, limit=min(100, limit), **params)
        n = 0
        while True:
            for item in body.get("data", []):
                yield item
                n += 1
                if n >= limit:
                    return
            nxt = (body.get("paging") or {}).get("next")
            if not nxt:
                return
            body = self.get(nxt, token)


def _comments_text(items: list[dict], who_key: str, text_key: str) -> str:
    lines = []
    for c in items[:5]:
        who = c.get(who_key)
        who = who.get("name") if isinstance(who, dict) else who
        text = (c.get(text_key) or "").strip()
        if text:
            lines.append(f"- {who or 'Someone'}: {text}")
    return "\n".join(lines)


def sync_meta_social(sync: Sync) -> None:
    if not _due(sync):
        return
    state = sync.connector["state"]
    token = access_token(int(sync.connector["config"]["account_id"]))
    with http() as client:
        g = Graph(client)
        pages = list(g.pages("me/accounts", token, 100, fields="id,name,access_token,link,fan_count,followers_count,"
                             "instagram_business_account{id,username,name,followers_count,media_count}"))
        if not pages:
            raise SocialError("No Facebook Pages were shared. Click Connect with Facebook again and select your "
                              "business Page (and its Instagram account) when Facebook asks.")
        accounts = []
        for page in pages:
            accounts.append(_facebook_page(sync, g, page))
            ig = page.get("instagram_business_account")
            if ig:
                accounts.append(_instagram(sync, g, ig, page.get("access_token") or token))
    state["accounts"] = [a for a in accounts if a]
    state["checked_at"] = time.time()
    state.pop("progress", None)


def _facebook_page(sync: Sync, g: Graph, page: dict) -> dict:
    ptoken = page.get("access_token") or ""
    posts = []
    fields = ("id,message,story,created_time,permalink_url,shares,"
              "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(5){message,from,created_time}")
    for p in g.pages(f"{page['id']}/posts", ptoken, MAX_POSTS, fields=fields):
        posts.append({
            "id": p["id"], "when": _when(p["created_time"]), "text": p.get("message") or p.get("story") or "",
            "link": p.get("permalink_url", ""), "likes": _n((p.get("reactions") or {}).get("summary", {}).get("total_count")),
            "comments": _n((p.get("comments") or {}).get("summary", {}).get("total_count")),
            "shares": _n((p.get("shares") or {}).get("count")),
            "top_comments": _comments_text((p.get("comments") or {}).get("data", []), "from", "message"),
        })
    account = {"platform": "Facebook", "key": f"fb:{page['id']}", "name": page.get("name", "Facebook Page"),
               "handle": page.get("name", ""), "link": page.get("link", ""),
               "followers": _n(page.get("followers_count") or page.get("fan_count")), "likes_word": "reactions"}
    _save_account(sync, account, posts, "post")
    return _public(account, posts)


def _instagram(sync: Sync, g: Graph, ig: dict, token: str) -> dict:
    posts = []
    fields = "id,caption,media_type,timestamp,permalink,like_count,comments_count,comments.limit(5){text,username,timestamp}"
    for p in g.pages(f"{ig['id']}/media", token, MAX_POSTS, fields=fields):
        kind = {"VIDEO": "video", "CAROUSEL_ALBUM": "carousel", "IMAGE": "photo"}.get(p.get("media_type", ""), "post")
        posts.append({
            "id": p["id"], "when": _when(p["timestamp"]), "text": p.get("caption") or "", "link": p.get("permalink", ""),
            "likes": _n(p.get("like_count")), "comments": _n(p.get("comments_count")), "shares": None, "type": kind,
            "top_comments": _comments_text((p.get("comments") or {}).get("data", []), "username", "text"),
        })
    handle = "@" + ig["username"] if ig.get("username") else ig.get("name", "Instagram")
    account = {"platform": "Instagram", "key": f"ig:{ig['id']}", "name": ig.get("name") or handle, "handle": handle,
               "link": f"https://www.instagram.com/{ig['username']}/" if ig.get("username") else "",
               "followers": _n(ig.get("followers_count")), "likes_word": "likes"}
    _save_account(sync, account, posts, "post")
    return _public(account, posts)


# ------------------------------------------------------------------ YouTube

def sync_youtube(sync: Sync) -> None:
    if not _due(sync):
        return
    state = sync.connector["state"]
    accounts = []
    with Api(int(sync.connector["config"]["account_id"])) as api:
        try:
            channels = api.json(f"{YOUTUBE}/channels", {"part": "snippet,statistics,contentDetails", "mine": "true"}).get("items", [])
        except ApiError as e:
            if _api_off(str(e)):
                raise  # explained with a link by friendly_error
            if e.status in (401, 403):
                raise SocialError("Google didn't give permission to read YouTube. Click Connect with Google again and "
                                  "tick YouTube.")
            raise
        if not channels:
            raise SocialError("This Google account has no YouTube channel. Click Connect YouTube channel again and, on "
                              "Google's page, choose the account or brand account that owns the channel.")
        for ch in channels:
            uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            ids: list[str] = []
            token = None
            while uploads and len(ids) < MAX_POSTS:
                body = api.json(f"{YOUTUBE}/playlistItems", {"part": "contentDetails", "playlistId": uploads,
                                                             "maxResults": 50, **({"pageToken": token} if token else {})})
                ids += [i["contentDetails"]["videoId"] for i in body.get("items", [])]
                token = body.get("nextPageToken")
                if not token:
                    break
            videos = []
            for start in range(0, len(ids), 50):
                body = api.json(f"{YOUTUBE}/videos", {"part": "snippet,statistics", "id": ",".join(ids[start:start + 50])})
                for v in body.get("items", []):
                    s, st = v.get("snippet", {}), v.get("statistics", {})
                    videos.append({"id": v["id"], "when": _when(s.get("publishedAt")), "title": s.get("title", ""),
                                   "text": s.get("description", ""), "link": f"https://www.youtube.com/watch?v={v['id']}",
                                   "likes": _n(st.get("likeCount")), "comments": _n(st.get("commentCount")),
                                   "views": _n(st.get("viewCount")), "shares": None, "top_comments": ""})
            cutoff = datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)
            for v in videos:
                if v["when"] < cutoff or not v["comments"]:
                    continue
                try:
                    body = api.json(f"{YOUTUBE}/commentThreads", {"part": "snippet", "videoId": v["id"], "maxResults": 5,
                                                                  "order": "relevance", "textFormat": "plainText"})
                except ApiError:
                    continue  # comments turned off for this video
                items = [t["snippet"]["topLevelComment"]["snippet"] for t in body.get("items", [])]
                v["top_comments"] = _comments_text(items, "authorDisplayName", "textDisplay")
            sn, st = ch.get("snippet", {}), ch.get("statistics", {})
            account = {"platform": "YouTube", "key": f"yt:{ch['id']}", "name": sn.get("title", "YouTube channel"),
                       "handle": sn.get("customUrl") or sn.get("title", ""),
                       "link": f"https://www.youtube.com/channel/{ch['id']}",
                       "followers": _n(st.get("subscriberCount")), "likes_word": "likes",
                       "total_views": _n(st.get("viewCount"))}
            _save_account(sync, account, videos, "video")
            accounts.append(_public(account, videos))
    state["accounts"] = accounts
    state["checked_at"] = time.time()


# ------------------------------------------------------------------ documents

def _public(account: dict, posts: list[dict]) -> dict:
    """What the Sources page shows for one account."""
    last = max(posts, key=lambda p: p["when"]) if posts else None
    week = datetime.now(timezone.utc) - timedelta(days=7)
    recent = [p for p in posts if p["when"] >= week]
    return {"platform": account["platform"], "name": account["name"], "handle": account["handle"], "link": account["link"],
            "followers": account["followers"], "posts": len(posts), "last_post": last["when"].isoformat() if last else None,
            "likes_week": sum(p["likes"] for p in recent), "posts_week": len(recent)}


def _post_line(p: dict, likes_word: str) -> str:
    bits = [f"{_fmt(p['likes'])} {likes_word}", f"{_fmt(p['comments'])} comments"]
    if p.get("shares") is not None:
        bits.append(f"{_fmt(p['shares'])} shares")
    if p.get("views") is not None:
        bits.insert(0, f"{_fmt(p['views'])} views")
    return ", ".join(bits)


def _snippet(p: dict, size: int = 140) -> str:
    text = (p.get("title") or p.get("text") or "").replace("\n", " ").strip()
    return (text[:size] + "…") if len(text) > size else (text or "(no text)")


def _save_account(sync: Sync, account: dict, posts: list[dict], noun: str) -> None:
    platform, name, likes_word = account["platform"], account["handle"] or account["name"], account["likes_word"]
    known = sync.imported_ids()
    cutoff = datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)
    posts.sort(key=lambda p: p["when"], reverse=True)
    for n, p in enumerate(posts):
        ext = f"{account['key']}:{p['id']}"
        if ext in known and p["when"] < cutoff:
            sync.seen.add(ext)  # older posts: numbers barely move, keep what we have
            continue
        day = p["when"].strftime("%Y-%m-%d")
        lines = [f"{platform} {noun} by {name} – {p['when']:%A %d %B %Y, %H:%M} UTC"]
        if p.get("title"):
            lines.append(f"Title: {p['title']}")
        if p.get("type"):
            lines.append(f"Type: {p['type']}")
        lines.append(f"Numbers (as of {date.today():%d %B %Y}): {_post_line(p, likes_word)}")
        if p.get("link"):
            lines.append(f"Link: {p['link']}")
        lines.append("")
        lines.append(p.get("text") or "(no text)")
        if p.get("top_comments"):
            lines += ["", "Comments:", p["top_comments"]]
        sync.upsert(ext, f"{platform} {noun} – {name} – {day}: {_snippet(p, 60)}", f"{platform.lower()}-{day}.txt",
                    "\n".join(lines).encode(), "social",
                    {"platform": platform, "account": name, "posted": p["when"].isoformat(), "likes": p["likes"],
                     "comments": p["comments"], "views": p.get("views"), "link": p.get("link", "")})
        if n % 50 == 0:
            sync.progress(n, len(posts))
    _save_summary(sync, account, posts, noun)


def _save_summary(sync: Sync, account: dict, posts: list[dict], noun: str) -> None:
    """One document per account that answers "how did we do last week?" style questions directly."""
    platform, name, likes_word = account["platform"], account["handle"] or account["name"], account["likes_word"]
    now = datetime.now(timezone.utc)
    today = now.date()
    history = sync.connector["state"].setdefault("history", {}).setdefault(account["key"], {})
    history[today.isoformat()] = {"followers": account["followers"], "likes": sum(p["likes"] for p in posts),
                                  "comments": sum(p["comments"] for p in posts)}
    for day in [d for d in history if d < (today - timedelta(days=HISTORY_DAYS)).isoformat()]:
        del history[day]

    def window(days: int) -> list[dict]:
        return [p for p in posts if p["when"] >= now - timedelta(days=days)]

    def totals(items: list[dict]) -> str:
        if not items:
            return f"no {noun}s published"
        text = f"{len(items)} {noun}{'s' if len(items) != 1 else ''} published, {_fmt(sum(p['likes'] for p in items))} {likes_word}, " \
               f"{_fmt(sum(p['comments'] for p in items))} comments"
        if any(p.get("views") is not None for p in items):
            text += f", {_fmt(sum(p.get('views') or 0 for p in items))} views"
        return text

    def change(days: int, key: str) -> str | None:
        past = (today - timedelta(days=days)).isoformat()
        earlier = [d for d in history if d <= past]
        if not earlier:
            return None
        diff = history[today.isoformat()][key] - history[max(earlier)][key]
        return f"{'+' if diff >= 0 else ''}{_fmt(diff)}"

    lines = [f"{platform} account summary: {account['name']}" + (f" ({name})" if name != account["name"] else ""),
             f"Updated {now:%A %d %B %Y, %H:%M} UTC. Numbers are as of this time.", ""]
    if account.get("link"):
        lines.append(f"Profile: {account['link']}")
    follower_word = "Subscribers" if platform == "YouTube" else "Followers"
    lines.append(f"{follower_word}: {_fmt(account['followers'])}")
    if account.get("total_views"):
        lines.append(f"Total channel views: {_fmt(account['total_views'])}")
    lines.append(f"{noun.capitalize()}s we know about: {len(posts)}")
    for days, label in ((7, "last 7 days"), (30, "last 30 days")):
        for key, word in (("followers", follower_word.lower()), ("likes", f"{likes_word} received on all {noun}s")):
            c = change(days, key)
            if c is not None:
                lines.append(f"Change in {word}, {label}: {c}")
    lines.append("")
    if posts:
        last = posts[0]
        lines += [f"Latest {noun}: {last['when']:%A %d %B %Y, %H:%M} UTC – {_snippet(last, 300)}",
                  f"  {_post_line(last, likes_word)}" + (f" – {last['link']}" if last.get("link") else ""), ""]
    # Calendar weeks too, so "last week" means Monday–Sunday as people usually mean it.
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_week = [p for p in posts if last_monday <= p["when"].date() < this_monday]
    this_week = [p for p in posts if p["when"].date() >= this_monday]
    lines += [f"This week (since Monday {this_monday:%d %B}): {totals(this_week)}",
              f"Last week (Monday {last_monday:%d %B} – Sunday {this_monday - timedelta(days=1):%d %B}): {totals(last_week)}",
              f"Last 7 days: {totals(window(7))}", f"Last 30 days: {totals(window(30))}",
              f"Last 365 days: {totals(window(365))}", ""]
    month = window(30)
    if month:
        best = max(month, key=lambda p: p["likes"] + p["comments"])
        lines += [f"Best {noun} of the last 30 days ({_post_line(best, likes_word)}): {best['when']:%d %B %Y} – {_snippet(best, 200)}"
                  + (f" – {best['link']}" if best.get("link") else ""), ""]
    weeks: dict[date, list[dict]] = defaultdict(list)
    for p in posts:
        d = p["when"].date()
        weeks[d - timedelta(days=d.weekday())].append(p)
    if weeks:
        lines.append(f"Week by week ({noun}s published that week and their {likes_word}/comments so far):")
        for monday in sorted(weeks, reverse=True)[:12]:
            lines.append(f"- Week of {monday:%d %B %Y}: {totals(weeks[monday])}")
        lines.append("")
    if posts:
        lines.append(f"Most recent {noun}s:")
        for p in posts[:15]:
            lines.append(f"- {p['when']:%d %b %Y}: {_snippet(p, 100)} ({_post_line(p, likes_word)})")
    sync.upsert(f"{account['key']}:summary", f"{platform} summary – {account['name']}", f"{platform.lower()}-summary.txt",
                "\n".join(lines).encode(), "social", {"platform": platform, "account": name, "summary": True})
