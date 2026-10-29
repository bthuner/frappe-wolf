// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.ui.form.on("Wolf Agent", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Rotate secret"), () =>
			frappe.confirm(__("The agent will stop sending until it is registered again. Continue?"), () =>
				frm.call("rotate_secret").then(() => frm.reload_doc())
			)
		);
	},
});
