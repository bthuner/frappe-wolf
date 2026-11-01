from frappe_wolf.patches import default_signal_settings


def execute():
	"""capture_app_security (on), the IP reputation sources and the SIEM export settings get their
	defaults on existing sites.

	default_signal_settings already ran on those sites, so it is run again under a new name."""
	default_signal_settings.execute()
