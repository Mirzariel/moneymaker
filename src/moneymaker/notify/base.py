from __future__ import annotations

import logging

log = logging.getLogger("moneymaker.notify")


class Notifier:
    """Fan-out notifier. Never raises: a broken Telegram must not break trading or panic."""

    def __init__(self) -> None:
        self.sinks: list = []

    def add(self, sink) -> None:
        self.sinks.append(sink)

    async def send(self, text: str) -> None:
        log.info("NOTIFY: %s", text)
        for s in self.sinks:
            try:
                await s(text)
            except Exception:  # noqa: BLE001
                log.exception("notifier sink failed")
