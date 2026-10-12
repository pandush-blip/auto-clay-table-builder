"""Credit tripwire: read Clay balances before and after every run."""

from __future__ import annotations

from .errors import CreditSpentError
from .official_api import Balance, OfficialApiClient  # noqa: F401  (re-exported)

EPSILON = 1e-9


class CreditTripwire:
    """Snapshot -> work -> verify. Any balance change raises."""

    def __init__(self, client: OfficialApiClient) -> None:
        self._client = client
        self._start: Balance | None = None
        self._end: Balance | None = None

    def start(self) -> Balance:
        self._start = self._client.get_balance()
        return self._start

    def check(self) -> Balance:
        if self._start is None:
            raise RuntimeError("tripwire.start() must run before check()")
        self._end = self._client.get_balance()
        if abs(self._end.balance - self._start.balance) > EPSILON:
            raise CreditSpentError(
                "credit balance changed: "
                f"{self._start.balance} -> {self._end.balance} (data credits). "
                "Stop all builds and investigate."
            )
        start_actions = self._start.action_execution_balance
        end_actions = self._end.action_execution_balance
        if (
            start_actions is not None
            and end_actions is not None
            and abs(end_actions - start_actions) > EPSILON
        ):
            raise CreditSpentError(
                "action execution balance changed: "
                f"{start_actions} -> {end_actions}. Stop all builds and investigate."
            )
        return self._end

    @property
    def start_balance(self) -> Balance | None:
        return self._start

    @property
    def end_balance(self) -> Balance | None:
        return self._end
