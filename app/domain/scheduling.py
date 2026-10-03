"""Scheduling operations required by application workflows."""

from datetime import datetime
from typing import Protocol


class TransferJobs(Protocol):
    def schedule(
        self,
        transfer_id: str,
        scheduled_for: datetime,
        *,
        replace_existing: bool = False,
        run_at: datetime | None = None,
    ) -> None: ...

    def remove(self, transfer_id: str) -> None: ...
