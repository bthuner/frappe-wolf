"""Host metrics read straight from /proc and statvfs."""

import os
import time

from .util import metric, utc_iso

PSEUDO_FS = {
	"autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts",
	"devtmpfs", "efivarfs", "fusectl", "fuse.gvfsd-fuse", "fuse.portal", "hugetlbfs", "mqueue",
	"nsfs", "overlay", "proc", "pstore", "ramfs", "rpc_pipefs", "securityfs", "squashfs",
	"sysfs", "tmpfs", "tracefs", "nfsd", "fuse.lxcfs",
}
SKIP_MOUNT_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker", "/var/lib/containers")


def read_cpu_times(proc="/proc"):
	"""Return `(idle, total)` jiffies from the aggregate cpu line of /proc/stat."""
	with open(os.path.join(proc, "stat")) as f:
		for line in f:
			if line.startswith("cpu "):
				values = [int(v) for v in line.split()[1:]]
				idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
				# guest times are already included in user/nice
				return idle, sum(values[:8])
	raise ValueError("no cpu line in /proc/stat")


def cpu_usage(prev, cur):
	idle = cur[0] - prev[0]
	total = cur[1] - prev[1]
	if total <= 0:
		return 0.0
	return round(100.0 * (total - idle) / total, 2)


def read_meminfo(proc="/proc"):
	info = {}
	with open(os.path.join(proc, "meminfo")) as f:
		for line in f:
			key, _, rest = line.partition(":")
			parts = rest.split()
			if parts:
				info[key] = int(parts[0])
	return info


def memory_percentages(info):
	out = {}
	total = info.get("MemTotal", 0)
	if total:
		available = info.get("MemAvailable")
		if available is None:  # very old kernels
			available = info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0)
		out["memory_used_percent"] = round(100.0 * (total - available) / total, 2)
	swap_total = info.get("SwapTotal", 0)
	if swap_total:
		out["swap_used_percent"] = round(100.0 * (swap_total - info.get("SwapFree", 0)) / swap_total, 2)
	return out


def read_loadavg(proc="/proc"):
	with open(os.path.join(proc, "loadavg")) as f:
		parts = f.read().split()
	return float(parts[0]), float(parts[1]), float(parts[2])


def real_mounts(proc="/proc"):
	"""Mount points backed by real block devices, one per device."""
	seen, mounts = set(), []
	try:
		with open(os.path.join(proc, "mounts")) as f:
			lines = f.readlines()
	except OSError:
		return ["/"]
	for line in lines:
		parts = line.split()
		if len(parts) < 3:
			continue
		device, mount, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
		if fstype in PSEUDO_FS or fstype.startswith("fuse."):
			continue
		if any(mount == p or mount.startswith(p + "/") for p in SKIP_MOUNT_PREFIXES):
			continue
		if device in seen:
			continue
		seen.add(device)
		mounts.append(mount)
	return mounts or ["/"]


def disk_used_percent(mount):
	st = os.statvfs(mount)
	total = st.f_blocks * st.f_frsize
	if not total:
		return None
	# same definition as df: used / (used + available to non-root)
	used = (st.f_blocks - st.f_bfree) * st.f_frsize
	avail = st.f_bavail * st.f_frsize
	return round(100.0 * used / (used + avail), 2) if used + avail else None


class HostMetrics:
	def __init__(self, host, proc="/proc"):
		self.host = host
		self.proc = proc
		self.prev_cpu = None

	def collect(self, now=None):
		ts = utc_iso(now)
		out = []

		def add(name, value, labels=None):
			out.append(metric(name, value, ts, self.host, labels=labels))

		try:
			cur = read_cpu_times(self.proc)
			if self.prev_cpu is None:
				# first cycle: take a short second sample so `check` reports CPU too
				time.sleep(0.25)
				self.prev_cpu, cur = cur, read_cpu_times(self.proc)
			add("cpu_usage", cpu_usage(self.prev_cpu, cur))
			self.prev_cpu = cur
		except (OSError, ValueError):
			pass

		try:
			for name, value in memory_percentages(read_meminfo(self.proc)).items():
				add(name, value)
		except OSError:
			pass

		try:
			l1, l5, l15 = read_loadavg(self.proc)
			add("load_1m", l1)
			add("load_5m", l5)
			add("load_15m", l15)
			add("load_per_core", round(l1 / (os.cpu_count() or 1), 3))
		except (OSError, ValueError, IndexError):
			pass

		for mount in real_mounts(self.proc):
			try:
				value = disk_used_percent(mount)
			except OSError:
				continue
			if value is not None:
				add("disk_used_percent", value, {"mount": mount})
		return out
