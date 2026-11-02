from frappe_wolf.patches import default_signal_settings


def execute():
	"""The CrowdSec settings get their defaults on existing sites (CrowdSec itself stays off).

	default_signal_settings already ran on those sites, so it is run again under a new name."""
	default_signal_settings.execute()
