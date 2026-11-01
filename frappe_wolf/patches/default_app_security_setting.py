from frappe_wolf.patches import default_signal_settings


def execute():
	"""capture_app_security (Frappe application security signals) defaults to on for existing sites.

	default_signal_settings already ran on those sites, so it is run again under a new name."""
	default_signal_settings.execute()
