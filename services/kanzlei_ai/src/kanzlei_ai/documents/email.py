"""Extract only the current message's visible body from the typed ERP payload."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from html.parser import HTMLParser
from typing import Literal

from pydantic import field_validator

from kanzlei_ai.connectors.contracts import MAX_SOURCE_BYTES, Contract, EmailId
from kanzlei_ai.documents.parse import TerminalExtractionError

PARSER_VERSION = "email-body-v1"


class EmailPayload(Contract):
    version: Literal[1]
    subject: str
    sender: str
    recipients: list[str]
    direction: Literal["incoming", "outgoing"]
    sent_at: str | None
    reply_to: EmailId | None
    body: str
    body_format: Literal["html", "text"]

    @field_validator("sent_at")
    @classmethod
    def valid_timestamp(cls, value: str | None) -> str | None:
        if value is not None:
            if len(value) <= 10 or value != value.strip():
                raise ValueError("Expected an ISO timestamp")
            datetime.fromisoformat(value)
        return value


# Quote wrappers contain older text. Outlook and Thunderbird also use marker
# elements immediately *before* the old text, so those markers end extraction.
_QUOTE_CLASSES = {
    "gmail_quote",
    "gmail_quote_container",
    "gmail_extra",
    "yahoo_quoted",
    "protonmail_quote",
    "moz-forward-container",
    "applemailquote",
}
_TAIL_MARKERS = {
    "divrplyfwdmsg",
    "replyforwardcontainer",
    "outlookmessageheader",
    "moz-cite-prefix",
    "moz-email-headers-table",
    "gmail_attr",
}
_HIDDEN_STYLE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)"
    r"|content-visibility\s*:\s*hidden|mso-hide\s*:\s*all|opacity\s*:\s*0(?:\.0+)?"
    r"|font-size\s*:\s*0(?:px|pt|em|%)?)\s*(?:!\s*important\s*)?(?=;|$)",
    re.IGNORECASE,
)
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.DOTALL)
_NON_CONTENT_TAGS = {
    "head",
    "script",
    "style",
    "template",
    "noscript",
    "iframe",
    "object",
    "svg",
    "form",
    "textarea",
    "select",
    "title",
    "canvas",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_BLOCK_TAGS = {
    "address",
    "article",
    "dd",
    "div",
    "dl",
    "dt",
    "footer",
    "header",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
_HTML_SPACES = re.compile(r"[\t\n\r\f ]+")


class _VisibleBody(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed: list[str] = []
        self._tail = False
        self._verbatim = False

    def _start(self, tag, attrs, *, self_closing=False):
        if self._tail:
            return
        if self._suppressed:
            if tag not in _VOID_TAGS and not self_closing:
                self._suppressed.append(tag)
            return
        # Browsers use the first occurrence of a duplicate attribute.
        first_attrs = {}
        for name, value in attrs:
            first_attrs.setdefault(name, value)
        attrs = first_attrs
        names = set((attrs.get("class") or "").casefold().split())
        names.add((attrs.get("id") or "").casefold())
        if names & _TAIL_MARKERS:
            self._tail = True
            return
        style = _CSS_COMMENT.sub("", attrs.get("style") or "")
        if (
            tag in _NON_CONTENT_TAGS
            or tag == "blockquote"
            or names & _QUOTE_CLASSES
            or (attrs.get("type") or "").strip().casefold() == "cite"
            or "hidden" in attrs
            or (attrs.get("aria-hidden") or "").strip().casefold() == "true"
            or "\\" in style
            or _HIDDEN_STYLE.search(style)
        ):
            if tag not in _VOID_TAGS:
                # In HTML a slash does not close a non-void element. Treat
                # malformed quote/hidden markup as suppressed until its end tag.
                self._suppressed.append(tag)
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")
        if tag == "pre":
            self._verbatim = True

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, self_closing=True)

    def handle_endtag(self, tag):
        if self._tail:
            return
        if self._suppressed:
            if tag == self._suppressed[-1]:
                self._suppressed.pop()
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n\n")
        if tag == "pre":
            self._verbatim = False

    def handle_data(self, data):
        if not self._tail and not self._suppressed:
            self.parts.append(data if self._verbatim else _HTML_SPACES.sub(" ", data))


_TAIL_SEPARATOR = re.compile(
    r"^[-_—–\s]*(?:original message|forwarded message|begin forwarded message"
    r"|ursprüngliche nachricht|originalnachricht|weitergeleitete nachricht"
    r"|исходное сообщение|оригинальное сообщение|пересылаемое сообщение"
    r"|перенаправленное сообщение|переадресованное сообщение|пересланное сообщение)"
    r"[\s:_—–-]*$",
    re.IGNORECASE,
)
_REPLY_START = re.compile(
    r"^(?:(?:on|am|в|пн|вт|ср|чт|пт|сб|вс)\b|\d{1,2}\.\d{1,2}\.\d{2,4}\b)", re.IGNORECASE
)
_REPLY_END = re.compile(r"\b(?:wrote|schrieb|писал[аи]?|написал[аи]?|пишет)\s*:\s*$", re.IGNORECASE)
_GERMAN_REPLY = re.compile(r"\bschrieb\s+.{1,500}:$", re.IGNORECASE)
_HEADER = re.compile(
    r"^(from|sender|von|absender|от|отправитель|sent|date|gesendet|datum|дата"
    r"|отправлено|to|an|кому|cc|копия|subject|betreff|тема)\s*:\s*\S",
    re.IGNORECASE,
)
_SENDER_HEADERS = {"from", "sender", "von", "absender", "от", "отправитель"}
_MARKER_PREFIX = re.compile(r"^[\s\ufeff\u200b\u200e\u200f]+")


def _strip_quoted_body(body: str) -> str:
    lines = body.splitlines()
    kept = []
    for index, line in enumerate(lines):
        stripped = _MARKER_PREFIX.sub("", line).rstrip()
        if stripped.startswith(">"):
            continue
        if _TAIL_SEPARATOR.fullmatch(stripped):
            break
        if stripped.endswith(":") and (
            _REPLY_END.search(stripped) or _GERMAN_REPLY.search(stripped)
        ):
            # Wrapped reply attribution can start on an earlier physical line.
            for start in range(index, max(-1, index - 4), -1):
                candidate = _MARKER_PREFIX.sub("", lines[start]).rstrip()
                if not candidate or candidate.startswith(">"):
                    break
                if _REPLY_START.match(candidate):
                    del kept[len(kept) - (index - start) :]
                    return "\n".join(kept).strip()
        header = _HEADER.match(stripped)
        if header and header[1].casefold() in _SENDER_HEADERS:
            # Require a second recognized header to avoid dropping a normal
            # message just because it contains a line reading "From: ...".
            following = (item.strip() for item in lines[index + 1 : index + 6])
            if any(
                match and match[1].casefold() not in _SENDER_HEADERS
                for match in map(_HEADER.match, following)
            ):
                break
        kept.append(line)
    return "\n".join(kept).strip()


@dataclass(frozen=True)
class EmailBlock:
    text: str
    page: None = None
    kind: Literal["text"] = "text"


@dataclass(frozen=True)
class ParsedEmail:
    content_sha256: str
    blocks: list[EmailBlock]
    parser_version: str = PARSER_VERSION
    page_count: int = 0

    def artifact_bytes(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":")).encode()


def _unique_keys(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("Duplicate email payload key")
        fields[key] = value
    return fields


def parse_email(content: bytes) -> ParsedEmail:
    if len(content) > MAX_SOURCE_BYTES:
        raise TerminalExtractionError("SOURCE_LIMIT_EXCEEDED")
    try:
        data = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_keys)
        value = EmailPayload.model_validate(data)
        for field in [
            value.subject,
            value.sender,
            value.body,
            *value.recipients,
            value.reply_to or "",
        ]:
            if "\x00" in field:
                raise ValueError("Invalid email text")
            field.encode("utf-8")  # Reject JSON-escaped lone surrogates.
        body = value.body
        if value.body_format == "html":
            parser = _VisibleBody()
            parser.feed(body)
            parser.close()
            body = "".join(parser.parts)
    except (ValueError, TypeError, RecursionError):
        raise TerminalExtractionError("INVALID_EMAIL") from None
    body = _strip_quoted_body(body)
    if not any(char.isalnum() for char in body):
        raise TerminalExtractionError("EMAIL_NO_TEXT")
    return ParsedEmail(sha256(content).hexdigest(), [EmailBlock(body)])
