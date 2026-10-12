from __future__ import annotations

import httpx
import pytest

from tri_clay.credits import CreditTripwire, OfficialApiClient
from tri_clay.errors import CreditSpentError, SessionExpiredError


def client_for(payloads: list[dict], status: int = 200) -> OfficialApiClient:
    queue = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"message": "nope"})
        return httpx.Response(200, json=queue.pop(0))

    return OfficialApiClient("key", transport=httpx.MockTransport(handler))


def test_balance_parsing():
    client = client_for([{"balance": 1404.8, "action_execution_balance": 5084}])
    balance = client.get_balance()
    assert balance.balance == pytest.approx(1404.8)
    assert balance.action_execution_balance == 5084


def test_tripwire_passes_when_unchanged():
    payload = {"balance": 1404.8, "action_execution_balance": 5084}
    tripwire = CreditTripwire(client_for([payload, dict(payload)]))
    tripwire.start()
    tripwire.check()


def test_tripwire_detects_data_credit_spend():
    tripwire = CreditTripwire(
        client_for(
            [
                {"balance": 1404.8, "action_execution_balance": 5084},
                {"balance": 1403.8, "action_execution_balance": 5084},
            ]
        )
    )
    tripwire.start()
    with pytest.raises(CreditSpentError):
        tripwire.check()


def test_tripwire_detects_action_spend():
    tripwire = CreditTripwire(
        client_for(
            [
                {"balance": 1404.8, "action_execution_balance": 5084},
                {"balance": 1404.8, "action_execution_balance": 5083},
            ]
        )
    )
    tripwire.start()
    with pytest.raises(CreditSpentError):
        tripwire.check()


def test_401_raises_session_expired():
    client = client_for([], status=401)
    with pytest.raises(SessionExpiredError):
        client.get_me()
