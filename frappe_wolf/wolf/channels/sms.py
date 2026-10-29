"""SMS through any HTTP gateway, described by a template (pure).

Provider agnostic: the URL, the optional body and the auth header are templates with
``{to}``, ``{text}`` and ``{secret}`` placeholders. ``{secret}`` comes from a Password
field so an API key never sits in a plain field. Values are URL-encoded in the URL and
in form bodies, JSON-escaped in JSON bodies, and inserted as-is in the header.

Length: an SMS segment holds 160 GSM-7 characters (extension characters such as
``€ [ ] { }`` take two) or 70 UCS-2 characters; concatenated messages hold 153 / 67
per segment (3GPP TS 23.038, https://en.wikipedia.org/wiki/GSM_03.38). Text is
first folded to GSM-7 where an ASCII equivalent exists, then cut on a word boundary.
"""

import json
import re
from urllib.parse import quote, urlencode

from frappe_wolf.wolf.channels import mask_number, recipients, truncate

GSM_BASIC = set(
	"@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
	"¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_EXTENSION = set("^{}\\[~]|€\f")
FOLD = {
	"·": "-",
	"…": "...",
	"→": "->",
	"–": "-",
	"—": "-",
	"‘": "'",
	"’": "'",
	"“": '"',
	"”": '"',
	" ": " ",
	"\t": " ",
}
# Characters per message for 1 and 2 segments, GSM-7 then UCS-2.
LIMITS = {160: (160, 70), 320: (306, 134)}


def fold(text):
	return "".join(FOLD.get(ch, ch) for ch in text or "")


def gsm_length(text):
	"""Length in GSM-7 septets, or None when the text needs UCS-2."""
	n = 0
	for ch in text:
		if ch in GSM_BASIC:
			n += 1
		elif ch in GSM_EXTENSION:
			n += 2
		else:
			return None
	return n


def limits(max_length):
	try:
		return LIMITS[int(max_length or 160)]
	except (KeyError, TypeError, ValueError):
		return LIMITS[160]


def fit(text, max_length=160, reserve=0):
	"""Fold and cut ``text`` to fit ``max_length`` (160: one SMS, 320: two), minus ``reserve``."""
	text = " ".join(fold(text).split())
	gsm_limit, ucs_limit = limits(max_length)
	if gsm_length(text) is None:
		return truncate(text, ucs_limit - reserve, "...")
	limit = n = gsm_limit - reserve
	out = truncate(text, n, "...")
	while n > 0 and gsm_length(out) > limit:
		n -= 1
		out = truncate(text, n, "...")
	return out


def sms_text(msg, max_length=160):
	"""Short alert: kind, severity, title, scope, then the incident URL if it fits."""
	head = f"Wolf {msg['label']} {msg['severity']}: {msg['title']}"
	if msg.get("scope") and msg["scope"] != "all":
		head += f" ({msg['scope']})"
	url = msg.get("url") or ""
	gsm_limit, ucs_limit = limits(max_length)
	limit = gsm_limit if gsm_length(" ".join(fold(head).split())) is not None else ucs_limit
	if url and len(url) + 1 <= limit // 2:
		return fit(head, max_length, reserve=len(url) + 1) + " " + url
	return fit(head, max_length)


_PLACEHOLDER = re.compile(r"\{(to|text|secret)\}")


def _fill(template, values, encode):
	"""One pass, so a ``{secret}`` inside the alert text is never expanded."""
	return _PLACEHOLDER.sub(lambda m: encode(values[m.group(1)]), template or "")


def build_payload(msg, cfg):
	text = sms_text(msg, cfg.get("sms_max_length") or 160)
	method = (cfg.get("sms_method") or "POST").upper()
	as_json = (cfg.get("sms_content_type") or "").lower().endswith("json")
	secret = cfg.get("sms_secret") or ""
	requests = []
	for to in recipients(cfg.get("sms_numbers")):
		values = {"to": to, "text": text, "secret": secret}
		req = {
			"method": method,
			"url": _fill(cfg["sms_url"], values, lambda v: quote(v, safe="")),
			"headers": {},
			"action": f"sms {mask_number(to)}",
		}
		header = (cfg.get("sms_auth_header") or "").strip()
		if ":" in header:
			name, value = header.split(":", 1)
			req["headers"][name.strip()] = _fill(value.strip(), values, str)
		if method != "GET":
			if as_json:
				req["headers"]["Content-Type"] = "application/json"
				if cfg.get("sms_body"):
					req["data"] = _fill(cfg["sms_body"], values, lambda v: json.dumps(v)[1:-1]).encode()
				else:
					req["data"] = json.dumps({"to": to, "text": text}).encode()
			else:
				req["headers"]["Content-Type"] = "application/x-www-form-urlencoded"
				if cfg.get("sms_body"):
					req["data"] = _fill(cfg["sms_body"], values, lambda v: quote(v, safe="")).encode()
				else:
					req["data"] = urlencode({"to": to, "text": text}).encode()
		requests.append(req)
	return requests
