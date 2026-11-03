"""Slack connector (connected with "Connect Slack workspace").

Reads public channels (joining them automatically) and private channels the Cortex app was
added to. Messages are grouped into one document per channel per day, with people's names and
thread replies, so answers can say "in #sales on 3 March, Dana wrote…". Nothing is ever posted.
"""
import re
from collections import defaultdict
from datetime import datetime, timezone

from ..oauth import Api, ApiError
from .base import Sync

SLACK = "https://slack.com/api"
MAX_REPLY_THREADS = 300  # per channel per round; replies are fetched for threads with answers


class SlackError(RuntimeError):
    pass


def _call(api: Api, method: str, **data) -> dict:
    body = api.post(f"{SLACK}/{method}", {k: v for k, v in data.items() if v is not None})
    if not body.get("ok"):
        err = body.get("error", "unknown_error")
        if err in ("invalid_auth", "token_revoked", "account_inactive", "not_authed"):
            raise SlackError("Access was revoked: the Slack app was removed from the workspace.")
        raise SlackError(f"Slack error: {err}")
    return body


def _pages(api: Api, method: str, key: str, **data):
    cursor = None
    while True:
        body = _call(api, method, cursor=cursor, limit=200, **data)
        yield from body.get(key, [])
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


def _text(message: dict, names: dict[str, str]) -> str:
    text = message.get("text") or ""
    text = re.sub(r"<@([A-Z0-9]+)>", lambda m: "@" + names.get(m.group(1), m.group(1)), text)
    text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    files = [f.get("name") or f.get("title") for f in message.get("files", []) if f.get("name") or f.get("title")]
    if files:
        text += " [shared: " + ", ".join(files) + "]"
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def _who(message: dict, names: dict[str, str]) -> str:
    return names.get(message.get("user", ""), "") or message.get("username") or (message.get("bot_profile") or {}).get("name") or "Someone"


def sync_slack(sync: Sync) -> None:
    state = sync.connector["state"]
    seen_until: dict = state.setdefault("channels", {})
    with Api(int(sync.connector["config"]["account_id"])) as api:
        people = list(_pages(api, "users.list", "members"))
        names = {u["id"]: (u.get("profile") or {}).get("real_name") or u.get("real_name") or u.get("name", "") for u in people}
        emails = {u["id"]: ((u.get("profile") or {}).get("email") or "").lower() for u in people}
        owner = sync.c.execute("SELECT email FROM users WHERE id=%s", (sync.connector.get("owner_id"),)).fetchone()
        owner_email = (owner["email"] if owner else "").lower()
        channels = [c for c in _pages(api, "conversations.list", "channels",
                                      types="public_channel,private_channel", exclude_archived="true")]
        readable = []
        for ch in channels:
            if not ch.get("is_member") and not ch.get("is_private"):
                try:
                    _call(api, "conversations.join", channel=ch["id"])  # public channels: join to read them
                except (SlackError, ApiError):
                    continue
            if ch.get("is_member") or not ch.get("is_private"):
                readable.append(ch)
        state["channel_count"] = len(readable)
        state["private_not_added"] = sum(1 for ch in channels if ch.get("is_private") and not ch.get("is_member"))

        for n, ch in enumerate(readable, 1):
            sync.progress(n - 1, len(readable), commit=n == 1)
            last = float(seen_until.get(ch["id"], 0))
            # Re-read from the start of the last day already imported, so that day's document is complete.
            oldest = datetime.fromtimestamp(last, timezone.utc).replace(hour=0, minute=0, second=0).timestamp() if last else 0
            messages = list(_pages(api, "conversations.history", "messages", channel=ch["id"], oldest=str(oldest) if oldest else None))
            threads = [m for m in messages if m.get("reply_count")][:MAX_REPLY_THREADS]
            replies: dict[str, list] = {}
            for m in threads:
                try:
                    rs = list(_pages(api, "conversations.replies", "messages", channel=ch["id"], ts=m["ts"]))
                    replies[m["ts"]] = [r for r in rs if r.get("ts") != m["ts"]]
                except (SlackError, ApiError):
                    continue
            # A private channel: only its members may find what was said there (matched by email address)
            allowed = None
            if ch.get("is_private"):
                try:
                    members = list(_pages(api, "conversations.members", "members", channel=ch["id"]))
                except (SlackError, ApiError):
                    members = []
                allowed = sorted({emails.get(m, "") for m in members} - {""} | ({owner_email} if owner_email else set()))
            days: dict[str, list[tuple[float, str]]] = defaultdict(list)
            for m in messages:
                if m.get("subtype") in ("channel_join", "channel_leave", "bot_add", "channel_topic", "channel_purpose"):
                    continue
                ts = float(m["ts"])
                when = datetime.fromtimestamp(ts, timezone.utc)
                line = f"{when:%H:%M} {_who(m, names)}: {_text(m, names)}"
                for r in replies.get(m["ts"], []):
                    line += f"\n    ↳ {_who(r, names)}: {_text(r, names)}"
                days[when.strftime("%Y-%m-%d")].append((ts, line))
            for day, lines in days.items():
                lines.sort()
                body = f"Slack channel #{ch.get('name', '')} – {day}\n\n" + "\n".join(line for _, line in lines)
                sync.upsert(f"slack:{ch['id']}:{day}", f"Slack #{ch.get('name', '')} – {day}", f"slack-{day}.txt",
                            body.encode(), "slack", {"channel": ch.get("name", ""), "day": day, "messages": len(lines),
                                                     "private_channel": bool(ch.get("is_private"))}, allowed_emails=allowed)
            if messages:
                seen_until[ch["id"]] = max(float(m["ts"]) for m in messages)
        sync.connector["state"].pop("progress", None)
