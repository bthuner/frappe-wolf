"""Days until the TLS certificate of each site domain expires.

The agent does a TLS handshake with SNI to `<domain>:443`, takes the leaf certificate as
DER (verification is off on purpose, so an expired or self-signed certificate is still
measured) and reads `notAfter` with a minimal DER walk of the X.509 structure.
"""

import ipaddress
import logging
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .util import metric, utc_iso

log = logging.getLogger(__name__)

LOCAL_SUFFIXES = (".localhost", ".local", ".test", ".internal", ".invalid", ".example", ".lan", ".home.arpa")
# RFC 2606 documentation names: never a real site
RESERVED = ("example.com", "example.net", "example.org")


class DERError(ValueError):
	pass


def _tlv(data, pos):
	"""(tag, content_start, content_end) of the DER element at `pos`."""
	if pos + 2 > len(data):
		raise DERError("truncated")
	tag, first = data[pos], data[pos + 1]
	pos += 2
	if first < 0x80:
		length = first
	else:
		n = first & 0x7F
		if not n or n > 4 or pos + n > len(data):
			raise DERError("bad length")
		length = int.from_bytes(data[pos : pos + n], "big")
		pos += n
	if pos + length > len(data):
		raise DERError("truncated")
	return tag, pos, pos + length


def _parse_time(tag, raw):
	text = raw.decode("ascii")
	if tag == 0x17:  # UTCTime YYMMDDHHMM[SS]Z
		fmt = "%y%m%d%H%M%SZ" if len(text) == 13 else "%y%m%d%H%MZ"
	elif tag == 0x18:  # GeneralizedTime YYYYMMDDHHMMSSZ
		fmt = "%Y%m%d%H%M%SZ"
	else:
		raise DERError(f"unexpected time tag {tag:#x}")
	return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)


def not_after(der):
	"""`notAfter` of a DER X.509 certificate as an aware UTC datetime."""
	tag, start, _ = _tlv(der, 0)  # Certificate
	if tag != 0x30:
		raise DERError("not a certificate")
	tag, pos, end = _tlv(der, start)  # tbsCertificate
	if tag != 0x30:
		raise DERError("no tbsCertificate")
	fields = []
	while pos < end and len(fields) < 6:
		tag, cstart, cend = _tlv(der, pos)
		fields.append((tag, cstart, cend))
		pos = cend
	if fields and fields[0][0] == 0xA0:  # [0] EXPLICIT version
		fields = fields[1:]
	# serialNumber, signature, issuer, validity
	if len(fields) < 4 or fields[3][0] != 0x30:
		raise DERError("no validity")
	vstart = fields[3][1]
	_, _, before_end = _tlv(der, vstart)  # notBefore
	tag, s, e = _tlv(der, before_end)  # notAfter
	return _parse_time(tag, der[s:e])


def fetch_not_after(domain, port=443, timeout=5.0, connect_host=None):
	ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
	ctx.check_hostname = False
	ctx.verify_mode = ssl.CERT_NONE
	with socket.create_connection((connect_host or domain, port), timeout=timeout) as sock:
		with ctx.wrap_socket(sock, server_hostname=domain) as tls:
			der = tls.getpeercert(binary_form=True)
	if not der:
		raise DERError("no certificate")
	return not_after(der)


def is_public_name(domain):
	"""False for names that cannot carry a public certificate (localhost, bare names, IPs)."""
	d = (domain or "").strip().lower().rstrip(".")
	if not d or "." not in d or d == "localhost" or d.endswith(LOCAL_SUFFIXES):
		return False
	if any(d == r or d.endswith("." + r) for r in RESERVED):
		return False
	try:
		ipaddress.ip_address(d)
		return False
	except ValueError:
		return True


def site_domains(benches, extra=(), exclude=()):
	"""`{domain: (bench, site)}`: every public bench site and custom domain, plus `extra`.

	`extra` entries are names or `{"domain", "bench", "site"}` and are always checked.
	"""
	excluded = {e.lower() for e in exclude or ()}
	out = {}
	for bench in benches:
		for site in bench.get("sites") or []:
			if is_public_name(site):
				out.setdefault(site.lower(), (bench["name"], site))
		for alias, site in (bench.get("site_domains") or {}).items():
			if is_public_name(alias):
				out.setdefault(alias.lower(), (bench["name"], site))
	for item in extra or ():
		if isinstance(item, str):
			item = {"domain": item}
		if isinstance(item, dict) and item.get("domain"):
			out[item["domain"].lower()] = (item.get("bench"), item.get("site"))
	return {d: scope for d, scope in sorted(out.items()) if d not in excluded}


def tls_metrics(domains, host, now=None, port=443, timeout=5.0, connect_host=None, workers=8, fetch=fetch_not_after):
	"""`tls_days_left` per reachable domain (unreachable ones are logged and skipped)."""
	now = now or datetime.now(timezone.utc)
	ts = utc_iso(now)

	def check(item):
		domain, (bench, site) = item
		try:
			expiry = fetch(domain, port=port, timeout=timeout, connect_host=connect_host)
		except (OSError, ssl.SSLError, ValueError) as e:
			log.info("TLS check %s:%s skipped: %s", domain, port, e)
			return None
		days = round((expiry - now).total_seconds() / 86400.0, 2)
		return metric("tls_days_left", days, ts, host, bench=bench, site=site, labels={"domain": domain})

	items = list(domains.items())
	if not items:
		return []
	with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
		results = list(pool.map(check, items))
	return [m for m in results if m]
