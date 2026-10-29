# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from frappe_wolf.wolf import agent_secrets


class WolfAgent(Document):
	@frappe.whitelist()
	def rotate_secret(self):
		"""Replace the secret: the old one stops working now. The new one is returned once and
		only its hash is kept, so put it in the agent's config (or re-register the agent)."""
		frappe.only_for(("System Manager", "Wolf Manager"))
		secret = agent_secrets.new_secret()
		# db_set: secret_hash is a permlevel 1 field nobody has write access to
		self.db_set({"secret_hash": agent_secrets.hash_secret(secret), "secret_issued_at": now_datetime()})
		return {"agent_id": self.name, "agent_secret": secret}
