// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.ui.form.on("Wolf Status Page", {
	refresh(frm) {
		if (frm.is_new()) return;
		const url = `/wolf-status/${encodeURIComponent(frm.doc.name)}`;
		frm.add_custom_button(__("Open public page"), () => window.open(url, "_blank", "noopener"));
		frm.set_intro(
			frm.doc.enabled
				? __("Public at {0}", [`<a href="${url}" target="_blank" rel="noopener">${frappe.utils.escape_html(window.location.origin + url)}</a>`])
				: __("Disabled: the public page answers 404 until you enable it."),
			frm.doc.enabled ? "green" : "orange"
		);
	},
});
