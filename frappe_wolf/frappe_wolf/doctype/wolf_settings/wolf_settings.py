# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WolfSettings(Document):
	@frappe.whitelist()
	def regenerate_registration_token(self):
		frappe.only_for(("System Manager", "Wolf Manager"))
		token = frappe.generate_hash(length=32)
		self.registration_token = token
		self.save()
		return token

	@frappe.whitelist()
	def get_install_command(self):
		frappe.only_for(("System Manager", "Wolf Manager"))
		token = self.get_password("registration_token", raise_exception=False)
		if not token:
			frappe.throw("Set or regenerate the registration token first")
		url = (self.public_url or frappe.utils.get_url()).rstrip("/")
		return f"curl -fsSL https://raw.githubusercontent.com/bthuner/frappe-wolf/main/agent/install.sh | sudo WOLF_URL={url} WOLF_TOKEN={token} bash"
