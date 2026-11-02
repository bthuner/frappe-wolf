"""MITRE ATT&CK mapping of Wolf security signals (no frappe imports).

Each security category Wolf raises maps to the ATT&CK (Enterprise) technique it is evidence
of. At ingest, `enrich(category, payload)` adds a `mitre_attack` list to the event payload,
so an analyst (or a SIEM the events are forwarded to) can pivot on technique and tactic
without knowing Wolf's category names. Agents may also send `attack_techniques` (a list of
technique IDs) in the payload; known IDs are named, unknown ones are kept as sent.
"""

import re

# technique id -> (name, tactic)
TECHNIQUES = {
	"T1078": ("Valid Accounts", "initial-access"),
	"T1078.003": ("Valid Accounts: Local Accounts", "privilege-escalation"),
	"T1078.004": ("Valid Accounts: Cloud Accounts", "initial-access"),
	"T1083": ("File and Directory Discovery", "discovery"),
	"T1059.006": ("Command and Scripting Interpreter: Python", "execution"),
	"T1059.007": ("Command and Scripting Interpreter: JavaScript", "execution"),
	"T1098": ("Account Manipulation", "persistence"),
	"T1098.001": ("Account Manipulation: Additional Cloud Credentials", "persistence"),
	"T1110": ("Brute Force", "credential-access"),
	"T1136.001": ("Create Account: Local Account", "persistence"),
	"T1189": ("Drive-by Compromise", "initial-access"),
	"T1190": ("Exploit Public-Facing Application", "initial-access"),
	"T1213": ("Data from Information Repositories", "collection"),
	"T1530": ("Data from Cloud Storage", "collection"),
	"T1548.003": ("Abuse Elevation Control Mechanism: Sudo and Sudo Caching", "privilege-escalation"),
	"T1552": ("Unsecured Credentials", "credential-access"),
	"T1552.001": ("Unsecured Credentials: Credentials In Files", "credential-access"),
	"T1552.004": ("Unsecured Credentials: Private Keys", "credential-access"),
	"T1562.001": ("Impair Defenses: Disable or Modify Tools", "defense-evasion"),
	"T1567": ("Exfiltration Over Web Service", "exfiltration"),
	"T1030": ("Data Transfer Size Limits", "exfiltration"),
	"T1595.002": ("Active Scanning: Vulnerability Scanning", "reconnaissance"),
	"T1595.003": ("Active Scanning: Wordlist Scanning", "reconnaissance"),
}

# Wolf category -> technique ids
CATEGORIES = {
	# host (agent, auth log)
	"ssh_failed_login": ["T1110"],
	"ssh_root_login": ["T1078.003"],
	"ssh_login_after_failures": ["T1110", "T1078"],
	"sudo_failure": ["T1548.003"],
	"account_created": ["T1136.001"],
	"admin_group_change": ["T1098"],
	# web (agent, nginx)
	"web_attack_probe": ["T1595.003"],
	# Suricata (agent, #72): the techniques come from each rule's metadata, sent by the agent
	"network_ids_alert": [],
	"web_sensitive_file_exposed": ["T1552.001"],
	# Frappe site (local signals)
	"frappe_failed_login": ["T1110"],
	"new_admin_ip": ["T1078"],
	"admin_login_new_country": ["T1078"],
	"privileged_user": ["T1098"],
	# Frappe application security (local signals)
	"server_script_change": ["T1059.006"],
	"client_script_change": ["T1059.007"],
	"console_execution": ["T1059.006"],
	"permission_change": ["T1098"],
	"security_settings_weakened": ["T1562.001"],
	"api_key_generated": ["T1098.001"],
	"api_key_new_ip": ["T1078", "T1552"],
	"webhook_external": ["T1567"],
	"data_exfiltration": ["T1567", "T1030"],
	# exposure (agent): weaknesses, mapped to what they open the door to
	"service_exposed": ["T1190"],
	"insecure_site_config": ["T1190"],
	# deception (api/threat.py): the payload adds the technique of the decoy kind
	"honeytoken_triggered": [],
	# compliance reminders (api/compliance.py): no attack technique
	"regulatory_deadline": [],
}

_ID_RE = re.compile(r"^T\d{4}(?:\.\d{3})?$")
MAX_TECHNIQUES = 10


def techniques_for(category, sent=None):
	"""Technique ids for a category plus valid ids sent by the agent, deduplicated, in order."""
	ids = []
	for tid in [*CATEGORIES.get(category or "", []), *(sent if isinstance(sent, list) else [])]:
		tid = str(tid).strip().upper()
		if _ID_RE.match(tid) and tid not in ids:
			ids.append(tid)
	return ids[:MAX_TECHNIQUES]


def describe(tid):
	name, tactic = TECHNIQUES.get(tid, (None, None))
	return {"id": tid, "name": name, "tactic": tactic}


def enrich(category, payload):
	"""Return `payload` with a `mitre_attack` list, or unchanged when nothing maps.

	Never overwrites a `mitre_attack` key the sender set itself.
	"""
	if isinstance(payload, dict) and "mitre_attack" in payload:
		return payload
	ids = techniques_for(category, (payload or {}).get("attack_techniques") if isinstance(payload, dict) else None)
	if not ids:
		return payload
	enriched = dict(payload or {})
	enriched["mitre_attack"] = [describe(tid) for tid in ids]
	return enriched

