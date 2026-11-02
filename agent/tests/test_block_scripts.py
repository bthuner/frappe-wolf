"""wolf-block-fail2ban.sh, run for real with a stub fail2ban-client and systemd-run in PATH."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "wolf-block-fail2ban.sh")

STUB = """#!/bin/sh
echo "$@" >> "{log}"
[ "$1" = status ] && [ "{jail}" = yes ] && exit 0
[ "$1" = status ] && exit 255
exit 0
"""


def read(path):
	with open(path) as f:
		return f.read()


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class TestFail2banHelper(unittest.TestCase):
	def setUp(self):
		self.dir = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, self.dir)
		self.state = os.path.join(self.dir, "state", "fail2ban.state")
		self.log = os.path.join(self.dir, "calls.log")
		with open(SCRIPT) as f:
			script = f.read().replace("STATE=/var/lib/wolf-block/fail2ban.state", f"STATE={self.state}")
		self.script = os.path.join(self.dir, "wolf-block-fail2ban")
		with open(self.script, "w") as f:
			f.write(script)
		os.chmod(self.script, 0o755)
		self.bin = os.path.join(self.dir, "bin")
		os.mkdir(self.bin)

		# always shadow the host's systemd-run: a test must never schedule a real unit
		self.stub("systemd-run")

	def stub(self, name, jail="yes", code=0):
		path = os.path.join(self.bin, name)
		with open(path, "w") as f:
			f.write(STUB.format(log=self.log, jail=jail) if code == 0 else f"#!/bin/sh\nexit {code}\n")
		os.chmod(path, 0o755)

	def run_script(self, *args):
		env = {"PATH": f"{self.bin}:/usr/bin:/bin"}
		done = subprocess.run([self.script, *args], capture_output=True, text=True, env=env, timeout=20)
		calls = read(self.log).splitlines() if os.path.exists(self.log) else []
		return done, calls

	def test_ban_records_and_schedules_the_unban(self):
		self.stub("fail2ban-client")
		done, calls = self.run_script("45.33.32.156", "3600")
		self.assertEqual(done.returncode, 0, done.stderr)
		self.assertIn("banned 45.33.32.156 for 3600s in fail2ban jail wolf; unban scheduled", done.stdout)
		self.assertIn("set wolf banip 45.33.32.156", calls)
		timer = next(c for c in calls if c.startswith("--quiet"))
		self.assertIn("--on-active=3600s", timer)
		self.assertTrue(timer.endswith("set wolf unbanip 45.33.32.156"), timer)
		addr, until = read(self.state).split()
		self.assertEqual(addr, "45.33.32.156")
		self.assertAlmostEqual(int(until), time.time() + 3600, delta=30)

	def test_without_systemd_the_sweep_unbans(self):
		self.stub("fail2ban-client")
		self.stub("systemd-run", code=1)  # no systemd (a container) or no bus
		done, _ = self.run_script("2001:db8::7", "120")
		self.assertIn("no timer: --sweep unbans it", done.stdout)
		with open(self.state, "a") as f:
			f.write("203.0.113.9 1\n")  # an expired one, as after a reboot
		done, calls = self.run_script("--sweep")
		self.assertEqual(done.stdout.strip(), "unbanned 203.0.113.9")
		self.assertIn("set wolf unbanip 203.0.113.9", calls)
		self.assertEqual(read(self.state).split()[0], "2001:db8::7")  # the other one is kept

	def test_a_ban_again_replaces_its_expiry(self):
		self.stub("fail2ban-client")
		self.run_script("45.33.32.156", "60")
		self.run_script("45.33.32.156", "7200")
		lines = read(self.state).splitlines()
		self.assertEqual(len(lines), 1)
		self.assertAlmostEqual(int(lines[0].split()[1]), time.time() + 7200, delta=30)

	def test_refusals(self):
		self.stub("fail2ban-client")
		for args, why in ((("1.2.3.4;id", "60"), "invalid address"), (("45.33.32.156", "30"), "between 60 and 604800"),
		                  (("45.33.32.156", "1h"), "whole number"), (("45.33.32.156",), "usage")):  # fmt: skip
			with self.subTest(args):
				done, calls = self.run_script(*args)
				self.assertEqual(done.returncode, 2)
				self.assertIn(why, done.stderr)
				self.assertFalse(any("banip" in c for c in calls), calls)

	def test_missing_jail(self):
		self.stub("fail2ban-client", jail="no")
		done, calls = self.run_script("45.33.32.156", "3600")
		self.assertEqual(done.returncode, 1)
		self.assertIn("no jail 'wolf'", done.stderr)
		self.assertFalse(any("banip" in c for c in calls), calls)


if __name__ == "__main__":
	unittest.main()
