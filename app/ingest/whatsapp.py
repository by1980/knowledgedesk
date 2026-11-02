"""WhatsApp chat exports (WhatsApp > chat > More > Export chat).

Handles the .txt file and the .zip export ("with media"), from both iPhone and Android, in any
date format. Voice notes inside a .zip are transcribed and placed where they were sent.
"""
import re
import tempfile
import zipfile
from collections import OrderedDict
from pathlib import Path

LINE = re.compile(
    r"^[‎‏]?\[?(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}),?\s+"
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap]\.?[Mm]\.?)?)\]?\s*(?:[-–]\s)?(.*)$"
)
VOICE = re.compile(r"([\w\-.]+\.(?:opus|m4a|mp3|ogg|aac|wav))", re.IGNORECASE)
INVISIBLE = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮﻿"), None)


def looks_like_whatsapp(text: str) -> bool:
    lines = [ln for ln in text.splitlines()[:40] if ln.strip()]
    return bool(lines) and sum(1 for ln in lines if LINE.match(ln)) >= max(2, len(lines) // 2)


def parse(text: str, transcripts: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Return one section per day: (messages, "chat <date>")."""
    transcripts = transcripts or {}
    days: OrderedDict[str, list[str]] = OrderedDict()
    current_day = None
    for raw in text.splitlines():
        m = LINE.match(raw)
        if m:
            day, time, rest = m.groups()
            current_day = day
            rest = rest.translate(INVISIBLE).strip()
            for name in VOICE.findall(rest):
                if name in transcripts:
                    rest = f"{rest} [voice note: {transcripts[name]}]"
            days.setdefault(day, []).append(f"{time} {rest}")
        elif current_day and raw.strip():
            days[current_day][-1] += "\n" + raw.translate(INVISIBLE).strip()
    return [("\n".join(msgs), f"chat {day}") for day, msgs in days.items()]


def extract_zip(path: Path) -> list[tuple[str, str]] | None:
    """If the zip is a WhatsApp export, return its sections; otherwise None."""
    with zipfile.ZipFile(path) as z:
        txts = [n for n in z.namelist() if n.lower().endswith(".txt")]
        chat_name = next((n for n in txts if "chat" in n.lower()), txts[0] if txts else None)
        if not chat_name:
            return None
        text = z.read(chat_name).decode("utf-8-sig", errors="replace")
        if not looks_like_whatsapp(text):
            return None
        transcripts = {}
        voice_files = [n for n in z.namelist() if VOICE.fullmatch(Path(n).name)]
        if voice_files:
            from .transcribe import transcribe

            with tempfile.TemporaryDirectory() as tmp:
                for name in voice_files:
                    target = Path(tmp) / Path(name).name
                    target.write_bytes(z.read(name))
                    try:
                        transcripts[target.name] = " ".join(t for t, _ in transcribe(target))
                    except Exception:  # noqa: BLE001 - a broken voice note shouldn't stop the chat import
                        continue
    return parse(text, transcripts)
