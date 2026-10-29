// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.ui.form.on("Wolf Event", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.incident) {
			frm.add_custom_button(__("Incident"), () => frappe.set_route("Form", "Wolf Incident", frm.doc.incident));
			return;
		}
		if (frappe.user.has_role(["Wolf Manager", "System Manager"])) {
			frm.add_custom_button(__("Open incident from this event"), () =>
				frappe
					.call({
						method: "frappe_wolf.api.incident.open_from_event",
						args: { event: frm.doc.name },
						freeze: true,
					})
					.then(({ message }) => frappe.set_route("Form", "Wolf Incident", message))
			);
		}
	},
});
