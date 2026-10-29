// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.ui.form.on("Wolf Postmortem", {
	refresh(frm) {
		if (frm.doc.incident) {
			frm.add_custom_button(__("Incident"), () => frappe.set_route("Form", "Wolf Incident", frm.doc.incident));
		}
	},
});
