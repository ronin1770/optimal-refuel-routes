from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.exceptions import (
    ProviderAccessError,
    ProviderResponseError,
    ProviderSuspendedError,
    ProviderTransientError,
)
from integrations.geocode_maps import GeocodeMapsClient
from stations.models import GeocodingAttempt, Station
from stations.services.geocoding import (
    AttemptBudget,
    JobLeaseError,
    Lease,
    ProviderAttemptBudgetExceeded,
    diagnostic_primary_query,
    force_or_release_lease,
    geocode_station,
    initialize_provider_state,
    normalized_query,
    reconcile_allowance,
    reevaluate_cached_station,
    station_name_parts,
)


class Command(BaseCommand):
    help = "Resumably geocode imported stations using geocode.maps.co"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int)
        parser.add_argument("--requests-per-second", type=float, default=settings.GEOCODE_REQUESTS_PER_SECOND)
        parser.add_argument("--retry-unresolved", action="store_true")
        parser.add_argument("--retry-failed", action="store_true")
        parser.add_argument("--source-id", action="append", dest="source_ids")
        parser.add_argument("--set-remaining-allowance", type=int)
        parser.add_argument("--release-lock", action="store_true")
        parser.add_argument("--force-release", action="store_true")
        parser.add_argument("--reevaluate-cached", action="store_true")
        parser.add_argument("--diagnostic-normalized-names", action="store_true")
        parser.add_argument("--max-provider-attempts", type=int, default=20)

    def handle(self, *args, **options):
        if options["set_remaining_allowance"] is not None:
            value = options["set_remaining_allowance"]
            if value < 0:
                raise CommandError("remaining allowance cannot be negative")
            reconcile_allowance(value)
            self.stdout.write(f"remaining initial allowance set to {value}")
            return
        if options["release_lock"] or options["force_release"]:
            try:
                removed = force_or_release_lease(force=options["force_release"])
            except JobLeaseError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write("lease released" if removed else "no lease existed")
            return
        if options["reevaluate_cached"]:
            changed = ambiguous = unresolved = 0
            lease = Lease(
                duration_seconds=settings.GEOCODE_JOB_LEASE_SECONDS,
                renewal_interval=settings.GEOCODE_JOB_RENEWAL_SECONDS,
            )
            try:
                lease.acquire()
            except JobLeaseError as exc:
                raise CommandError(str(exc)) from exc
            stations = Station.objects.filter(
                geocoding_status__in=[
                    Station.GeocodingStatus.UNRESOLVED,
                    Station.GeocodingStatus.AMBIGUOUS,
                ]
            ).order_by("id")
            try:
                for station in stations:
                    lease.confirm()
                    previous = station.geocoding_status
                    decision = reevaluate_cached_station(station)
                    if not decision:
                        continue
                    changed += int(previous != decision.status)
                    ambiguous += int(decision.status == Station.GeocodingStatus.AMBIGUOUS)
                    unresolved += int(decision.status == Station.GeocodingStatus.UNRESOLVED)
            finally:
                lease.release()
            self.stdout.write(
                f"cached re-evaluation complete: changed={changed} "
                f"ambiguous={ambiguous} unresolved={unresolved}; matched stations were not modified"
            )
            return
        if options["diagnostic_normalized_names"] and not (1 <= options["max_provider_attempts"] <= 20):
            raise CommandError("diagnostic max provider attempts must be between 1 and 20")
        if options["diagnostic_normalized_names"] and (
            options["limit"] is not None
            or options["source_ids"]
            or options["retry_unresolved"]
            or options["retry_failed"]
        ):
            raise CommandError(
                "diagnostic normalized-name mode selects exactly ten eligible stations; "
                "do not combine it with selection or retry options"
            )
        if options["requests_per_second"] <= 0:
            raise CommandError("requests per second must be positive")
        if not settings.GEOCODE_MAPS_API_KEY:
            raise CommandError("GEOCODE_MAPS_API_KEY is required")

        initial_remaining = settings.GEOCODE_INITIAL_REQUESTS_REMAINING
        initialize_provider_state(initial_remaining)
        statuses = [Station.GeocodingStatus.PENDING]
        if options["retry_unresolved"]:
            statuses.append(Station.GeocodingStatus.UNRESOLVED)
        if options["retry_failed"]:
            statuses.append(Station.GeocodingStatus.FAILED)
        stations = Station.objects.filter(geocoding_status__in=statuses).order_by("id")
        diagnostic_budget = None
        if options["diagnostic_normalized_names"]:
            diagnostic_budget = AttemptBudget(options["max_provider_attempts"])
            eligible = []
            candidates = Station.objects.filter(
                geocoding_status=Station.GeocodingStatus.UNRESOLVED,
                geocoding_cache_entries__response=[],
            ).distinct().order_by("id").prefetch_related("geocoding_cache_entries")
            for station in candidates:
                _brand, station_number = station_name_parts(station.name)
                query = diagnostic_primary_query(station)
                cached_queries = {
                    entry.normalized_query for entry in station.geocoding_cache_entries.all()
                }
                if station_number and normalized_query(query) not in cached_queries:
                    eligible.append(station)
                if len(eligible) == 10:
                    break
            if len(eligible) < 10:
                raise CommandError(
                    f"only {len(eligible)} eligible previously-empty numbered stations were found; no requests issued"
                )
            stations = eligible
        if options["source_ids"]:
            if isinstance(stations, list):
                selected_ids = set(options["source_ids"])
                stations = [station for station in stations if station.source_truckstop_id in selected_ids]
            else:
                stations = stations.filter(source_truckstop_id__in=options["source_ids"])
        if options["limit"] is not None and not options["diagnostic_normalized_names"]:
            if options["limit"] < 1:
                raise CommandError("limit must be at least 1")
            stations = stations[: options["limit"]]

        lease = Lease(
            duration_seconds=settings.GEOCODE_JOB_LEASE_SECONDS,
            renewal_interval=settings.GEOCODE_JOB_RENEWAL_SECONDS,
        )
        try:
            lease.acquire()
        except JobLeaseError as exc:
            raise CommandError(str(exc)) from exc

        processed = matched = ambiguous = unresolved = failed = 0
        attempts_before = GeocodingAttempt.objects.count()
        try:
            with GeocodeMapsClient(
                api_key=settings.GEOCODE_MAPS_API_KEY,
                base_url=settings.GEOCODE_MAPS_BASE_URL,
            ) as client:
                for station in stations:
                    lease.confirm()
                    try:
                        decision, _station_calls = geocode_station(
                            station=station,
                            client=client,
                            lease=lease,
                            configured_rate=options["requests_per_second"],
                            initial_remaining=initial_remaining,
                            primary_query=(
                                diagnostic_primary_query(station)
                                if options["diagnostic_normalized_names"]
                                else None
                            ),
                            primary_only=options["diagnostic_normalized_names"],
                            attempt_budget=diagnostic_budget,
                        )
                        if options["diagnostic_normalized_names"]:
                            decision = reevaluate_cached_station(station) or decision
                    except ProviderAccessError as exc:
                        raise CommandError(f"geocoding stopped: {exc}") from exc
                    except ProviderSuspendedError as exc:
                        raise CommandError(f"geocoding paused: {exc}") from exc
                    except ProviderAttemptBudgetExceeded as exc:
                        self.stderr.write(str(exc))
                        break
                    except (ProviderTransientError, ProviderResponseError) as exc:
                        Station.objects.filter(pk=station.pk).update(
                            geocoding_status=Station.GeocodingStatus.FAILED,
                            latitude=None,
                            longitude=None,
                            coordinate_source="",
                            geocoded_at=None,
                        )
                        failed += 1
                        processed += 1
                        self.stderr.write(f"station {station.source_truckstop_id}: {exc}")
                        continue
                    processed += 1
                    if decision.status == Station.GeocodingStatus.MATCHED:
                        matched += 1
                    elif decision.status == Station.GeocodingStatus.AMBIGUOUS:
                        ambiguous += 1
                    else:
                        unresolved += 1
        except JobLeaseError as exc:
            raise CommandError(f"geocoding stopped: {exc}") from exc
        finally:
            lease.release()

        calls = GeocodingAttempt.objects.count() - attempts_before
        self.stdout.write(
            f"processed={processed} matched={matched} ambiguous={ambiguous} "
            f"unresolved={unresolved} failed={failed} provider_calls={calls}"
            + (
                f" diagnostic_attempt_budget={diagnostic_budget.maximum}"
                if diagnostic_budget
                else ""
            )
        )
