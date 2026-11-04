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
- "We", "our" and "us" mean {company} itself, never the other companies in its emails. Questions about what we do, our \
services or products are answered from {company}'s own website and documents.
- If the sources do not contain the answer, say so in one or two sentences and suggest which department or person \
might know. Do not fill the answer with information about other topics or other companies.
- If sources contradict each other, point out the conflict and prefer the most recent one.
- Information from employee interviews, emails, Slack and WhatsApp chats is one person's account at one point in time; \
say who said it and when (e.g. "according to Moshe's email from March") when relying on it.
- Social media numbers (likes, views, followers) are as of the date written in the source; say that date. \
For "last week" or "latest post" questions, prefer the account summary source.
- When the user asks for one specific item ("the last proposal I sent to X", "the contract with Y"), give exactly ONE: \
the best match, and the newest one when they say last or latest. Do not list other candidates unless they ask for more.
- Attachments are separate sources; their title says which email they were attached to. When the item you give is a \
file (PDF, Word, Excel...), or an email with an attached file, put [[file:N]] alone on its own line right after the \
sentence that names it, where N is the file's source number. The reader sees the file there with Open and Download \
buttons, so don't add instructions about clicking. Use [[file:N]] only for file sources, at most once per file.
- Never say a file doesn't exist just because you don't see it; say you couldn't find it among the results.
- When asked to find something specific (an email, a proposal, a file, a message), say exactly which source it is, \
with its date and who it was from and to. Only when the user asks for several items, or for "all" of something, list \
them, newest first. Each source has a date and, for emails, from/to/cc.
- When asked to write something (a summary, a briefing, a reply or email draft, a checklist), write it in full from the \
sources, ready to use, with citations. For an email, write one line "To: name <address>" (the address from the sources), one line "Subject: ...", \
an empty line, then the email itself, without citations inside it. Cortex never sends email: the person sends it \
from their own mail app.
- Start with the direct answer in the first sentence; details after. Reply in the same language the question was \
written in. Be concise and practical: short paragraphs, numbered steps for procedures."""


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
        system=ANSWER_SYSTEM.format(company=company_name()),
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
            "about_us": {"type": "boolean", "description": "True when the question is about OUR OWN company itself: what we do, our services, "
                                                          "products, prices, team, policies, how we work. Best answered by our own website and documents."},
        },
        "required": ["query", "keywords", "people", "direction", "newest_first"],
    },
}
PLAN_SYSTEM = ("You plan searches for a company knowledge assistant. Read the user's request and fill in search_plan. "
               "Be generous with keyword synonyms and Hebrew/English translations, and with name spellings. Never answer the question.")


def company_name() -> str:
    from . import branding

    try:
        return branding.get()["name"] or settings.company_name
    except Exception:  # noqa: BLE001
        return settings.company_name


def plan_search(question: str, history: list[dict], company: str = "", website: str = "") -> dict | None:
    """Turn the question into a search plan (who, which way, when, newest first...). None = plain search."""
    if settings.fake_llm:
        return None
    try:
        c = client()
    except LLMNotConfigured:
        return None
    from datetime import date

    us = f"The user works at {company}" + (f" (website {website})" if website else "") + \
        ". 'We', 'our' and 'us' mean this company, not the companies it emails with.\n" if company else ""
    messages = list(history[-4:]) + [{"role": "user", "content": f"Today is {date.today():%Y-%m-%d}.\n{us}\nRequest: {question}"}]
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


BRIEF_TOOL = {
    "name": "attention_list",
    "description": "Only the things worth interrupting this person for, and which of the dates are worth showing.",
    "input_schema": {"type": "object", "properties": {
        "alerts": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["reply", "send", "follow_up", "prepare", "deadline"],
                     "description": "reply: a real person is waiting for their answer. send: they promised to send something soon. "
                                    "follow_up: chase someone (no answer to their proposal or question, or a payment they asked for "
                                    "with no sign it was paid). prepare: a meeting soon worth preparing. deadline: something due soon."},
            "title": {"type": "string", "description": "Short and specific, e.g. 'Reply to Dima about the website proposal'."},
            "why": {"type": "string", "description": "One plain sentence: why now."},
            "due": {"type": "string", "description": "YYYY-MM-DD if a date is known, else empty."},
            "person": {"type": "string"},
            "source_ids": {"type": "array", "items": {"type": "integer"}},
            "action": {"type": "string", "description": "The request to give the assistant, e.g. 'Draft a reply to Dima confirming the "
                                                        "price and asking when they want to start.'"}},
            "required": ["kind", "title", "why", "source_ids", "action"]}},
        "keep_dates": {"type": "array", "description": "The dates (by id) that truly need this person's attention now, each with why.",
                       "items": {"type": "object", "properties": {"id": {"type": "integer"}, "why": {"type": "string"}},
                                 "required": ["id", "why"]}}},
        "required": ["alerts", "keep_dates"]},
}
BRIEF_SYSTEM = ("You are a sharp, discreet chief of staff. You interrupt your boss only for what truly needs them. "
                "You get conversations already filtered by signals (waiting_for_my_reply, no_answer_to_my_email, "
                "payment_requested_no_sign_of_payment), upcoming meetings, and dates found in documents. Read them like a person "
                "would and keep only what matters now:\n"
                "- reply: only when a real person asked them something or needs a decision, and nobody answered. Not for 'thanks', "
                "FYI, automatic mail, invoices or receipts from suppliers, marketing, or anything already handled.\n"
                "- follow_up: a proposal or question they sent with no answer, if still relevant (a few days to a few weeks old). "
                "A payment they asked for with no sign it was paid: suggest checking with the person.\n"
                "- prepare: meetings with people outside the company, or important ones. Not routine internal meetings.\n"
                "- Dates: keep only those that need an action by them soon. Drop bills that are paid automatically (cloud services, "
                "software subscriptions, card charges), anything the conversations show is already done, proposals that are "
                "simply old, and duplicates. For each kept date, say in one sentence why it matters.\n"
                "Never invent anything: use only the ids given. When unsure, leave it out: three right items beat ten noisy ones. "
                "Write in the language of the items. Do not use dashes as punctuation.")


def briefing(name: str, company: str, addresses: list[str], items: list[dict], meetings: list[dict],
             dates: list[dict] | None = None) -> dict:
    """The 'needs your attention' list and the dates worth showing, judged by the AI from what this person may see."""
    from datetime import date

    def msg(m):
        meta = m["metadata"] or {}
        return (f"  {m['at']:%Y-%m-%d} from {meta.get('from', '')} to {meta.get('to', '')}: "
                + " ".join((m["text"] or "").split())[:500])

    def line(i):
        return (f"[id {i['id']}] signal: {i['signal']} | {i['title']} | last message {i['age']} days ago\n"
                + "\n".join(msg(m) for m in i["thread"]))

    cal = "\n".join(f"- {e['starts_at']:%Y-%m-%d %H:%M} UTC {e['title']} (with {', '.join(e['attendees'][:6]) or 'no one listed'})"
                    for e in meetings) or "(none)"
    dated = "\n".join(f"[id {d['id']}] {d['kind']} due {d['due']:%Y-%m-%d} ({d['days']} days): {d['title']}"
                      f"{' | ' + d['direction'] if d.get('direction') else ''} | from '{d['doc_title']}' ({d['doc_at']:%Y-%m-%d}): "
                      f"\"{d.get('quote') or ''}\"" for d in (dates or [])) or "(none)"
    body = (f"Today is {date.today():%Y-%m-%d}. The person is {name}" + (f" at {company}" if company else "") +
            f". Their own addresses: {', '.join(addresses) or 'unknown'}.\n\nMeetings in the next week:\n{cal}\n\n"
            f"Dates found in documents:\n{dated}\n\nConversations:\n\n" + ("\n\n".join(line(i) for i in items) or "(none)"))
    r = client().messages.create(model=settings.llm_model, max_tokens=3000, system=BRIEF_SYSTEM, tools=[BRIEF_TOOL],
                                 tool_choice={"type": "tool", "name": "attention_list"},
                                 messages=[{"role": "user", "content": body[:120000]}], timeout=120)
    for block in r.content:
        if block.type == "tool_use":
            return {"alerts": list(block.input.get("alerts") or []), "keep_dates": list(block.input.get("keep_dates") or [])}
    return {"alerts": [], "keep_dates": []}


DATES_TOOL = {
    "name": "dated_commitments",
    "description": "Dates in this item that someone will have to act on.",
    "input_schema": {"type": "object", "properties": {"items": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["renewal", "contract_end", "notice", "payment", "deadline", "promise"],
                 "description": "renewal: a contract or subscription renews (often automatically). contract_end: an agreement, "
                                "lease, licence, insurance or warranty ends. notice: the last day to cancel or give notice (work it "
                                "out: end date minus the notice period). payment: money due on a date. deadline: something must be "
                                "delivered or decided by a date. promise: someone said they would do or send something by a date."},
        "title": {"type": "string", "description": "Short and specific, with who or what, e.g. 'Meridian Clinics retainer ends' or "
                                                   "'Send Harbor Coffee the revised proposal'."},
        "due": {"type": "string", "description": "YYYY-MM-DD. Resolve relative dates ('by Thursday', 'end of next month') "
                                                 "against the item's own date."},
        "person": {"type": "string", "description": "Who it is with or who must act, if known."},
        "amount": {"type": "string", "description": "Money involved, if stated, e.g. '4,800 USD per month'."},
        "quote": {"type": "string", "description": "The exact words from the item that show this date."},
        "direction": {"type": "string", "enum": ["we_get_paid", "we_pay", "we_pay_automatically", "not_money"],
                      "description": "For payments: money owed to the reader's company, money it must pay by hand, or a bill "
                                     "charged automatically (card, direct debit, cloud or software subscription)."},
        "needs_action": {"type": "boolean", "description": "True only if someone at the reader's company must do something by "
                                                           "this date. False for automatic charges, receipts, information only."}},
        "required": ["kind", "title", "due", "quote", "direction", "needs_action"]}}}, "required": ["items"]},
}
DATES_SYSTEM = ("You read one business document or message and list the dates in it that someone at the reader's "
                "company will have to act on: contract ends, renewals, the last day to give notice, payments due, deadlines and "
                "promises with a date. Ignore dates that need no action: when a document was written or signed, past meetings, "
                "newsletters, marketing offers, shipping notices, receipts, and invoices from suppliers that are charged "
                "automatically (cloud services, software subscriptions, card payments). A proposal's 'valid until' date is not "
                "a task. Ignore anything already done. A promise needs a clear commitment "
                "and a time ('I'll send it by Thursday'). Never guess a date that isn't stated or clearly implied. "
                "If there is nothing, return an empty list. Write titles in the language of the item, without dashes as punctuation.")


def extract_dates(text: str) -> list[dict]:
    """The dated commitments in one document (fast model first, the main one if it isn't available)."""
    if settings.fake_llm:
        return []
    c = client()
    for model in dict.fromkeys([settings.planner_model, settings.llm_model]):
        try:
            r = c.messages.create(model=model, max_tokens=1500, system=DATES_SYSTEM, tools=[DATES_TOOL],
                                  tool_choice={"type": "tool", "name": "dated_commitments"},
                                  messages=[{"role": "user", "content": text}], timeout=60)
        except (anthropic.NotFoundError, anthropic.BadRequestError, anthropic.PermissionDeniedError):
            continue
        for block in r.content:
            if block.type == "tool_use":
                return list(block.input.get("items") or [])
        return []
    return []
