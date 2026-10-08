# Optimal Refuel Routes

This Django REST Framework project will provide cost-effective USA refueling routes. The current milestone implements only the station-data and geocoding foundation. Routing endpoints and fuel optimization remain outside this milestone.

## Environment

The project owner reports Python 3.13.16 and Django 6.1.2. Before this station work, the owner reported successful migrations, `manage.py check`, and development-server startup. The new station code and tests have not been executed by the implementer.

Target OS: Ubuntu 22.04. Do not describe it as tested there until the commands below complete on that system.

## Architecture

- `integrations/geocode_maps.py`: Geocode Maps authentication, HTTP transport, timeouts, parsing, and provider errors. The API key is sent through the provider-required `api_key` query parameter but is never logged, cached, or persisted.
- `stations/models.py`: canonical stations, source provenance, geocoding attempts/query cache, provider state, and job leases.
- `stations/services/csv_import.py`: validation, canonical selection, idempotency, and conflict reporting.
- `stations/services/geocoding.py`: query selection, exact matching, persistence, retries, allowance accounting, pacing, and job coordination.
- `stations/services/spatial_index.py`: R*Tree capability checks and bounding-box candidate lookup. Exact corridor distance and route progress belong to the routing milestone.
- `stations/management/commands/`: CLI orchestration for import, preparation, and export.
- `stations/migrations/0002_station_rtree.py`: R*Tree, synchronization/invalidation triggers, and database-cache table.
- `stations/migrations/0003_geocoding_decisions.py`: candidate-level decisions and station-level decision summaries.
- `stations/migrations/0004_repair_station_triggers.py`: installs the R*Tree synchronization and identity-invalidation triggers for existing databases.

`integrations` is an ordinary Python package. No empty OSRM client is included because routing is outside this task.

## Setup

```bash
cd /usr/local/development/optimal-refuel-routes
source .venv/bin/activate
uv pip install -r requirements.txt
cp .env.example .env  # only when .env does not already exist
python manage.py migrate
```

Set the real API key in the ignored `.env` file:

```dotenv
GEOCODE_MAPS_API_KEY=
GEOCODE_MAPS_BASE_URL=https://geocode.maps.co
GEOCODE_REQUESTS_PER_SECOND=4
GEOCODE_INITIAL_REQUESTS_REMAINING=
GEOCODE_JOB_LEASE_SECONDS=120
GEOCODE_JOB_RENEWAL_SECONDS=30
```

An unset initial allowance defaults conservatively to zero and one request per second. The environment value initializes an absent SQLite provider-state row only; command restarts do not reset it.

## CSV import

```bash
python manage.py import_stations data/fuel-prices.csv
```

The importer handles quoted CSV and UTF-8 BOM, validates all columns, rejects non-positive prices, preserves eight decimal places, and stores each distinct source row. One canonical station exists per OPIS Truckstop ID. The first valid row in CSV order supplies canonical identity and price. Later distinct rows for that ID are retained as conflicting source records and flag the station for review; they do not replace canonical values or verified coordinates. This first-row simplification is deterministic for an initial import but requires manual review when records conflict.

Repeated imports are idempotent by source-row fingerprint. The command reports imported, unchanged, invalid, and conflicting counts.

## Station geocoding

Populate the API key, then run and review the required 50-station pilot first:

```bash
python manage.py geocode_stations --limit 50 --requests-per-second 4
python manage.py export_station_coordinates data/station-coordinates.csv
```

Only after reviewing the pilot, resume preparation with:

```bash
python manage.py geocode_stations --requests-per-second 4
```

Pending stations are selected by default. Optional controls are:

```bash
python manage.py geocode_stations --retry-unresolved
python manage.py geocode_stations --retry-failed
python manage.py geocode_stations --source-id 7 --source-id 20
python manage.py geocode_stations --set-remaining-allowance 12345
python manage.py geocode_stations --release-lock
python manage.py geocode_stations --force-release
python manage.py geocode_stations --reevaluate-cached
```

`--release-lock` refuses to remove an active lease. `--force-release` is an explicit recovery override.

### Queries, pacing, and recovery

The primary query is `{Truckstop Name}, {City}, {State}, USA`. If it does not confidently match, the only fallback is `{Address}, {City}, {State}, USA`; empty or normalized-duplicate fallbacks are skipped. Highway-exit text stays literal. Responses are cached against the current station identity, so interrupted jobs resume without repeating completed queries. Identity changes cannot reuse previous cached responses.

Every outbound attempt, including retries and fallbacks, atomically consumes the locally tracked allowance. Initial pacing defaults to four requests per second, is capped at five, and has no bursts. At zero allowance it changes to one request per second. This is only a local estimate: requests made elsewhere with the same API key also consume the provider allowance. Reconcile it with `--set-remaining-allowance` when needed.

Only one preparation job may run at once. Its SQLite lease lasts 120 seconds by default, renews every 30 seconds during waits, and has a unique ownership token. A job stops if ownership is lost; an expired lease can be acquired by a later run.

Each query permits three attempts. Connect/read timeouts are 5/20 seconds. Only connection/timeouts and HTTP 429, 502, 503, and 504 are retried. Backoff starts at two seconds, is capped at 30 seconds, and adds 0–1 second jitter. Valid `Retry-After` values are honored beyond that cap. A delay above 120 seconds is persisted as a provider-wide not-before time and the job exits for later resumption. Authentication/access failures stop the job.

### Exact matching and limitations

Matching is intentionally exact, not fuzzy. Comparisons are case-insensitive with punctuation/whitespace normalization; state names normalize to codes, and station numbers remain meaningful. Provider importance/ranking is not treated as confidence.

A match requires USA country, source state and locality, a fuel-station or named-truckstop result, and the normalized station name including a supplied number. Alternatively, address matching requires country/state/locality, exact street and house number, a fuel-station result, and no contradictory returned name. ZIP is not required.

Multiple matches are ambiguous. A missing station number is insufficient evidence and remains ambiguous; an explicitly different number is contradictory. A standalone `namedetails.ref` may confirm the source number after trimming whitespace and normalizing numeric leading zeros, but only when brand, country, state, and locality also match. Partial text is never searched for digits. A standalone numeric final path segment in an official candidate `website` URL is treated as explicit store-number evidence, and a conflicting value blocks confirmation. Number confirmation does not establish fuel availability. A credible matching brand and locality without a fuel classification is ambiguous, never automatically matched. Empty, contradictory, city, road, intersection, and highway-exit-only results are unresolved. Only matched stations receive coordinates and enter the spatial index. City centroids and fabricated coordinates are never used. Candidate-level dispositions and reasons are stored with attempts, while the final combined explanation is stored on the station.

Cached responses can be re-evaluated without network calls. Existing matched stations are excluded:

```bash
python manage.py geocode_stations --reevaluate-cached
```

The capped diagnostic mode selects ten previously unresolved numbered stations with an empty cached response. It removes the internal station-number suffix from the new search text while retaining the original name and number for validation. It issues only the normalized primary query and enforces a hard maximum of 20 outbound attempts, including retries:

```bash
python manage.py geocode_stations --diagnostic-normalized-names --max-provider-attempts 20
```

Review its report before any complete-dataset run.

Canonical name/address/city/state edits clear coordinates and return a station to pending, including bulk updates. Database triggers remove its R*Tree entry. Price-only edits preserve coordinates. Attempts remain for audit.

## Coordinate export

```bash
python manage.py export_station_coordinates data/station-coordinates.csv
```

Export ordering is deterministic and file replacement is atomic. All statuses are included for review.

## Spatial index

Migration `0002` requires SQLite R*Tree support, backfills eligible matched stations, and installs synchronization triggers. Routing code should use bounding-box queries through `station_ids_in_bounds()`, then calculate precise route distance and progress in application code.

## Verification

Automated provider calls are mocked; do not perform live geocoding for test verification.

```bash
python manage.py makemigrations --check --dry-run
python manage.py check
python manage.py test stations
```

To verify migrations on a fresh temporary SQLite database, use a temporary test-settings override rather than deleting, resetting, or recreating the development database.
