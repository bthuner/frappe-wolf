// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.listview_settings["Wolf Silence"] = {
	add_fields: ["starts_at", "ends_at"],
	get_indicator(doc) {
		const now = frappe.datetime.now_datetime();
		if (doc.ends_at <= now) return [__("Expired"), "gray", `ends_at,<=,${now}`];
		if (doc.starts_at > now) return [__("Scheduled"), "blue", `starts_at,>,${now}`];
		return [__("Active"), "orange", `ends_at,>,${now}`];
	},
};
