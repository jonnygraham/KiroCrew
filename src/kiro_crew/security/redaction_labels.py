"""Context rules: a secret found by the field it sits in or the key ID beside it.

Two rules live here, both ranked after the fixed patterns of
:mod:`kiro_crew.security.redaction_patterns`:

* **Labelled values.** A value after a secret field name (``password =``,
  ``"SecretAccessKey":``, ``Authorization: Bearer``) is a Secret. Only the VALUE
  is replaced; the label stays, so structured output still parses and a reader
  sees which field went. Labels match case-insensitively and accept any of the
  usual word separators (see ``credential_patterns``).
* **Key-ID pairing.** An AWS access key ID is an Identifier and stays visible,
  but an unlabelled 40-character secret-shaped value next to one is the secret
  half of the pair, so that value is a Secret.

Each function returns spans against its input; the redactor merges them with the
other passes' spans.

:mod:`kiro_crew.security.redaction` is the import path callers use.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from kiro_crew.credential_patterns import (
    AUTHORIZATION_LABEL,
    AUTHORIZATION_VALUE,
    AWS_KEY_ID,
    AWS_KEY_ID_REDACTION,
    AWS_SECRET_LABEL,
    AWS_SESSION_LABEL,
    GENERIC_SECRET_LABEL,
    LABEL_SEPARATOR,
    LABEL_VALUE_OR_QUOTED,
    REFERENCE_VALUE,
)

from .redaction_normalise import normalised_view
from .secret_catalog import is_identifier_value, is_naming_value

#: Every rule id this module writes into a span.
RULE_IDS = (
    "authorization_header",
    "aws_secret_access_key",
    "aws_session_token",
    "secret_label",
    "aws_key_pair",
)


class LabelSpan(NamedTuple):
    """One value to redact: its span, the rule that found it and the label kept."""

    start: int
    end: int
    rule: str
    label: str


#: The label group takes up to four leading words of the field name, so a record
#: names the whole field (``AWS_SECRET_ACCESS_KEY=``, not its tail). It has no
#: left boundary: a label glued to the text before it is still a label.
_LABELLED_RE = re.compile(
    rf"(?P<label>(?:[A-Za-z0-9]{{1,32}}[_.-]){{0,4}}(?:(?P<aws_secret>{AWS_SECRET_LABEL})|(?P<aws_session>{AWS_SESSION_LABEL})"
    rf"|{GENERIC_SECRET_LABEL}){LABEL_SEPARATOR})(?P<value>{LABEL_VALUE_OR_QUOTED})"
)

#: The AWS labels with no separator at all (``aws configure set
#: aws_secret_access_key <key>``, ``--aws-secret-access-key <key>``, ``secret
#: access key is <key>``) or with the value in brackets (``key: <value>``). The
#: label alone is weak evidence here, so the value must have the secret's own
#: shape: 40 base64 characters for the key, 100 or more for a session token.
_AWS_SHAPED_RE = re.compile(
    rf"(?P<label>(?:{AWS_SECRET_LABEL}|(?P<session>{AWS_SESSION_LABEL}))"
    r"""["'>]?\s{0,8}(?:(?i:is|was)\s|[:=])?\s{0,8}["'<{(\[]?)"""
    r"(?P<value>(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])"
    r"|(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{100,})"
)

#: A field whose NAME is a secret label and whose value sits in a sibling field:
#: ``{"name": "AWS_SECRET_ACCESS_KEY", "value": "..."}`` (container and task
#: environments) and its YAML form.
_NAME_VALUE_RE = re.compile(
    r"""(?i:name)["']?\s{0,8}[:=]\s{0,8}["']?"""
    rf"(?P<name>[A-Za-z0-9_.-]{{0,40}}(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}|{GENERIC_SECRET_LABEL}))"
    r"""["']?\s{0,8},?\s{0,16}(?:-\s{1,8})?"""
    r"""(?P<label>["']?(?i:value)["']?\s{0,8}[:=]\s{0,8}["']?)"""
    rf"(?P<value>{LABEL_VALUE_OR_QUOTED})"
)

#: The labels whose value is a credential whatever its shape: the AWS secret
#: access key and session token, as plain patterns for a scan that reads only
#: ``get_credential_patterns()``.
FIXED_LABEL_RE = re.compile(
    rf"(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}){LABEL_SEPARATOR}(?![$%{{<*])"
    r"""[^\s"'`,;&{}()<>\[\]\\]{8,}"""
)

_AUTHORIZATION_RE = re.compile(rf"(?P<label>{AUTHORIZATION_LABEL})(?P<value>{AUTHORIZATION_VALUE})")

#: The shortest value a label rule redacts. Shorter values are flags, units and
#: words (``password: no``), not secrets.
_MIN_VALUE_LEN = 8

#: A WHOLE value that names a secret instead of holding one: an environment or
#: template reference (``$VAR``, ``${VAR}``, ``$(cmd)``, ``%VAR%``, ``{{ x }}``,
#: ``{name}``, ``<your key>``) or a mask (``****``). A value that only STARTS with
#: one of these characters (``$Xy9!kQ``) is a literal.
_REFERENCE_RE = re.compile(REFERENCE_VALUE)

#: A whole redaction tag (``[REDACTED: credential]``): a fixed point, not a value.
_TAG_RE = re.compile(r"\[REDACTED[^\]\n]{0,64}\]")


def _is_reference(value: str) -> bool:
    """Whether *value* is a reference or a mask (:data:`_REFERENCE_RE`), whole."""
    return _REFERENCE_RE.fullmatch(value) is not None


#: A secret-store reference (``secret://name``, ``vault://path``, ``env:NAME``):
#: it names where a secret lives, and is not one.
_STORE_REFERENCE_RE = re.compile(r"(?i:[a-z][a-z0-9+.-]{1,15}://|(?:env|ssm|op|keychain):)")

#: Words that, before ``_token``, name a token that is not a credential: a page
#: cursor, a form's anti-forgery nonce, a count.
_NOT_A_SECRET_TOKEN = frozenset(
    {"next", "page", "pagination", "continuation", "cursor", "csrf", "xsrf", "sync", "resume"}
    | {"max", "num", "total", "count", "the", "a", "end", "start", "stop", "sort", "search"}
)


def _names_no_secret(label: str) -> bool:
    """A ``*_token`` label whose word before it names a cursor or a nonce."""
    words = [w for w in re.split(r"[^a-z0-9]+", label.lower()) if w]
    # The text before a label can end mid-word; only the last two words count.
    return len(words) >= 2 and words[-1] == "token" and words[-2] in _NOT_A_SECRET_TOKEN


#: A one-case run of letters this long is a passphrase, not a word.
_WORD_LEN_MAX = 15


def _value_shaped(value: str) -> bool:
    """Whether a value reads as a credential rather than a word.

    A digit, a symbol or mixed case marks one; so does a single-case run of
    letters longer than an ordinary word. ``password: required`` and
    ``token: expired`` stay; ``password: correcthorsebatterystaple`` goes.
    """
    if not value.isalpha():
        return True
    return not (value.islower() or value.isupper()) or len(value) > _WORD_LEN_MAX


#: A dotted chain of names: code reading an attribute (``settings.SECRET``).
def _in_arn(text: str, pos: int) -> bool:
    """A label word right after a ``:`` of an ARN (``arn:...:secret:name``) is a
    resource type; after ``$env:`` or any other colon it is still a label."""
    if pos == 0 or text[pos - 1] != ":":
        return False
    token = text[max(0, pos - 256) : pos].split()
    return bool(token) and "arn:" in token[-1].lower()


def _is_attribute_read(value: str) -> bool:
    """A dotted read (``settings.aws_key``): identifier parts, none key material
    (16+ chars with a digit and no ``_``, as in ``<key>.txt``). Linear time."""
    parts = value.split(".")
    return len(parts) > 1 and all(
        p.isascii()
        and p.isidentifier()
        and not (len(p) >= 16 and "_" not in p and any(c.isdigit() for c in p))
        for p in parts
    )


#: A type or class name: an annotation (``api_key: SecretStr``) names a type.
_TYPE_NAME_RE = re.compile(r"(?:[A-Z][a-z]{1,15}){2,4}[0-9]?")

#: The longest quoted value read through to its closing quote.
_QUOTED_MAX = 512


def _looks_like_code(text: str, value: str, end: int) -> bool:
    """Whether an UNQUOTED value is source code that reads or types a secret.

    A call (``get_token(...)``), a subscript or a type parameter (``os.environ[``,
    ``Optional[str]``), a union (``str | None``), a dotted, constant, variable or
    type name, and a path. A quoted value is a literal and is never code.
    """
    if text.startswith(("(", "[", "|"), end) or text.startswith(" |", end):
        return True
    if value.startswith(("/", "~/", "./", "../")) or _is_attribute_read(value):
        return True
    if _TYPE_NAME_RE.fullmatch(value):
        return True
    # A bare name is code only on a line that reads as code: an env dump or a
    # config line (``DB_PASSWORD=correct_horse_battery``) holds a passphrase.
    return _is_code_name(value) and _CODE_LINE_RE.search(_line_of(text, end)) is not None


#: Syntax that only source code writes on a line.
_CODE_LINE_RE = re.compile(
    r"[(){};]|\b(?:self|this|cls)\.|\b(?:const|let|var|def|return|function|val)\s"
)


def _line_of(text: str, pos: int) -> str:
    """The line of *text* that holds *pos*, at most 512 characters each side."""
    start = text.rfind("\n", max(0, pos - 512), pos) + 1 or max(0, pos - 512)
    end = text.find("\n", pos, pos + 512)
    return text[start : end if end != -1 else min(len(text), pos + 512)]


def _is_code_name(value: str) -> bool:
    """A CONSTANT or snake_case name, or a camelCase variable (``passwordInput``,
    letters only). String checks, linear time."""
    if not (value.isascii() and value.isidentifier() and value[0].isalpha()):
        return False
    if "_" in value:
        return value.isupper() or value.islower()
    humps = value[1:]
    return (
        value.isalpha()
        and value[0].islower()
        and any(c.isupper() for c in humps)
        and all(c.islower() for a, c in zip(humps, humps[1:]) if a.isupper())
        and humps[-1].islower()
    )


def _quoted_value_end(text: str, start: int, quote: str) -> int:
    """End of a value opened by *quote* at *start*: its unescaped closing quote.

    A value still open at the line end, the text end or after ``_QUOTED_MAX``
    characters ends there, so it is removed whole up to that point rather than
    cut at its first delimiter.
    """
    pos = start
    limit = min(len(text), start + _QUOTED_MAX)
    while pos < limit:
        char = text[pos]
        if char == "\\":
            pos += 2
            continue
        if char == quote or char == "\n":
            return pos
        pos += 1
    return min(pos, len(text))


def _labelled_value(text: str, m: re.Match[str]) -> tuple[str, int, str]:
    """``(value, end, quote)``: a quoted literal runs to its closing quote, delimiters and all."""
    value, end, start = m["value"], m.end("value"), m.start("value")
    quote = m["label"][-1] if m["label"][-1] in "\"'" else ""
    close = _quoted_value_end(text, start, quote) if quote else None
    if close is not None and close > end:
        # Never shorter than the unquoted run (a 600-character token in quotes
        # is past the quoted bound but still one value).
        value, end = text[start:close], close
    if _TAG_RE.fullmatch(value):
        return "", end, quote  # already a redaction tag: a fixed point
    return value, end, quote


def _generic_value_is_secret(text: str, value: str, end: int, *, quoted: bool = False) -> bool:
    """Whether a value after a generic secret label holds a secret.

    A generic label (``password``, ``api_key``, ``*_token``) also names fields in
    code and prose, so its value must look like a value: long enough, not a
    single-case word (:func:`_value_shaped`), not a reference, not a call (``get_token(...)``), not a dotted name or
    a constant's name, and not an identifier shape.
    """
    if len(value) < _MIN_VALUE_LEN or _is_reference(value):
        return False
    if _STORE_REFERENCE_RE.match(value):
        return False
    if not _value_shaped(value):
        return False
    if not quoted and _looks_like_code(text, value, end):
        return False
    # A UUID or a hex string behind a secret label is a secret in that format
    # (webhook secrets often are); only a value that can only name something --
    # a key ID, an account ID, an ARN -- is kept.
    return not is_naming_value(value)


def fixed_label_patterns() -> list[re.Pattern[str]]:
    """The AWS label and ``Authorization`` patterns, for scans that only search."""
    return [FIXED_LABEL_RE, _AUTHORIZATION_RE]


def label_spans(text: str) -> list[LabelSpan]:
    """Every labelled secret value in *text*, sorted by start and disjoint."""
    found: list[LabelSpan] = []
    for m in _AUTHORIZATION_RE.finditer(text):
        found.append(
            LabelSpan(m.start("value"), m.end("value"), "authorization_header", m["label"])
        )
    for m in _LABELLED_RE.finditer(text):
        value, end, quote = _labelled_value(text, m)
        if m["aws_secret"] is not None or m["aws_session"] is not None:
            if len(value) < _MIN_VALUE_LEN or _is_reference(value):
                continue
            # An AWS secret is base64 and may start with ``/`` or look like a
            # word, so only code that READS one (a call, a subscript, a dotted
            # attribute read) is exempt here.
            if _STORE_REFERENCE_RE.match(value) or (
                not quote and (text.startswith(("(", "["), end) or _is_attribute_read(value))
            ):
                continue
            rule = "aws_secret_access_key" if m["aws_secret"] is not None else "aws_session_token"
        elif (
            # Inside an ARN (``...:secret:name``) the word is a resource type.
            not _in_arn(text, m.start())
            and not _names_no_secret(text[max(0, m.start() - 40) : m.end("label")])
            and _generic_value_is_secret(text, value, end, quoted=bool(quote))
        ):
            rule = "secret_label"
        else:
            continue
        found.append(LabelSpan(m.start("value"), end, rule, m["label"]))
    for m in _AWS_SHAPED_RE.finditer(text):
        if _secret_shaped(m["value"]) or (m["session"] is not None and len(m["value"]) >= 100):
            rule = "aws_session_token" if m["session"] is not None else "aws_secret_access_key"
            found.append(LabelSpan(m.start("value"), m.end("value"), rule, m["label"]))
    for m in _NAME_VALUE_RE.finditer(text):
        value, end, _quote = _labelled_value(text, m)
        if len(value) >= _MIN_VALUE_LEN and not _is_reference(value):
            found.append(LabelSpan(m.start("value"), end, "secret_label", m["label"]))
    # A label that sits inside an earlier match's value is part of that value,
    # not a label: judge overlap from where each label starts.
    found.sort(key=lambda span: span.start - len(span.label))
    disjoint: list[LabelSpan] = []
    for span in found:
        if disjoint and span.start - len(span.label) < disjoint[-1].end:
            continue
        disjoint.append(span)
    return disjoint


# ── URI passwords with a slash ──
# The fixed URI-userinfo pattern stops a password at ``/`` so it never runs past
# the authority into the path. A password may still carry an unencoded ``/`` (a
# cloud secret key used as a database password does), so this rule reads a
# password that holds one, up to the first ``@`` that is followed by a host.
_URI_SLASH_PASSWORD_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|rediss?|amqps?|https?|ftps?)"
    r"://[^\s:/@]{0,128}:(?P<value>[^\s/@]{0,256}/[^\s@]{0,256}?)@"
    r"(?=[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?::[0-9]{1,5})?(?:[/?#\s\"'>]|\Z))"
)


def uri_password_spans(text: str) -> list[LabelSpan]:
    """Passwords holding a ``/`` in ``scheme://user:password@host`` URIs."""
    if "://" not in text:
        return []
    return [
        LabelSpan(m.start("value"), m.end("value"), "url_userinfo", "")
        for m in _URI_SLASH_PASSWORD_RE.finditer(text)
    ]


# ── Key-ID pairing ──
#: Any AWS key ID a secret can be paired with. Only an upper-case letter or a
#: digit before it is part of a longer token; a lower-case word glued in front
#: (``key<ID>``) still leaves it a key ID. The tag a review scrubber writes in a
#: key ID's place (``[REDACTED-AWS-KEY-ID]``) still marks where one stood.
_KEY_ID_RE = re.compile(
    rf"(?<![A-Z0-9])(?:{AWS_KEY_ID}|{AWS_KEY_ID_REDACTION})(?![A-Za-z0-9])|\[?REDACTED-AWS-KEY-ID\]"
)

#: A secret access key: exactly 40 characters of the base64 alphabet, standing
#: alone (not part of a longer run).
_PAIRED_SECRET_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])")

#: The same key as a URL path segment run, tried when no standalone run is in reach.
_PAIRED_SECRET_IN_PATH_RE = re.compile(r"[A-Za-z0-9+/]{40}(?![A-Za-z0-9+=])")

#: How far from a key ID its secret may sit: the width of a CSV row, a JSON
#: object or an ``export`` pair of lines, in characters, on either side.
_PAIR_REACH = 256

#: The length of a secret access key.
_SECRET_LEN = 40


def _secret_shaped(value: str) -> bool:
    """A 40-character value that can be a secret key: mixed case or symbols, not hex."""
    if is_identifier_value(value):
        return False
    # The secret key's alphabet is base64; a digit is common but not required.
    return any(c.islower() for c in value) and any(c.isupper() for c in value)


def paired_secret_spans(text: str, units: list[tuple[int, int]] | None = None) -> list[LabelSpan]:
    """Unlabelled secret-shaped values within reach of an AWS access key ID.

    The key ID itself is never in a span: it is an Identifier. *units*, when
    given, receives each pair as one span, key ID and secret, either order.
    """
    if not any(
        prefix in text for prefix in ("AKIA", "ASIA", "ABIA", "ACCA", "REDACTED-AWS-KEY-ID")
    ):
        return []
    found: list[LabelSpan] = []
    last_end = 0
    for key in _KEY_ID_RE.finditer(text):
        lo = max(last_end, key.start() - _PAIR_REACH)
        hi = min(len(text), key.end() + _PAIR_REACH)
        # The window ends one past the last run that can start in reach, so the
        # look-ahead sees what follows a run and a longer one is never cut to 40.
        window_end = min(len(text), hi + _SECRET_LEN + 1)
        count = len(found)
        for m in _PAIRED_SECRET_RE.finditer(text, lo, window_end):
            if m.start() >= hi:
                break
            # A run reaching the window's edge is judged on the whole text.
            edge = m.end() == window_end and window_end < len(text)
            if (edge and not _PAIRED_SECRET_RE.match(text, m.start())) or not _secret_shaped(
                m.group()
            ):
                continue
            found.append(LabelSpan(m.start(), m.end(), "aws_key_pair", ""))
            last_end = m.end()
            if units is not None:
                units.append((min(key.start(), m.start()), max(key.end(), m.end())))
        # A URL path (``/<key ID>/<secret>``): a run after each ``/`` past the key.
        slash = text.find("/", max(last_end, key.end()), hi) if len(found) == count else -1
        while slash != -1:
            hit = _PAIRED_SECRET_IN_PATH_RE.match(text, slash + 1)
            if hit is not None and _secret_shaped(hit.group()):
                found.append(LabelSpan(hit.start(), hit.end(), "aws_key_pair", ""))
                last_end = hit.end()
                if units is not None:
                    units.append((key.start(), hit.end()))
                break
            slash = text.find("/", slash + 1, hi)
    return found


# ── Stream holdback ──
# The stream redactor emits text in pieces, and the rules above need a label,
# a key ID or a sibling field in the same batch call as the value. These are
# the shapes whose value may still be arriving at the end of a buffer.

#: How much of a buffer's tail the hold looks at: the pairing reach plus a key
#: and its secret, with room for a label.
_HOLD_TAIL = _PAIR_REACH + 2 * _SECRET_LEN + 64

#: An AWS label with no separator (``secret access key is``), possibly with the
#: start of its value, ending the buffer.
_AWS_SHAPED_TAIL_RE = re.compile(
    rf"(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL})"
    r"""["'>]?\s{0,8}(?:(?i:is|was)\s|[:=])?\s{0,8}["'<{(\[]?[A-Za-z0-9+/=]*\Z"""
)

#: A ``name`` field naming a secret label, then the start of its sibling
#: ``value`` field, ending the buffer.
_NAME_VALUE_TAIL_RE = re.compile(
    r"""(?i:name)["']?\s{0,8}[:=]\s{0,8}["']?"""
    rf"[A-Za-z0-9_.-]{{0,40}}(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}|{GENERIC_SECRET_LABEL})"
    r"""(?:["']?\s{0,8},?\s{0,16}(?:-\s{1,8})?(?:["']?(?i:v(?:a(?:l(?:u(?:e)?)?)?)?)"""
    r"""(?:["']?\s{0,8}[:=]\s{0,8}["']?[^\s"'`,;&{}()<>\[\]\\]*)?)?)?\Z"""
)

#: A label whose quoted value is still open at the buffer end: the batch rule
#: reads a quoted value through to its closing quote, delimiters and all.
_OPEN_QUOTED_TAIL_RE = re.compile(
    rf"(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}|{GENERIC_SECRET_LABEL})"
    r"""["'*_]{0,3}\s{0,8}(?:=>|:=|[:=>\uff1a\uff1d])[*_]{0,3}\s{0,8}"""
    # Still open, or just closed with only a run of non-blank characters after
    # it (the stream's own holdback would otherwise cut inside the value).
    # An escaped character inside the value (``\"``) does not close it.
    r"""(?:"(?:[^"\\\n]|\\.){0,544}\\?(?:"\S{0,64})?"""
    r"""|'(?:[^'\\\n]|\\.){0,544}\\?(?:'\S{0,64})?)\Z"""
)

#: How far back a quoted value's label may sit: the label and separator, the
#: longest quoted value the batch rule reads, its closing quote and what follows.
QUOTED_HOLD_REACH = 96 + _QUOTED_MAX + 64

_KEY_ID_PREFIXES = ("AKIA", "ASIA", "ABIA", "ACCA", "REDACTED-AWS-KEY-ID")

#: The first word of every label form that has no separator, or whose value
#: sits in a sibling field: while one is within reach of the buffer end, the
#: label it starts may still be arriving in pieces.
_LABEL_WORD_RE = re.compile(r"(?i:secret|session|security|name)")

#: A secret label at the buffer's end with its separator, and the value, still
#: arriving: held so the value is redacted beside its label.
_LABEL_TAIL_RE = re.compile(
    rf"(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}|{GENERIC_SECRET_LABEL})"
    r"""["'*_\\]{0,6}\s{0,8}(?:(?:=>|:=|[:=>\uff1a\uff1d])[*_\s"'\\]{0,12}"""
    r"""[^\s"'`,;&{}()<>\[\]\\]{0,512})?\Z"""
)
#: How long after a label word the stream keeps holding: a label, its
#: separator and a quoted value's opening, or the no-separator AWS form and its
#: 40-character value.
_LABEL_WORD_REACH = 96


def stream_hold_start(buf: str) -> int | None:
    """:func:`_hold_start` on the buffer and on its normalised view, the earlier.

    The batch pass also reads labels through escapes and terminal codes
    (``{\\"password\\": ...}``), so the hold looks through them too and maps the
    view's position back to the buffer.
    """
    hold = _hold_start(buf)
    lo = max(0, len(buf) - 2 * _HOLD_TAIL)
    view = normalised_view(buf[lo:])
    seen = _hold_start(view.text) if view is not None else None
    if view is not None and seen is not None and seen < len(view.text):
        start = lo + view.starts[seen]
        hold = start if hold is None else min(hold, start)
    return hold


def open_quoted_start(buf: str) -> int | None:
    """Where a labelled quoted value still open at the buffer's end starts its
    label, looking back :data:`QUOTED_HOLD_REACH`; ``None`` when there is none.

    The stream holds from here until the closing quote, past its usual 512
    floor up to that reach, so the value is redacted beside its label.
    """
    lo = max(0, len(buf) - QUOTED_HOLD_REACH)
    tail = _OPEN_QUOTED_TAIL_RE.search(buf, lo)
    return tail.start() if tail is not None else None


def _hold_start(buf: str) -> int | None:
    """Where the stream must hold from so a context rule still sees its whole match.

    Earliest of: an AWS key ID whose pairing reach has not fully arrived, a
    secret-shaped 40-character run that a key ID may still follow, a label word
    near the end, an AWS label with no separator ending the buffer, a
    ``name``/``value`` pair and a quoted value still open. ``None`` when nothing needs holding. Looks only
    at the buffer's tail, so it costs the same however long the buffer is.
    """
    lo = max(0, len(buf) - _HOLD_TAIL)
    starts: list[int] = []
    reach = _PAIR_REACH + _SECRET_LEN + 1
    if any(prefix in buf[lo:] for prefix in _KEY_ID_PREFIXES):
        for key in _KEY_ID_RE.finditer(buf, lo):
            if len(buf) - key.end() < reach:
                starts.append(key.start())
                break
    for run in _PAIRED_SECRET_RE.finditer(buf, lo):
        if len(buf) - run.end() < reach and _secret_shaped(run.group()):
            starts.append(run.start())
            break
    words = [
        m.start()
        for m in _LABEL_WORD_RE.finditer(buf, max(lo, len(buf) - _LABEL_WORD_REACH))
        if len(buf) - m.start() < _LABEL_WORD_REACH
    ]
    if words:
        starts.append(words[0])
    for pattern in (_AWS_SHAPED_TAIL_RE, _NAME_VALUE_TAIL_RE):
        tail = pattern.search(buf, lo)
        if tail is not None:
            starts.append(tail.start())
    # A quoted value is held until its closing quote, as far as the batch rule
    # reads one (``_QUOTED_MAX``): look back that far for its label.
    for pattern in (_OPEN_QUOTED_TAIL_RE, _LABEL_TAIL_RE):
        tail = pattern.search(buf, max(0, len(buf) - QUOTED_HOLD_REACH))
        if tail is not None:
            starts.append(tail.start())
    # A label word in reach also covers the label written before a value (the
    # value's own hold would otherwise cut between them); the caller widens the
    # hold to the start of the token it lands in.
    return min(starts) if starts else None
