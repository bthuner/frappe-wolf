"""Frappe/ERPNext versions against GitHub Security Advisories (no frappe imports).

The advisories are the repositories' own (GET /repos/{repo}/security-advisories, published
ones, readable without a token). Their version ranges are typed by hand and vary:
"<16.21.0", "< v15.120.0", "<= v16.18.2", ">= 15.0.0, < 15.123.0", "tbd", a bare "16.11.0".
An advisory usually has one range per supported major line ("<16.21.0" and "<15.111.0"):
an upper bound only applies to its own major line, so 15.200.0 is not caught by "<16.21.0",
and a range with an explicit lower bound applies as written. A version at or past the range's
patched version is never affected. Ranges that cannot be read ("tbd") are skipped.

A version some advisory applies to is affected, whatever its line. Otherwise, a major line no
advisory of the last year names (a fork such as Dodock, numbered differently, or a line out of
support) cannot be judged: that app is unknown, never safe.

Versions travel from the agent as the `app_version` metric, encoded as a number
(major * 1e6 + minor * 1e3 + patch) so a chart shows upgrades; `encode` and `decode` convert.
"""

import datetime
import json
import re
import urllib.request

# app name -> repository whose security advisories cover it
REPOS = {"frappe": "frappe/frappe", "erpnext": "frappe/erpnext"}
SEVERITIES = ("critical", "high", "medium", "low")
API = "https://api.github.com/repos/{repo}/security-advisories?state=published&per_page=100"
MAX_PAGES = 10
RECENT_DAYS = 365  # a major line no advisory of the last year names is judged unknown

_EXACT = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)\s*$")
_BOUND = re.compile(r"(<=|<|>=|>|=)?\s*v?(\d+)\.(\d+)\.(\d+)")


def parse_version(text):
	"""(major, minor, patch) of a release version; None for anything else (dev, develop, x)."""
	m = _EXACT.match(str(text or ""))
	return tuple(int(p) for p in m.groups()) if m else None


def encode(version):
	"""Number for a (major, minor, patch) version; None when a part does not fit."""
	major, minor, patch = version
	if not (0 <= minor < 1000 and 0 <= patch < 1000 and 0 <= major < 10000):
		return None
	return major * 1_000_000 + minor * 1_000 + patch


def decode(value):
	value = int(round(float(value)))
	return value // 1_000_000, value // 1_000 % 1_000, value % 1_000


def fmt(version):
	return ".".join(str(p) for p in version)


def parse_range(text):
	"""[(op, version)] of a vulnerable_version_range; None when it cannot be read."""
	text = str(text or "").strip()
	if not text:
		return None
	if _EXACT.match(text):
		return [("=", parse_version(text))]
	bounds = []
	for part in text.split(","):
		m = _BOUND.fullmatch(part.strip())
		if not m:
			return None
		op, *nums = m.groups()
		bounds.append((op or "=", tuple(int(n) for n in nums)))
	return bounds or None


def _match(version, op, bound):
	return {"<": version < bound, "<=": version <= bound, ">": version > bound, ">=": version >= bound, "=": version == bound}[op]


def entry_line(bounds):
	"""Major line an upper-bound-only range is limited to; None when it has a lower bound."""
	if any(op in (">", ">=") for op, _ in bounds):
		return None
	return bounds[0][1][0]


def affects(version, entry):
	"""Whether `version` is in `entry` ({range, patched}); None when the entry cannot be read."""
	bounds = parse_range(entry.get("range"))
	if bounds is None:
		return None
	line = entry_line(bounds)
	if line is not None and line != version[0]:
		return False
	patched = parse_version(entry.get("patched"))  # "> v16.18.2" says nothing usable
	if patched is not None and version >= patched and (line is None or patched[0] == version[0]):
		return False
	return all(_match(version, op, bound) for op, bound in bounds)


def lines(advisories, within_days=RECENT_DAYS):
	"""Major lines named by the readable ranges of the advisories published in the last
	`within_days` before the newest one: a line out of support stops being named."""
	dates = sorted(a["published"] for a in advisories if a.get("published"))
	since = None
	if dates and within_days:
		since = (datetime.date.fromisoformat(dates[-1]) - datetime.timedelta(days=within_days)).isoformat()
	out = set()
	for adv in advisories:
		if since and (adv.get("published") or "") < since:
			continue
		for entry in adv.get("ranges") or ():
			bounds = parse_range(entry.get("range"))
			if bounds:
				out.update(b[1][0] for b in bounds)
	return out


def compact(raw):
	"""What Wolf keeps of the API's advisories: published, not withdrawn."""
	out = []
	for adv in raw or ():
		if adv.get("withdrawn_at") or (adv.get("state") not in (None, "published")):
			continue
		ranges = [{"range": v.get("vulnerable_version_range"), "patched": v.get("patched_versions")}
		          for v in adv.get("vulnerabilities") or ()]  # fmt: skip
		out.append({
			"id": adv.get("ghsa_id"), "cve": adv.get("cve_id"), "severity": (adv.get("severity") or "unknown").lower(),
			"summary": (adv.get("summary") or "").strip()[:200], "url": adv.get("html_url"),
			"published": (adv.get("published_at") or "")[:10], "ranges": ranges,
		})  # fmt: skip
	return out


def _next_link(header):
	for part in (header or "").split(","):
		url, _, rel = part.partition(";")
		if 'rel="next"' in rel:
			return url.strip().strip("<>")
	return None


def fetch(repo, opener=urllib.request.urlopen, timeout=20):
	"""Published advisories of `repo`, every page. Raises OSError/ValueError on failure."""
	url, raw = API.format(repo=repo), []
	for _ in range(MAX_PAGES):
		req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "frappe-wolf"})
		with opener(req, timeout=timeout) as resp:
			page = json.loads(resp.read().decode("utf-8"))
			link = resp.headers.get("Link")
		if not isinstance(page, list):
			raise ValueError(f"unexpected answer from {repo}: {str(page)[:200]}")
		raw += page
		url = _next_link(link)
		if not url:
			break
	return compact(raw)


def assess(app, version, advisories):
	"""{app, version, status, found: [...], fixed_in, why} for one installed version.

	status: "affected" when an advisory applies; else "unknown" when no advisory of the last
	year names its major line (a fork such as Dodock, numbered differently, or a line out of
	support); else "clear"."""
	out = {"app": app, "version": fmt(version), "found": [], "fixed_in": None, "why": None}
	fixed = []
	for adv in advisories:
		hits = [e for e in adv.get("ranges") or () if affects(version, e)]
		if not hits:
			continue
		out["found"].append({k: adv.get(k) for k in ("id", "cve", "severity", "summary", "url")})
		for e in hits:
			p = parse_version(e.get("patched"))
			if p and p[0] == version[0]:
				fixed.append(p)
	rank = {s: i for i, s in enumerate(SEVERITIES)}
	out["found"].sort(key=lambda a: (rank.get(a["severity"], len(SEVERITIES)), a["id"] or ""))
	out["fixed_in"] = fmt(max(fixed)) if fixed else None
	if out["found"]:
		out["status"] = "affected"
		return out
	named = sorted(lines(advisories))
	if version[0] in named:
		out["status"] = "clear"
		return out
	out["status"] = "unknown"
	out["why"] = (f"{app} {fmt(version)}: the last year's advisories name {', '.join(f'{n}.x' for n in named) or 'no line'}, "
	              f"not {version[0]}.x (a fork such as Dodock, numbered differently, or a line out of support)")  # fmt: skip
	return out


def summary(result):
	"""'frappe 15.100.0: 12 advisories (2 critical, 5 high, 5 medium), fixed in 15.123.0'."""
	found = result["found"]
	if not found:
		return f"{result['app']} {result['version']}: no advisory applies"
	counts = {}
	for a in found:
		counts[a["severity"]] = counts.get(a["severity"], 0) + 1
	parts = ", ".join(f"{counts[s]} {s}" for s in (*SEVERITIES, "unknown") if counts.get(s))
	text = f"{result['app']} {result['version']}: {len(found)} advisor{'y' if len(found) == 1 else 'ies'} ({parts})"
	return text + (f", fixed in {result['fixed_in']}" if result["fixed_in"] else "")
