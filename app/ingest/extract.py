"""Turn any supported file into text sections: list of (text, location) pairs.

The location is a human-readable pointer such as "page 3", "sheet Prices" or "12:30"
so answers can cite exactly where information came from.
"""
from pathlib import Path

TEXT_EXT = {".txt", ".md", ".markdown", ".log", ".json", ".xml", ".yaml", ".yml"}
AUDIO_VIDEO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus",
                   ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
SUPPORTED_EXT = TEXT_EXT | AUDIO_VIDEO_EXT | {
    ".pdf", ".docx", ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".pptx", ".html", ".htm", ".eml", ".vtt", ".srt", ".zip",
}

Section = tuple[str, str]


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXT


def is_audio_video(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_VIDEO_EXT


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1255", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def strip_quoted_reply(text: str) -> str:
    """Drop the quoted earlier messages at the bottom of a reply (each email in a thread is indexed on its own)."""
    import re

    markers = [r"^On .{5,200} wrote:\s*$", r"^בתאריך .{5,200} מאת .*$", r"^.{0,80}כתב\(ה\):\s*$",
               r"^-{2,}\s*Original Message\s*-{2,}", r"^From: .+\nSent: ", r"^_{20,}$"]
    cut = len(text)
    for m in markers:
        found = re.search(m, text, flags=re.MULTILINE | re.IGNORECASE)
        if found and found.start() > 20:
            cut = min(cut, found.start())
    kept = text[:cut]
    kept = "\n".join(line for line in kept.splitlines() if not line.lstrip().startswith(">"))
    return kept.strip() or text.strip()


def extract(path: Path) -> list[Section]:
    ext = path.suffix.lower()

    if ext == ".txt":
        from . import whatsapp

        text = _read_text(path)
        if whatsapp.looks_like_whatsapp(text):
            return whatsapp.parse(text)
        return [(text, "")]

    if ext in TEXT_EXT or ext in {".vtt", ".srt"}:
        return [(_read_text(path), "")]

    if ext == ".zip":
        from . import whatsapp

        sections = whatsapp.extract_zip(path)
        if sections is None:
            raise ValueError("Only WhatsApp chat exports are supported inside .zip files. Unzip other archives first.")
        return sections

    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return [((page.extract_text() or ""), f"page {i}") for i, page in enumerate(reader.pages, 1)]

    if ext == ".docx":
        import docx

        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        return [("\n".join(parts), "")]

    if ext in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        return [(text, f"sheet {ws.title}") for ws in wb.worksheets
                if (text := _sheet_text(ws.iter_rows(values_only=True)))]

    if ext == ".xls":  # older Excel files, e.g. LinkedIn page analytics exports
        import xlrd

        book = xlrd.open_workbook(str(path))
        return [(text, f"sheet {sh.name}") for sh in book.sheets()
                if (text := _sheet_text(sh.get_rows(), cell=lambda c: c.value))]

    if ext in {".csv", ".tsv"}:
        import csv
        import io

        text = _read_text(path)
        reader = csv.reader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else ",")
        rows = list(reader)
        if not rows:
            return []
        header, lines = rows[0], [" | ".join(rows[0])]
        for r in rows[1:]:
            lines.append("; ".join(f"{h}: {v}" for h, v in zip(header, r) if v))
        return [("\n".join(lines), "")]

    if ext == ".pptx":
        from pptx import Presentation

        prs = Presentation(str(path))
        sections = []
        for i, slide in enumerate(prs.slides, 1):
            texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    texts.append(f"Notes: {notes}")
            if texts:
                sections.append(("\n".join(texts), f"slide {i}"))
        return sections

    if ext in {".html", ".htm"}:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(_read_text(path), "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return [(soup.get_text("\n"), "")]

    if ext == ".eml":
        from email import policy
        from email.parser import BytesParser

        msg = BytesParser(policy=policy.default).parse(path.open("rb"))
        body = msg.get_body(preferencelist=("plain", "html"))
        content = body.get_content() if body else ""
        if body is not None and body.get_content_type() == "text/html":
            from bs4 import BeautifulSoup

            content = BeautifulSoup(content, "html.parser").get_text("\n")
        files = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
        head = (f"From: {msg['from']}\nTo: {msg['to']}\n" + (f"Cc: {msg['cc']}\n" if msg["cc"] else "")
                + f"Date: {msg['date']}\nSubject: {msg['subject']}\n"
                + (f"Attachments: {', '.join(files)}\n" if files else "") + "\n")
        return [(head + strip_quoted_reply(content), "")]

    if ext in AUDIO_VIDEO_EXT:
        from .transcribe import transcribe

        return transcribe(path)

    raise ValueError(f"Unsupported file type: {ext}")


def _sheet_text(rows, cell=lambda v: v) -> str:
    """Spreadsheet rows as text. Rows become "Column: value; ..." so meaning survives chunking.
    The header is the first row with at least 3 filled cells (exports often start with a title line)."""
    table = [["" if (v := cell(x)) is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
              for x in row] for row in rows]
    table = [values for values in table if any(values)]
    need = min(3, max((sum(1 for v in values if v) for values in table), default=0))
    out, header = [], None
    for values in table:
        if header is None:
            if sum(1 for v in values if v) >= need:
                header = values
            out.append(" | ".join(v for v in values if v))
        else:
            pairs = [f"{h}: {v}" for h, v in zip(header, values) if v]
            out.append("; ".join(pairs))
    return "\n".join(out)
