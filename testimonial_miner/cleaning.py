"""Turn a raw email into clean sender-written text and numbered sentences.

Everything here is deterministic code: MIME decoding, HTML to text, stripping quoted
replies and signatures, and sentence splitting. The model only ever sees the result.
"""

from __future__ import annotations

import re
from email.message import Message
from html.parser import HTMLParser

BLOCK_TAGS = {
    "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table",
    "ul", "ol", "pre", "section", "article", "header", "footer", "td", "th",
}
VOID_TAGS = {"br", "hr", "img", "meta", "link", "input", "area", "base", "col", "embed",
             "source", "track", "wbr"}
SKIP_TAGS = {"script", "style", "head", "title", "blockquote"}
QUOTE_CLASS_HINTS = ("gmail_quote", "yahoo_quoted", "moz-cite-prefix", "protonmail_quote",
                     "OutlookMessageHeader", "gmail_attr")
QUOTE_ID_HINTS = ("divRplyFwdMsg", "appendonsend", "x_divRplyFwdMsg", "isForwardContent")


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            if tag in ("br", "hr") and not self._skip:
                self.parts.append("\n")
            return
        a = dict(attrs)
        cls, id_ = a.get("class") or "", a.get("id") or ""
        skip = (tag in SKIP_TAGS
                or any(h in cls for h in QUOTE_CLASS_HINTS)
                or any(h in id_ for h in QUOTE_ID_HINTS))
        self._stack.append((tag, skip))
        if skip:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if not any(t == tag for t, _ in self._stack):
            return  # stray end tag
        while self._stack:
            t, skip = self._stack.pop()
            if skip:
                self._skip -= 1
            if t == tag:
                break
        if tag in BLOCK_TAGS and not self._skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML falls back to tag stripping
        return re.sub(r"<[^>]+>", " ", html)
    return "".join(parser.parts)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "latin-1"):
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", "replace")


def message_to_text(msg: Message) -> str:
    """Prefer the text/plain body; otherwise convert text/html. Attachments are ignored."""
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_decode_part(part))
        elif ctype == "text/html":
            html.append(_decode_part(part))
    if any(p.strip() for p in plain):
        return "\n".join(plain)
    if html:
        return html_to_text("\n".join(html))
    return ""


# --- quoted replies, forwards, signatures -------------------------------------------------

_QUOTE_LINE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^On .{0,300}wrote:\s*$",
        r"^-{2,}\s*Original Message\s*-{2,}\s*$",
        r"^-{2,}\s*Forwarded message\s*-{2,}\s*$",
        r"^Begin forwarded message:\s*$",
        r"^_{5,}\s*$",
        r"^Le .{0,200} a écrit\s*:\s*$",
        r"^Am .{0,200} schrieb .{0,100}:\s*$",
        r"^El .{0,200} escribió:\s*$",
        r"^Il .{0,200} ha scritto:\s*$",
        r"^Op .{0,200} schreef .{0,100}:\s*$",
        r"^Den .{0,200} skrev .{0,100}:\s*$",
        r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}.{0,120}<[^>]+@[^>]+>\s*:?\s*$",
        r"^בתאריך .{0,200}כתב/ה:\s*$",
    )
]
_OUTLOOK_HEADER = re.compile(r"^(From|Von|De):\s", re.IGNORECASE)
_OUTLOOK_FOLLOW = re.compile(r"^(Sent|To|Subject|Date|Gesendet|An|Envoyé|À):\s", re.IGNORECASE)
_SIG_DELIM = re.compile(r"^-- ?$")
_DEVICE_SIG = re.compile(r"^(Sent from my|Sent via|Get Outlook for|Envoyé de mon|Von meinem)",
                         re.IGNORECASE)
_CLOSING = re.compile(
    r"^(best|kind|warm|warmest)?\s*(regards|wishes)[,.!]?$"
    r"|^(thanks|thank you|thanks again|many thanks|thx|cheers|sincerely|best|yours|take care|"
    r"all the best|kind regards|with kind regards)[,.!]?$"
    r"|^(viele grüße|liebe grüße|mit freundlichen grüßen|beste grüße|grüße|lg|mfg)[,.!]?$"
    r"|^(cordialement|bien à vous|amicalement|merci)[,.!]?$"
    r"|^(saludos|un saludo|gracias|muchas gracias)[,.!]?$"
    r"|^(met vriendelijke groet(en)?|groeten|bedankt)[,.!]?$"
    r"|^(grazie|cordiali saluti|saluti)[,.!]?$",
    re.IGNORECASE,
)


def strip_quoted_and_signature(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Re-join an "On <date>\n<name> wrote:" header that wrapped onto two lines.
    text = re.sub(r"\n(On [^\n]{0,200})\n([^\n]{0,160}wrote:)", r"\n\1 \2", text)
    lines = text.split("\n")
    kept: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(">"):
            continue
        if any(p.match(s) for p in _QUOTE_LINE_PATTERNS):
            break
        if _OUTLOOK_HEADER.match(s) and any(
            _OUTLOOK_FOLLOW.match(lines[j].strip()) for j in range(i + 1, min(i + 4, len(lines)))
        ):
            break
        if _SIG_DELIM.match(line.rstrip()):
            break
        if _DEVICE_SIG.match(s):
            continue
        if _CLOSING.match(s) and any(k.strip() for k in kept):
            break
        kept.append(line)
    return "\n".join(kept).strip()


# --- sentences -----------------------------------------------------------------------------

# Split after . ! ? when the next unit starts like a sentence. An ellipsis only ends a
# sentence when a capital letter follows ("really … cool!" stays whole).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Þ0-9\"“„«(\[])|(?<=…)\s+(?=[A-ZÀ-Þ])")
_URL = re.compile(r"https?://\S+")


def split_sentences(text: str, max_units: int, max_chars: int) -> tuple[list[str], bool]:
    """Split cleaned text into sentence-sized units. Returns (units, truncated)."""
    truncated = len(text) > max_chars
    text = text[:max_chars]
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = re.sub(r"\s*\n\s*", " ", para).strip()
        para = _URL.sub("[link]", para)
        para = re.sub(r"\s{2,}", " ", para)
        if not para:
            continue
        for piece in _SENTENCE_SPLIT.split(para):
            piece = piece.strip()
            if len(piece) >= 2:
                units.append(piece)
    # Keep greetings, sign-off names, and other short lines as their own units so they
    # never get glued onto a quotable sentence; the model simply scores them low.
    units = [u for u in units if re.search(r"[A-Za-zÀ-ÿ\u0400-\u04FF\u0590-\u05FF]", u)]
    if len(units) > max_units:
        truncated = True
        units = units[:max_units]
    return units, truncated


def sentence_id(i: int) -> str:
    return f"S{i:02d}"
