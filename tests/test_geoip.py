import ipaddress
import os
import struct
import tempfile
import unittest

from frappe_wolf.wolf import geoip

# --- a tiny MaxMind DB writer, just enough to build fixtures for the reader -----------------


class Pointer:
	def __init__(self, offset):
		self.offset = offset


def _ctrl(kind, size):
	if size < 29:
		head, extra = size, b""
	elif size < 285:
		head, extra = 29, bytes([size - 29])
	elif size < 65821:
		head, extra = 30, (size - 285).to_bytes(2, "big")
	else:
		head, extra = 31, (size - 65821).to_bytes(3, "big")
	if kind <= 7:
		return bytes([(kind << 5) | head]) + extra
	return bytes([head, kind - 7]) + extra


def encode(value):
	if isinstance(value, Pointer):
		p = value.offset
		if p < 2048:
			return bytes([0x20 | (p >> 8), p & 0xFF])
		p -= 2048
		return bytes([0x28 | (p >> 16)]) + (p & 0xFFFF).to_bytes(2, "big")
	if isinstance(value, bool):
		return _ctrl(14, int(value))
	if isinstance(value, str):
		data = value.encode()
		return _ctrl(2, len(data)) + data
	if isinstance(value, float):
		return _ctrl(3, 8) + struct.pack(">d", value)
	if isinstance(value, int):
		data = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
		kind = 5 if len(data) <= 2 else 6 if len(data) <= 4 else 9
		return _ctrl(kind, len(data)) + data
	if isinstance(value, dict):
		return _ctrl(7, len(value)) + b"".join(encode(k) + encode(v) for k, v in value.items())
	if isinstance(value, list):
		return _ctrl(11, len(value)) + b"".join(encode(v) for v in value)
	raise TypeError(value)


def build_mmdb(path, networks, ip_version=6, record_size=24):
	"""networks: [(cidr, record)]; a record may contain Pointer(offset of an earlier record)."""
	data, offsets = b"", []
	for _, record in networks:
		offsets.append(len(data))
		data += encode(record)
	nodes = [[None, None]]
	for (cidr, _), offset in zip(networks, offsets, strict=True):
		net = ipaddress.ip_network(cidr)
		bits = format(int(net.network_address), f"0{net.max_prefixlen}b")[: net.prefixlen]
		if net.version == 4 and ip_version == 6:
			bits = "0" * 96 + bits
		node = 0
		for i, bit in enumerate(bits):
			b = int(bit)
			if i == len(bits) - 1:
				nodes[node][b] = ("data", offset)
			else:
				if nodes[node][b] is None:
					nodes.append([None, None])
					nodes[node][b] = ("node", len(nodes) - 1)
				node = nodes[node][b][1]
	count = len(nodes)

	def value(rec):
		if rec is None:
			return count
		return rec[1] if rec[0] == "node" else count + 16 + rec[1]

	tree = b""
	for left, right in nodes:
		lv, rv = value(left), value(right)
		if record_size == 24:
			tree += lv.to_bytes(3, "big") + rv.to_bytes(3, "big")
		elif record_size == 28:
			tree += (lv & 0xFFFFFF).to_bytes(3, "big") + bytes([((lv >> 24) << 4) | (rv >> 24)]) + (rv & 0xFFFFFF).to_bytes(3, "big")
		else:
			tree += lv.to_bytes(4, "big") + rv.to_bytes(4, "big")
	meta = {
		"node_count": count, "record_size": record_size, "ip_version": ip_version,
		"database_type": "Wolf-Test-Country", "binary_format_major_version": 2,
		"binary_format_minor_version": 0, "build_epoch": 1_800_000_000, "languages": ["en"],
	}  # fmt: skip
	with open(path, "wb") as f:
		f.write(tree + b"\x00" * 16 + data + geoip.METADATA_MARKER + encode(meta))


FR = {"country": {"iso_code": "FR", "names": {"en": "France"}}, "continent": {"code": "EU"}}
DE_REGISTERED = {"registered_country": {"iso_code": "de"}, "weight": 1.5, "flag": True, "tags": ["a", "b"]}


class TestMMDB(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()

	def tearDown(self):
		geoip._cache.clear()
		self.tmp.cleanup()

	def build(self, name, **kw):
		path = os.path.join(self.tmp.name, name)
		# this record's "country" is a pointer to the FR record's country map: the FR record is
		# first in the data section (offset 0) and its value starts after the map header + key
		shared = {"country": Pointer(1 + len(encode("country")))}
		build_mmdb(path, [("81.2.69.0/24", FR), ("89.160.20.0/22", DE_REGISTERED), ("2a02:8070::/32", shared),
		                  ("8.8.8.0/24", {"country_code": "us"})], **kw)  # fmt: skip
		return path

	def test_lookups_for_every_record_size(self):
		for record_size in (24, 28, 32):
			path = self.build(f"db{record_size}.mmdb", record_size=record_size)
			reader = geoip.MMDBReader(path)
			self.assertEqual(reader.metadata["database_type"], "Wolf-Test-Country")
			self.assertEqual(reader.country("81.2.69.160"), "FR")
			self.assertEqual(reader.country("89.160.23.1"), "DE")  # registered_country fallback
			self.assertEqual(reader.country("2a02:8070:1::1"), "FR")  # through the pointer
			self.assertEqual(reader.country("::ffff:81.2.69.1"), "FR")  # IPv4-mapped IPv6
			self.assertEqual(reader.country("8.8.8.8"), "US")
			self.assertIsNone(reader.country("1.1.1.1"))
			rec = reader.lookup("89.160.20.5")
			self.assertEqual((rec["weight"], rec["flag"], rec["tags"]), (1.5, True, ["a", "b"]))
			reader.close()

	def test_ipv4_only_database(self):
		path = os.path.join(self.tmp.name, "v4.mmdb")
		build_mmdb(path, [("81.2.69.0/24", FR)], ip_version=4)
		reader = geoip.MMDBReader(path)
		self.assertEqual(reader.country("81.2.69.1"), "FR")
		self.assertIsNone(reader.country("2a02:8070::1"))
		reader.close()

	def test_not_a_database(self):
		path = os.path.join(self.tmp.name, "junk.mmdb")
		with open(path, "wb") as f:
			f.write(b"\x00" * 64)
		with self.assertRaises(geoip.GeoIPError):
			geoip.MMDBReader(path)

	def test_country_helper_skips_private_and_caches(self):
		path = self.build("c.mmdb")
		self.assertEqual(geoip.country(path, "81.2.69.10"), "FR")
		self.assertIs(geoip.open_database(path), geoip.open_database(path))
		for ip in ("10.0.0.1", "127.0.0.1", "192.168.1.1", "::1", "fe80::1", "not-an-ip", "", None):
			self.assertIsNone(geoip.country(path, ip), ip)
		self.assertIsNone(geoip.country(None, "81.2.69.10"))


class TestCSV(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()

	def tearDown(self):
		geoip._cache.clear()
		self.tmp.cleanup()

	def write(self, name, text):
		path = os.path.join(self.tmp.name, name)
		with open(path, "w") as f:
			f.write(text)
		return path

	def test_dbip_style_ranges(self):
		path = self.write("dbip.csv", (
			"start_ip,end_ip,country\n"
			"81.2.69.0,81.2.69.255,FR\n"
			"89.160.20.0,89.160.23.255,DE\n"
			"2a02:8070::,2a02:8070:ffff:ffff:ffff:ffff:ffff:ffff,DE\n"
			"8.8.8.0,8.8.8.255,ZZ\n"
		))  # fmt: skip
		self.assertEqual(geoip.country(path, "81.2.69.200"), "FR")
		self.assertEqual(geoip.country(path, "89.160.21.1"), "DE")
		self.assertEqual(geoip.country(path, "2a02:8070:42::1"), "DE")
		self.assertIsNone(geoip.country(path, "8.8.8.8"))  # unknown country code
		self.assertIsNone(geoip.country(path, "81.2.70.1"))  # between ranges

	def test_ip2location_integers_and_cidr(self):
		path = self.write("ip2l.csv", '"1359103232","1359103487","FR","France"\n')  # 81.2.69.0/24
		self.assertEqual(geoip.country(path, "81.2.69.7"), "FR")
		path = self.write("cidr.csv", "network,country\n81.2.69.0/24,fr\n2a02:8070::/32,DE\n")
		self.assertEqual(geoip.country(path, "81.2.69.7"), "FR")
		self.assertEqual(geoip.country(path, "2a02:8070::9"), "DE")

	def test_empty_file(self):
		path = self.write("empty.csv", "nothing here\n")
		with self.assertRaises(geoip.GeoIPError):
			geoip.country(path, "81.2.69.7")


if __name__ == "__main__":
	unittest.main()
