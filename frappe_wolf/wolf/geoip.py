"""Offline IP → country lookup (no frappe imports, unit tested). Never calls a network service.

Two database formats are read, chosen by file extension:

- **MaxMind DB** (`.mmdb`: GeoLite2-Country/City, DB-IP Lite, IPinfo Lite ...) through the
  minimal reader below (search tree + data section decoder of the MaxMind DB format v2).
  The country is `country.iso_code`, else `registered_country.iso_code`, else `country_code`.
- **CSV** ranges, one per line, with an optional header:
  `start_ip,end_ip,CC[,...]` (DB-IP / IPinfo "country lite" CSV), the same with integer
  addresses (IP2Location LITE DB1), or `network/prefix,CC`.
"""

import bisect
import csv
import ipaddress
import mmap
import os
import struct
import threading

METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"
_V4_MAPPED = 0xFFFF00000000
_UNKNOWN = {"", "-", "ZZ", "XX", "--"}


class GeoIPError(ValueError):
	pass


class _Decoder:
	"""MaxMind DB data section decoder. Offsets are absolute in `buf`; pointers are relative to `base`."""

	def __init__(self, buf, base):
		self.buf = buf
		self.base = base

	def _uint(self, off, size):
		return int.from_bytes(self.buf[off : off + size], "big"), off + size

	def decode(self, off, depth=0):
		if depth > 64:
			raise GeoIPError("data nesting too deep")
		buf = self.buf
		ctrl = buf[off]
		off += 1
		kind = ctrl >> 5
		if kind == 1:  # pointer
			ss, v = (ctrl >> 3) & 0x3, ctrl & 0x7
			if ss == 0:
				ptr, off = (v << 8) | buf[off], off + 1
			elif ss == 1:
				ptr, off = ((v << 16) | int.from_bytes(buf[off : off + 2], "big")) + 2048, off + 2
			elif ss == 2:
				ptr, off = ((v << 24) | int.from_bytes(buf[off : off + 3], "big")) + 526336, off + 3
			else:
				ptr, off = int.from_bytes(buf[off : off + 4], "big"), off + 4
			value, _ = self.decode(self.base + ptr, depth + 1)
			return value, off
		if kind == 0:  # extended type
			kind = 7 + buf[off]
			off += 1
		size = ctrl & 0x1F
		if size >= 29:
			n = size - 28
			size = (29, 285, 65821)[n - 1] + int.from_bytes(buf[off : off + n], "big")
			off += n
		if kind == 2:
			return buf[off : off + size].decode("utf-8", errors="replace"), off + size
		if kind == 3:
			return struct.unpack(">d", buf[off : off + 8])[0], off + 8
		if kind == 4:
			return bytes(buf[off : off + size]), off + size
		if kind in (5, 6, 9, 10):
			return self._uint(off, size)
		if kind == 7:
			out = {}
			for _ in range(size):
				key, off = self.decode(off, depth + 1)
				out[key], off = self.decode(off, depth + 1)
			return out, off
		if kind == 8:
			value, off = self._uint(off, size)
			if size == 4 and value >= 2**31:
				value -= 2**32
			return value, off
		if kind == 11:
			items = []
			for _ in range(size):
				item, off = self.decode(off, depth + 1)
				items.append(item)
			return items, off
		if kind == 14:
			return bool(size), off
		if kind == 15:
			return struct.unpack(">f", buf[off : off + 4])[0], off + 4
		if kind in (12, 13):
			return None, off
		raise GeoIPError(f"unknown data type {kind}")


class MMDBReader:
	def __init__(self, path):
		with open(path, "rb") as f:
			self.buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
		start = self.buf.rfind(METADATA_MARKER, max(0, len(self.buf) - 128 * 1024))
		if start < 0:
			raise GeoIPError(f"{path}: not a MaxMind DB file")
		meta_start = start + len(METADATA_MARKER)
		meta, _ = _Decoder(self.buf, meta_start).decode(meta_start)
		try:
			self.node_count = int(meta["node_count"])
			self.record_size = int(meta["record_size"])
			self.ip_version = int(meta["ip_version"])
		except (KeyError, TypeError, ValueError):
			raise GeoIPError(f"{path}: incomplete metadata") from None
		if self.record_size not in (24, 28, 32):
			raise GeoIPError(f"{path}: unsupported record size {self.record_size}")
		self.metadata = meta
		self.node_bytes = self.record_size * 2 // 8
		tree_size = self.node_bytes * self.node_count
		self.data = _Decoder(self.buf, tree_size + 16)
		self._ipv4_start = None

	def _record(self, node, bit):
		buf, base = self.buf, node * self.node_bytes
		if self.record_size == 24:
			off = base + bit * 3
			return int.from_bytes(buf[off : off + 3], "big")
		if self.record_size == 28:
			middle = buf[base + 3]
			if bit == 0:
				return ((middle & 0xF0) << 20) | int.from_bytes(buf[base : base + 3], "big")
			return ((middle & 0x0F) << 24) | int.from_bytes(buf[base + 4 : base + 7], "big")
		off = base + bit * 4
		return int.from_bytes(buf[off : off + 4], "big")

	def _v4_root(self):
		if self._ipv4_start is None:
			node = 0
			for _ in range(96):
				if node >= self.node_count:
					break
				node = self._record(node, 0)
			self._ipv4_start = node
		return self._ipv4_start

	def lookup(self, ip):
		"""The data record for `ip`, or None."""
		addr = ipaddress.ip_address(ip)
		if addr.version == 6 and addr.ipv4_mapped:
			addr = addr.ipv4_mapped
		if addr.version == 6 and self.ip_version == 4:
			return None
		node = self._v4_root() if addr.version == 4 and self.ip_version == 6 else 0
		packed = addr.packed
		for i in range(len(packed) * 8):
			if node >= self.node_count:
				break
			node = self._record(node, (packed[i >> 3] >> (7 - (i & 7))) & 1)
		if node <= self.node_count:
			return None  # == node_count: no data; < node_count: ran out of bits
		value, _ = self.data.decode(self.data.base + node - self.node_count - 16)
		return value

	def country(self, ip):
		record = self.lookup(ip)
		if not isinstance(record, dict):
			return None
		for key in ("country", "registered_country"):
			code = (record.get(key) or {}).get("iso_code") if isinstance(record.get(key), dict) else None
			if code:
				return str(code).upper()
		code = record.get("country_code")
		return str(code).upper() if code else None

	def close(self):
		self.buf.close()


def _addr_int(text):
	"""An address as an int in one IPv6 space (IPv4 → ::ffff:a.b.c.d)."""
	text = text.strip()
	if text.isdigit():
		value = int(text)
		return value + _V4_MAPPED if value <= 0xFFFFFFFF else value
	addr = ipaddress.ip_address(text)
	if addr.version == 4:
		return int(addr) + _V4_MAPPED
	return int(addr)


class CSVRanges:
	def __init__(self, path):
		rows = []
		with open(path, newline="", encoding="utf-8", errors="replace") as f:
			for fields in csv.reader(f):
				row = self._parse(fields)
				if row:
					rows.append(row)
		if not rows:
			raise GeoIPError(f"{path}: no IP range found")
		rows.sort()
		self.starts = [r[0] for r in rows]
		self.ends = [r[1] for r in rows]
		self.countries = [r[2] for r in rows]

	@staticmethod
	def _parse(fields):
		fields = [f.strip() for f in fields]
		try:
			if len(fields) >= 2 and "/" in fields[0]:
				net = ipaddress.ip_network(fields[0], strict=False)
				start, end, code = _addr_int(str(net[0])), _addr_int(str(net[-1])), fields[1]
			elif len(fields) >= 3:
				start, end, code = _addr_int(fields[0]), _addr_int(fields[1]), fields[2]
			else:
				return None
		except ValueError:
			return None  # header or comment line
		code = code.upper()
		return (start, end, None if code in _UNKNOWN else code)

	def country(self, ip):
		value = _addr_int(str(ipaddress.ip_address(ip)))
		i = bisect.bisect_right(self.starts, value) - 1
		if i >= 0 and value <= self.ends[i]:
			return self.countries[i]
		return None


_cache = {}
_lock = threading.Lock()


def open_database(path):
	"""A reader for `path`, cached per process until the file changes."""
	st = os.stat(path)
	key = (path, st.st_mtime, st.st_size)
	with _lock:
		reader = _cache.get(path)
		if reader and reader[0] == key:
			return reader[1]
		db = MMDBReader(path) if path.lower().endswith(".mmdb") else CSVRanges(path)
		if reader and isinstance(reader[1], MMDBReader):
			reader[1].close()
		_cache[path] = (key, db)
		return db


def country(path, ip):
	"""ISO country code of `ip`, or None (private address, unknown, unreadable database)."""
	if not path or not ip:
		return None
	try:
		addr = ipaddress.ip_address(str(ip).strip())
	except ValueError:
		return None
	if addr.version == 6 and addr.ipv4_mapped:
		addr = addr.ipv4_mapped
	if not addr.is_global:
		return None
	return open_database(path).country(str(addr))
