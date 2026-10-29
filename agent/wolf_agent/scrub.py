"""PII and secret scrubbing applied to events before they leave the host.

Every string in an event's title and payload (sample lines, tracebacks, messages, routes...)
goes through the same ordered rules. Replacements are fixed placeholders, so scrubbing is
deterministic and idempotent, and the structural fields (type, host, bench, site, timestamp,
fingerprint) are never touched: parsers compute fingerprints from the raw values, so grouping
on the Wolf site is the same with or without scrubbing.

Config (`scrub` in config.json, every key optional):

    {"enabled": true, "ips": false, "extra_patterns": [], "keep": []}

- `ips`: also replace IPv4/IPv6 addresses. Off by default: SSH brute-force and "login from a
  new IP" signals are built on them. When on, an address becomes `[IP:<8 hex>]`, a keyed hash
  that is stable per agent, so events from one address still look alike.
- `extra_patterns`: more regexes, either strings (replaced by `[REDACTED]`) or
  `{"pattern": "...", "replacement": "..."}`. Invalid ones are logged and ignored.
- `keep`: values never replaced: exact strings (case-insensitive), or `@domain` to keep every
  address of that domain.
"""

import hashlib
import hmac
import logging
import re

log = logging.getLogger(__name__)

REDACTED = "[REDACTED]"
EMAIL = "[EMAIL]"
CARD = "[CARD]"
IBAN = "[IBAN]"
JWT = "[JWT]"

# event fields that are scope/structure, not content
STRUCTURAL_FIELDS = ("type", "category", "severity", "timestamp", "host", "bench", "site", "source", "fingerprint")

_QUOTED_OR_BARE = r"""(?:(?P<q>['"])(?P<qv>.*?)(?P=q)|(?P<bv>\[REDACTED\]|[^&\s'",;}\)\]]+))"""

# `Authorization: Bearer x`, `'Authorization': 'token key:secret'`, `Proxy-Authorization=Basic x`
_AUTH_HEADER_RE = re.compile(
	r"(?i)(?P<k>\b(?:proxy-)?authorization['\"]?\s*[:=]\s*)"
	r"""(?:(?P<q>['"])(?P<qv>.*?)(?P=q)|(?P<bv>[^\s'",;]+(?:[ \t]+[^\s'",;]+)?))"""
)
# `Cookie: sid=...; system_user=yes`, `Set-Cookie: sid=...; Path=/`: the whole value goes
_COOKIE_HEADER_RE = re.compile(
	r"""(?i)(?P<k>\b(?:set-)?cookie['"]?\s*[:=]\s*)(?:(?P<q>['"])(?P<qv>.*?)(?P=q)|(?P<bv>[^\r\n'"]+))"""
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
# `Bearer <x>`, `token <api_key>:<api_secret>`, `Basic <b64>`: the credential must hold a digit
_SCHEME_TOKEN_RE = re.compile(
	r"(?i)\b(?P<k>bearer|token|basic)(?P<sp>\s+)(?P<v>(?=[^\s]*\d)[A-Za-z0-9._~+/=-]{8,}(?::[A-Za-z0-9._~+/=-]{6,})?)"
)
_SECRET_KEYS = (
	r"(?:[\w-]*?[_-])?(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|private[_-]?key|access[_-]?key"
	r"|session[_-]?id|otp|signature|sig)|sid|auth|key[_-]?secret"
)
# `password=x`, `?pwd=x&`, `"api_secret": "x"`, `'sid': 'x'`, `new_password = 'x y'`
_KEY_VALUE_RE = re.compile(
	r"(?i)(?P<k>(?<![\w-])(?:" + _SECRET_KEYS + r")['\"]?\s*[:=]\s*)" + _QUOTED_OR_BARE
)
# also matches URL-encoded addresses (jane%40example.com) in routes and query strings
_EMAIL_RE = re.compile(r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*(?:@|%40)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
# 13-19 digits, optionally grouped by spaces or dashes; issuer digit 2-6 (Visa, MC, Amex, Discover...)
_CARD_RE = re.compile(r"(?<![\d.])[2-6]\d{3}(?:[ -]?\d){9,15}(?![\d.])")
_IPV4_RE = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
# full 8-group form, or a compressed form with `::` (so `02:00:13` and MAC addresses are left alone)
_H = r"[0-9a-fA-F]{1,4}"
_IPV6_RE = re.compile(
	r"(?<![\w:])(?:"
	rf"(?:{_H}:){{7}}{_H}"
	rf"|(?:{_H}:){{1,6}}:(?:{_H}(?::{_H}){{0,5}})?"
	rf"|::(?:{_H}:){{0,6}}{_H}"
	r")(?![\w:])"
)


def luhn_ok(digits):
	total = 0
	for i, ch in enumerate(reversed(digits)):
		n = int(ch)
		if i % 2:
			n *= 2
			if n > 9:
				n -= 9
		total += n
	return total % 10 == 0


def iban_ok(value):
	s = value.replace(" ", "")
	if not 15 <= len(s) <= 34:
		return False
	rearranged = s[4:] + s[:4]
	try:
		return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1
	except ValueError:
		return False


class Scrubber:
	def __init__(self, enabled=True, ips=False, extra_patterns=(), keep=(), salt=""):
		self.enabled = bool(enabled)
		self.ips = bool(ips)
		self.salt = str(salt or "").encode()
		self.keep_exact = set()
		self.keep_domains = set()
		for value in keep or ():
			value = str(value).strip().lower()
			if value.startswith("@"):
				self.keep_domains.add(value[1:])
			elif value:
				self.keep_exact.add(value)
		self.extra = []
		for entry in extra_patterns or ():
			pattern, replacement = (entry.get("pattern"), entry.get("replacement")) if isinstance(entry, dict) else (entry, None)
			try:
				self.extra.append((re.compile(pattern), REDACTED if replacement is None else str(replacement)))
			except (re.error, TypeError) as e:
				log.warning("scrub: ignoring invalid extra pattern %r: %s", pattern, e)

	@classmethod
	def from_config(cls, cfg):
		section = cfg.get("scrub")
		if section is False:
			section = {"enabled": False}
		section = section if isinstance(section, dict) else {}
		return cls(
			enabled=section.get("enabled", True),
			ips=section.get("ips", False),
			extra_patterns=section.get("extra_patterns") or (),
			keep=section.get("keep") or (),
			salt=cfg.get("agent_id") or cfg.get("hostname") or "",
		)

	def _kept(self, value):
		v = value.strip().lower()
		if v in self.keep_exact:
			return True
		v = v.replace("%40", "@")
		return "@" in v and v.rsplit("@", 1)[1] in self.keep_domains

	def _keep_or(self, value, placeholder):
		return value if self._kept(value) else placeholder

	def _redact_value(self, m):
		if m.group("q"):
			return m.group("k") + m.group("q") + self._keep_or(m.group("qv"), REDACTED) + m.group("q")
		return m.group("k") + self._keep_or(m.group("bv"), REDACTED)

	def _ip(self, m):
		value = m.group(0)
		if self._kept(value):
			return value
		digest = hmac.new(self.salt, value.lower().encode(), hashlib.sha256).hexdigest()[:8]
		return f"[IP:{digest}]"

	def _card(self, m):
		digits = re.sub(r"[ -]", "", m.group(0))
		if 13 <= len(digits) <= 19 and luhn_ok(digits) and not self._kept(m.group(0)):
			return CARD
		return m.group(0)

	def _iban(self, m):
		return IBAN if iban_ok(m.group(0)) and not self._kept(m.group(0)) else m.group(0)

	def text(self, s):
		if not self.enabled or not isinstance(s, str) or not s:
			return s
		s = _AUTH_HEADER_RE.sub(self._redact_value, s)
		s = _COOKIE_HEADER_RE.sub(self._redact_value, s)
		s = _JWT_RE.sub(lambda m: self._keep_or(m.group(0), JWT), s)
		s = _SCHEME_TOKEN_RE.sub(lambda m: m.group("k") + m.group("sp") + self._keep_or(m.group("v"), REDACTED), s)
		s = _KEY_VALUE_RE.sub(self._redact_value, s)
		s = _EMAIL_RE.sub(lambda m: self._keep_or(m.group(0), EMAIL), s)
		s = _IBAN_RE.sub(self._iban, s)
		s = _CARD_RE.sub(self._card, s)
		if self.ips:
			s = _IPV4_RE.sub(self._ip, s)
			s = _IPV6_RE.sub(self._ip, s)
		for pattern, replacement in self.extra:
			s = pattern.sub(replacement, s)
		return s

	def value(self, v):
		"""Scrub every string in a JSON-like value; keys and non-strings are kept."""
		if isinstance(v, str):
			return self.text(v)
		if isinstance(v, dict):
			return {k: self.value(x) for k, x in v.items()}
		if isinstance(v, (list, tuple)):
			return [self.value(x) for x in v]
		return v

	def event(self, ev):
		if not self.enabled:
			return ev
		out = dict(ev)
		for key, val in ev.items():
			if key not in STRUCTURAL_FIELDS:
				out[key] = self.value(val)
		return out

	def events(self, events):
		return [self.event(e) for e in events] if self.enabled else events
