"""Liveness check for the standalone poller.

Run by Docker as the ``bot`` service's healthcheck::

    python -m app.bot.heartbeat

Exits 0 when the heartbeat file at ``BOT_HEARTBEAT_PATH`` is fresh, 1 otherwise.

**What this proves, and what it does not.** It proves the poller's event loop is
still turning — a wedged or dead loop stops writing the file and the age crosses
the threshold. It does *not* prove Telegram has delivered anything recently, and
it does not prove the process is consuming updates. That is a much stronger
claim than a timestamp can support, and the difference matters: an idle bot on a
quiet chat has a perfectly healthy heartbeat and has done no work. Read a green
status as "the poller is running", not "the bot is working".

The threshold is twice the write interval, so a single missed write is tolerated
while a genuinely stopped loop is caught on the second.
"""

from __future__ import annotations

import sys
import time

from app.core.config import get_settings


def main() -> int:
    settings = get_settings()
    path = settings.BOT_HEARTBEAT_PATH
    staleness = settings.BOT_HEARTBEAT_INTERVAL_SECONDS * 2 + 5

    try:
        with open(path, encoding="ascii") as handle:
            last = float(handle.read().strip())
    except FileNotFoundError:
        # Never started, or crashed before the first tick. Not healthy.
        print(f"no heartbeat file at {path}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"heartbeat at {path} is unreadable: {exc}", file=sys.stderr)
        return 1

    age = time.time() - last
    if age > staleness:
        print(
            f"heartbeat is stale: {age:.0f}s old, limit {staleness:.0f}s",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
