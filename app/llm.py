"""Claude calls: answering questions and turning interview transcripts into knowledge notes."""
from collections.abc import Iterator

import logging

import anthropic

from .config import settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"

_client: anthropic.Anthropic | None = None


class LLMNotConfigured(RuntimeError):
    pass


_key_cache: dict = {"loaded": False, "value": ""}


def api_key() -> str:
    """The Claude API key: the one saved in Settings, otherwise ANTHROPIC_API_KEY from .env."""
    if not _key_cache["loaded"]:
        from .connectors.base import decrypt
        from .db import conn

        try:
            with conn() as c:
                row = c.execute("SELECT value FROM meta WHERE key='anthropic_api_key'").fetchone()
            _key_cache["value"] = decrypt(row["value"]) if row else ""
        except Exception:  # noqa: BLE001 - fall back to .env
            _key_cache["value"] = ""
        _key_cache["loaded"] = True
    return _key_cache["value"] or settings.anthropic_api_key


def save_api_key(key: str) -> None:
    from .connectors.base import encrypt
    from .db import conn

    global _client
    with conn() as c:
        c.execute("INSERT INTO meta(key, value) VALUES ('anthropic_api_key', %s) "
                  "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (encrypt(key),))
    _key_cache.update(loaded=True, value=key)
    _client = None


def client() -> anthropic.Anthropic:
    global _client
    key = api_key()
    if not key:
        raise LLMNotConfigured("No Claude API key yet. An admin can add it in Settings.")
    if _client is None or _client.api_key != key:
        _client = anthropic.Anthropic(api_key=key)
    return _client


log = logging.getLogger("llm")

ANSWER_SYSTEM = """You are the internal knowledge assistant of {company}. Employees ask you questions \
about how the business works: processes, policies, products, customers, suppliers, prices, tools and \
"who handles what".

Rules:
- Answer ONLY from the numbered sources provided in the user's message. Do not use outside knowledge \
about this company, and do not guess names, numbers, prices or dates.
- Cite sources inline with their number in square brackets, e.g. [2]. Every factual sentence needs a citation.
- If the sources do not contain the answer, say so plainly, mention what related information you did find \
(if any), and suggest which department or person might know.
- If sources contradict each other, point out the conflict and prefer the most recent one.
- Information from employee interviews, emails, Slack and WhatsApp chats is one person's account at one point in time; \
say who said it and when (e.g. "according to Moshe's email from March") when relying on it.
- Social media numbers (likes, views, followers) are as of the date written in the source; say that date. \
For "last week" or "latest post" questions, prefer the account summary source.
- Attachments are separate sources (their title says which email they were attached to). If the user asks for a file \
(e.g. "the PDF proposal"), give that file's source number and tell them they can click it to open or download it. \
Never say a file doesn't exist just because you don't see it; say you couldn't find it among the results.
- When asked to find something specific (an email, a proposal, a file, a message), say exactly which source it is, \
with its date and who it was from and to. If several match, list them newest first. Each source has a date and, for \
emails, from/to/cc.
- Reply in the same language the question was written in. Be concise and practical: short paragraphs, \
numbered steps for procedures."""


def stream_answer(question: str, sources_block: str, history: list[dict]) -> Iterator[str]:
    """Yield answer text as it is generated."""
    messages = list(history[-6:])  # a few previous turns for follow-up questions
    from datetime import date

    today = date.today()
    messages.append({
        "role": "user",
        "content": f"<sources>\n{sources_block}\n</sources>\n\nToday is {today:%A, %d %B %Y}. "
                   f"Use this for questions like \"this week\" or \"next month\".\n\nQuestion: {question}",
    })
    if settings.fake_llm:  # quality-assurance mode: no call to Claude
        first = sources_block.split("\n", 2)[1][:200] if "\n" in sources_block else ""
        yield f"(Test answer) According to the first source [1]: {first}"
        return
    with client().beta.messages.stream(
        model=settings.llm_model,
        max_tokens=8000,
        system=ANSWER_SYSTEM.format(company=settings.company_name),
        messages=messages,
        output_config={"effort": settings.llm_effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        for text in stream.text_stream:
            yield text
        final = stream.get_final_message()
        if final.stop_reason == "refusal":
            yield "\n\n(The assistant could not answer this request.)"
        elif final.stop_reason == "max_tokens":
            yield "\n\n(Answer cut off because it was too long.)"


NOTES_PROMPT = """Below is a transcript of a knowledge-capture interview with an employee of {company}.
Interviewee: {who}

Turn it into clear, searchable knowledge notes that a new employee could use. Write in the same \
language as the transcript. Use these headings (skip any that have no content):

## Summary
## Responsibilities and role
## Processes and step-by-step procedures
## Tools, systems and where things are kept
## Customers, suppliers and contacts mentioned
## Rules, policies, numbers and deadlines
## Tips, known problems and workarounds
## Questions this interview answers
(a list of 5-15 questions an employee might ask, each with a one-line answer)

Only include what the interviewee actually said. Do not invent details.

<transcript>
{transcript}
</transcript>"""


def interview_notes(transcript: str, who: str) -> str:
    resp = client().beta.messages.create(
        model=settings.llm_model,
        max_tokens=16000,
        messages=[{
            "role": "user",
            "content": NOTES_PROMPT.format(company=settings.company_name, who=who, transcript=transcript),
        }],
        output_config={"effort": "medium"},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        return ""
    return "".join(b.text for b in resp.content if b.type == "text")


# ---------------------------------------------------------------- search planning
PLAN_TOOL = {
    "name": "search_plan",
    "description": "How to search the company's knowledge base (emails, files, chats, calendar, website, social media) for this question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The request rewritten as a complete, standalone search query (resolve follow-ups using the conversation)."},
            "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 12,
                         "description": "Words the item itself probably contains: the topic and its synonyms, in English AND Hebrew (e.g. proposal, quote, offer, הצעה, הצעת מחיר). Not the names of people or companies."},
            "people": {"type": "array", "items": {"type": "string"}, "maxItems": 8,
                       "description": "People, companies, email addresses or domains the item is from, to or about, with spelling variants (e.g. 'cutinside', 'cut inside', 'קאט אינסייד'). Empty if none."},
            "direction": {"type": "string", "enum": ["sent", "received", "any"],
                          "description": "'sent' when the user means something they or their company sent; 'received' for something sent to them."},
            "kinds": {"type": "array", "items": {"type": "string", "enum": ["email", "file", "calendar", "slack", "whatsapp", "web", "interview", "social"]},
                      "description": "Only when the user clearly limits the type (e.g. 'the email', 'in Slack'). Usually empty."},
            "after": {"type": "string", "description": "YYYY-MM-DD, only if the question gives a time frame."},
            "before": {"type": "string", "description": "YYYY-MM-DD, only if the question gives a time frame."},
            "newest_first": {"type": "boolean", "description": "True when the user wants the latest / last / most recent item."},
        },
        "required": ["query", "keywords", "people", "direction", "newest_first"],
    },
}
PLAN_SYSTEM = ("You plan searches for a company knowledge assistant. Read the user's request and fill in search_plan. "
               "Be generous with keyword synonyms and Hebrew/English translations, and with name spellings. Never answer the question.")


def plan_search(question: str, history: list[dict]) -> dict | None:
    """Turn the question into a search plan (who, which way, when, newest first...). None = plain search."""
    if settings.fake_llm:
        return None
    try:
        c = client()
    except LLMNotConfigured:
        return None
    from datetime import date

    messages = list(history[-4:]) + [{"role": "user", "content": f"Today is {date.today():%Y-%m-%d}.\n\nRequest: {question}"}]
    for model in dict.fromkeys([settings.planner_model, settings.llm_model]):
        try:
            r = c.messages.create(model=model, max_tokens=700, system=PLAN_SYSTEM, messages=messages, tools=[PLAN_TOOL],
                                  tool_choice={"type": "tool", "name": "search_plan"}, timeout=25)
        except (anthropic.NotFoundError, anthropic.BadRequestError, anthropic.PermissionDeniedError):
            continue  # this model isn't available on the account: try the main one
        except Exception:  # noqa: BLE001 - search still works without a plan
            log.warning("Search planning failed", exc_info=True)
            return None
        for block in r.content:
            if block.type == "tool_use":
                plan = dict(block.input)
                log.info("Search plan: %s", plan)
                return plan
    return None
