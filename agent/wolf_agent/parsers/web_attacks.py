"""Web attack probes in nginx access logs (scanners, injection attempts, exposed files).

`classify(method, target, status, user_agent)` returns the names of the probe rules a
request matches. The nginx parser aggregates matches per (site, client address) and ships
`security` events:

- `web_attack_probe`: requests that look like a scanner or an injection attempt
  (warning; critical once a client sends `CRITICAL_THRESHOLD` of them in one interval);
- `web_sensitive_file_exposed`: a request for a secret-bearing file (`.env`, `.git/`,
  database dumps, private keys) answered with a 2xx: the file was probably served (critical).

Patterns are deliberately conservative: a Frappe site never serves these paths, so a match
is a probe, not a false positive from normal traffic.
"""

import re
from urllib.parse import unquote_plus

# rule -> regex over the decoded, lower-cased request target (path and query)
_RULES = [
	# secret-bearing files: also checked for a 2xx answer (see SENSITIVE_RULES)
	("env_file", r"(?:^|/)\.env(?:\.[\w-]+)?(?:$|[?#])"),
	("vcs_metadata", r"(?:^|/)\.(?:git|svn|hg)(?:/|$)"),
	("secret_file", r"(?:^|/)(?:id_(?:rsa|dsa|ecdsa|ed25519)|\.htpasswd|\.aws/credentials|\.ssh/|"
	 r"site_config\.json|common_site_config\.json|wp-config\.php(?:\.bak|~)?|\.ds_store)(?:$|[?#])"),
	("backup_dump", r"(?:^|/)[\w.-]*\.(?:sql|sql\.gz|sqlite3?|bak|old|tar\.gz|tgz|zip)(?:$|[?#])"),
	# CMS / admin panels that do not exist on a Frappe bench
	("cms_probe", r"(?:^|/)(?:wp-(?:login|admin|includes|content)|xmlrpc\.php|wordpress/|joomla|"
	 r"administrator/index\.php|phpmyadmin|pma/|adminer(?:\.php)?|phpinfo\.php|server-status|"
	 r"vendor/phpunit|cgi-bin/|boaform|hnap1|actuator/|solr/admin|manager/html|console/|"
	 r"_ignition/|telescope/|owa/|autodiscover/autodiscover\.xml)"),
	("path_traversal", r"(?:\.\./|\.\.\\|/etc/(?:passwd|shadow|hosts)|c:\\windows|/proc/self/)"),
	("sql_injection", r"(?:union(?:\s|/\*.*?\*/)+(?:all\s+)?select|"
	 r"'\s*(?:or|and)\s*'?\d+'?\s*=\s*'?\d+|\bsleep\s*\(\s*\d+\s*\)|benchmark\s*\(|"
	 r"information_schema|waitfor\s+delay|;\s*drop\s+table)"),
	("xss", r"(?:<script|javascript:|onerror\s*=|onload\s*=|<svg[^>]*on\w+\s*=|document\.cookie)"),
	("command_injection", r"(?:;\s*(?:cat|wget|curl|uname|bash|sh)\b|;\s*id(?:\s|;|$)|\$\(|`[^`]*`|\|\s*(?:sh|bash)\b|"
	 r"\$\{jndi:|/bin/(?:sh|bash))"),
	("template_injection", r"(?:\{\{.*?\}\}|\$\{.*?\}|<%.*?%>)"),
]
_COMPILED = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in _RULES]

# Known offensive tooling identifying itself in the User-Agent.
_SCANNER_UA = re.compile(
	r"(?:sqlmap|nikto|nmap|masscan|zgrab|nuclei|wpscan|dirbuster|gobuster|feroxbuster|ffuf|"
	r"wfuzz|acunetix|nessus|openvas|qualys|hydra|jaeles|whatweb|httpx|interactsh)",
	re.IGNORECASE,
)

# Frappe serves user attachments (/files, /private/files) and authorized backup downloads
# (/backups, System Manager only): a .zip or .sql.gz there is normal traffic.
_DUMP_ALLOWED_PREFIXES = ("/files/", "/private/files/", "/backups/", "/assets/")
SENSITIVE_RULES = {"env_file", "vcs_metadata", "secret_file", "backup_dump"}
CRITICAL_THRESHOLD = 50
MAX_TARGET = 300

# MITRE ATT&CK techniques per rule, kept in the payload so a SOC can pivot on them.
ATTACK_TECHNIQUES = {
	"env_file": "T1552.001",
	"vcs_metadata": "T1213",
	"secret_file": "T1552.004",
	"backup_dump": "T1530",
	"cms_probe": "T1595.003",
	"path_traversal": "T1190",
	"sql_injection": "T1190",
	"xss": "T1189",
	"command_injection": "T1190",
	"template_injection": "T1190",
	"scanner_user_agent": "T1595.002",
}


def _decode(target):
	"""Percent-decode twice (double encoding is a classic evasion) and lower-case."""
	text = target or ""
	for _ in range(2):
		decoded = unquote_plus(text)
		if decoded == text:
			break
		text = decoded
	return text.lower()


def classify(method, target, status=None, user_agent=None):
	"""Probe rule names matched by one request (empty list for normal traffic)."""
	decoded = _decode(target)
	path = decoded.split("?", 1)[0]
	matched = []
	for name, rx in _COMPILED:
		# file and CMS rules look at the path only: a query string such as
		# `?file=report.zip` is not a probe for a backup
		subject = path if name in SENSITIVE_RULES or name == "cms_probe" else decoded
		if not rx.search(subject):
			continue
		if name == "backup_dump" and path.startswith(_DUMP_ALLOWED_PREFIXES):
			continue
		matched.append(name)
	if user_agent and _SCANNER_UA.search(user_agent):
		matched.append("scanner_user_agent")
	return matched


# Frappe routes that answer 200 with a page for any sub-path (Desk, the Wolf UI, the API's own
# errors): a 2xx there is an HTML page, not the file the probe asked for
CATCH_ALL_PREFIXES = ("/app/", "/wolf/", "/desk/", "/api/", "/helpdesk/", "/crm/", "/hrms/", "/insights/", "/drive/")


def exposed(rules, status, target=""):
	"""True when a secret-bearing file was answered with a 2xx outside a catch-all route."""
	if not (status and 200 <= status < 300 and SENSITIVE_RULES.intersection(rules)):
		return False
	path = _decode(target).split("?", 1)[0]
	return not path.startswith(CATCH_ALL_PREFIXES)
