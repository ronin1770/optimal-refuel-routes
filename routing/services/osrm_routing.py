from __future__ import annotations

import random
import time
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from integrations.exceptions import ProviderResponseError, ProviderTransientError
from integrations.osrm import get_osrm_client
from routing.models import RoutingProviderState


class RoutingCallBudgetExceeded(ProviderResponseError):
    pass


class RoutingDeadlineExceeded(ProviderTransientError):
    pass


class RoutingCallBudget:
    def __init__(self, maximum=3):
        self.maximum = maximum
        self.used = 0

    @property
    def remaining(self):
        return self.maximum - self.used

    def consume(self):
        if self.remaining <= 0:
            raise RoutingCallBudgetExceeded("the three-call OSRM budget is exhausted")
        self.used += 1


def _reserve_slot(deadline_monotonic):
    now = timezone.now()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT OR IGNORE INTO routing_routingproviderstate
                  (provider, next_request_at, not_before, updated_at)
                VALUES ('osrm', NULL, NULL, %s)
                """,
                [now],
            )
            cursor.execute(
                "UPDATE routing_routingproviderstate SET updated_at = updated_at WHERE provider = 'osrm'"
            )
        state = RoutingProviderState.objects.get(provider="osrm")
        required = max(now, state.next_request_at or now, state.not_before or now)
        wait = max(0.0, (required - now).total_seconds())
        if time.monotonic() + wait > deadline_monotonic:
            raise RoutingDeadlineExceeded("OSRM pacing or Retry-After exceeds the request deadline")
        rate = max(0.01, min(float(settings.OSRM_REQUESTS_PER_SECOND), 1.0))
        state.next_request_at = required + timedelta(seconds=1.0 / rate)
        state.save(update_fields=["next_request_at", "updated_at"])
    return wait


def _suspend_provider(seconds):
    until = timezone.now() + timedelta(seconds=seconds)
    with transaction.atomic():
        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT OR IGNORE INTO routing_routingproviderstate
                  (provider, next_request_at, not_before, updated_at)
                VALUES ('osrm', NULL, NULL, %s)
                """,
                [now],
            )
            cursor.execute(
                "UPDATE routing_routingproviderstate SET updated_at = updated_at WHERE provider = 'osrm'"
            )
        state = RoutingProviderState.objects.get(provider="osrm")
        if not state.not_before or state.not_before < until:
            state.not_before = until
            state.save(update_fields=["not_before", "updated_at"])


def request_route(waypoints, *, budget, deadline_monotonic, max_attempts=2, client=None):
    client = client or get_osrm_client(settings.OSRM_BASE_URL)
    attempts = min(max_attempts, budget.remaining)
    if attempts < 1:
        raise RoutingCallBudgetExceeded("no OSRM call remains for this route request")
    for attempt in range(1, attempts + 1):
        wait = _reserve_slot(deadline_monotonic)
        if wait:
            time.sleep(wait)
        budget.consume()
        try:
            return client.route_once(waypoints)
        except ProviderTransientError as exc:
            retry_after = exc.retry_after or 0
            if retry_after:
                _suspend_provider(retry_after)
            if attempt == attempts:
                raise
            backoff = 1.0 + random.uniform(0, 0.5)
            delay = max(retry_after, backoff)
            if time.monotonic() + delay > deadline_monotonic:
                if retry_after:
                    _suspend_provider(retry_after)
                raise RoutingDeadlineExceeded("OSRM retry wait exceeds the request deadline") from exc
            time.sleep(delay)
    raise AssertionError("OSRM retry loop exited unexpectedly")
