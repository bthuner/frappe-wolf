"""Log parsers.

Each parser is fed raw lines with `feed(line)` and, once per push interval,
`drain(now)` returns `(metrics, events)` aggregated since the previous drain.
"""

from .auth import AuthParser
from .nginx import NginxParser
from .scheduler import SchedulerParser
from .suricata import SuricataParser
from .wazuh import WazuhParser
from .traceback import TracebackParser
from .worker import WorkerParser

PARSERS = {
	"nginx": NginxParser,
	"traceback": TracebackParser,
	"scheduler": SchedulerParser,
	"worker": WorkerParser,
	"auth": AuthParser,
	"suricata": SuricataParser,
	"wazuh": WazuhParser,
}

__all__ = ["PARSERS", "AuthParser", "NginxParser", "SchedulerParser", "SuricataParser", "TracebackParser", "WazuhParser",
           "WorkerParser"]
