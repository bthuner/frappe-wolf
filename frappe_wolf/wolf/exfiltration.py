"""Unusual data exports (MITRE T1567 / T1030): what a request reads out, and when a user's volume
is unusual for them (no frappe imports; api/exfiltration.py counts and alerts).

- `classify(path, http_method, args)`: whether a request takes data out, and how. Exports
  (report export, Data Export, a Data Import template with records, bulk PDF) and list reads
  (`frappe.client.get_list`, the list and report views, `/api/resource/<DocType>`). Every list
  read counts, whatever its page size: a script paging through a table in small pages adds up
  like one big read. Single documents and everything else return None.
- `rows_in(message)`: the rows a list read returned, when its response says so.
- `verdict(hour_bytes, baseline, ...)`: None, "warning" or "critical" for a user's volume this
  hour, against their largest hour over the baseline window.
"""

# method -> what it is, for the event; download_template only with records (see classify)
EXPORT_METHODS = {
	"frappe.desk.reportview.export_query": "report export",
	"frappe.desk.query_report.export_query": "report export",
	"frappe.core.doctype.data_export.exporter.export_data": "Data Export",
	"frappe.core.doctype.data_import.data_import.download_template": "Data Import export",
	"frappe.utils.print_format.download_multi_pdf": "bulk PDF",
}
LIST_METHODS = {
	"frappe.client.get_list": "list read",
	"frappe.desk.reportview.get": "list view",
	"frappe.desk.reportview.get_list": "list read",
}
# download_template without records is an empty template
EMPTY_TEMPLATE = ("", None, "blank_template")

DEFAULT_FLOOR_MB = 50
DEFAULT_FACTOR = 5
CRITICAL_FACTOR = 20
BASELINE_DAYS = 14


def _method(path, args):
	path = path or ""
	if path.startswith("/api/method/"):
		return path[len("/api/method/"):].strip("/")
	return (args or {}).get("cmd") or None


def _resource_doctype(path):
	"""`/api/resource/<DocType>` and `/api/v2/document/<DocType>`: a list, not one document."""
	for prefix in ("/api/resource/", "/api/v2/document/"):
		if (path or "").startswith(prefix):
			parts = [p for p in path[len(prefix):].split("/") if p]
			return parts[0] if len(parts) == 1 else None
	return None


def classify(path, http_method, args):
	"""{"kind": "export" | "list", "label": ..., "method": ..., "doctype": ...} or None."""
	args = args or {}
	method = _method(path, args)
	doctype = args.get("doctype") or args.get("report_name") or args.get("doctype_name")
	if method in EXPORT_METHODS:
		if method.endswith("download_template") and args.get("export_records") in EMPTY_TEMPLATE:
			return None
		return {"kind": "export", "label": EXPORT_METHODS[method], "method": method, "doctype": doctype}
	if method in LIST_METHODS:
		return {"kind": "list", "label": LIST_METHODS[method], "method": method, "doctype": doctype}
	resource = _resource_doctype(path) if (http_method or "GET").upper() == "GET" else None
	if resource:
		return {"kind": "list", "label": "REST list", "method": None, "doctype": resource}
	return None


def rows_in(message):
	"""Rows in a list read's message: a list, or the list view's {"keys", "values"}."""
	if isinstance(message, list):
		return len(message)
	if isinstance(message, dict) and isinstance(message.get("values"), list):
		return len(message["values"])
	return None


def verdict(hour_bytes, baseline, floor_bytes, factor=DEFAULT_FACTOR, critical_factor=CRITICAL_FACTOR, learning=False):
	"""None, "warning" or "critical" for a user's volume this hour.

	baseline: their largest hour over the baseline window, None without any. Below the floor
	nothing counts. A user without history is critical, except while the site itself has less
	than a full window of history (`learning`): then every user lacks one, and it is a warning.
	"""
	if hour_bytes < floor_bytes:
		return None
	if not baseline:
		return "warning" if learning else "critical"
	ratio = hour_bytes / baseline
	if ratio >= critical_factor:
		return "critical"
	if ratio >= factor:
		return "warning"
	return None


def megabytes(n):
	return round((n or 0) / 1e6, 1)
