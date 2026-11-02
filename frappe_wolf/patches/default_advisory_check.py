from frappe_wolf.patches import default_signal_settings


def execute():
	"""check_security_advisories (on) gets its default on existing sites.

	default_signal_settings already ran on those sites, so it is run again under a new name."""
	default_signal_settings.execute()
