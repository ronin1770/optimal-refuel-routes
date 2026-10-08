# Optimal Refuel Routes

This Django REST Framework project accepts USA start and finish locations, obtains an OSRM driving route, selects verified station coordinates near that route, minimizes fuel-purchase cost for the fixed route, optionally verifies selected detours, and returns JSON plus a saved Leaflet map.

## Installation

### 1. Install system dependencies and uv

```bash
sudo apt update
sudo apt install -y curl ca-certificates

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

uv --version
```

If running as root, omit `sudo`. Run subsequent commands as the same user who installed uv.

### 2. Create the Python environment

```bash
cd /usr/local/development/optimal-refuel-routes

uv python install 3.13
uv venv --python 3.13
source .venv/bin/activate

python --version
```

### 3. Install dependencies

If `requirements.txt` is already present:

```
uv pip install -r requirements.txt
```

Otherwise, install the initial dependencies and save their exact versions:

```
uv pip install "Django==6.1.2" djangorestframework httpx python-dotenv
uv pip freeze > requirements.txt
```

Verify Django:

```
python -m django --version
```

## Environment

The project owner reports Python 3.13.16 and Django 6.1.2. Before this station work, the owner reported successful migrations, `manage.py check`, and development-server startup. The new station code and tests have not been executed by the implementer.

Target OS: Ubuntu 22.04. Do not describe it as tested there until the commands below complete on that system.

## Architecture

- `integrations/geocode_maps.py`: Geocode Maps authentication, HTTP transport, timeouts, parsing, and provider errors. The API key is sent through the provider-required `api_key` query parameter but is never logged, cached, or persisted.
- `stations/models.py`: canonical stations, source provenance, geocoding attempts/query cache, provider state, and job leases.
- `stations/services/csv_import.py`: validation, canonical selection, idempotency, and conflict reporting.
- `stations/services/geocoding.py`: query selection, exact matching, persistence, retries, allowance accounting, pacing, and job coordination.
- `stations/services/spatial_index.py`: R*Tree capability checks and batched bounding-box candidate lookup.
- `stations/management/commands/`: CLI orchestration for import, preparation, and export.
- `stations/migrations/0002_station_rtree.py`: R*Tree, synchronization/invalidation triggers, and database-cache table.
- `stations/migrations/0003_geocoding_decisions.py`: candidate-level decisions and station-level decision summaries.
- `stations/migrations/0004_repair_station_triggers.py`: installs the R*Tree synchronization and identity-invalidation triggers for existing databases.

- `integrations/osrm.py`: reusable synchronous `httpx.Client`, timeouts, response validation, and OSRM errors.
- `routing/services/endpoint_resolution.py`: coordinate/address endpoint validation and cached address geocoding.
- `routing/services/geometry.py`: R*Tree route-segment shortlisting followed by precise route distance and annotation-based progress.
- `routing/services/osrm_routing.py`: SQLite-coordinated one-request-per-second pacing, retry policy, request deadline, and three-call journey budget.
- `optimization/services/fuel_optimizer.py`: fixed-route fuel purchasing with tank/range feasibility checks and Decimal monetary arithmetic.
- `routing/services/planner.py`: route/result caches, dataset-version invalidation, detour verification, response assembly, and saved maps.

`integrations` is an ordinary Python package, not a Django app. Routing remains synchronous by design; reusable provider connections and caching reduce external calls without introducing async complexity.

## Setup

```bash
cd /usr/local/development/optimal-refuel-routes
source .venv/bin/activate
uv pip install -r requirements.txt
cp .env.example .env  # only when .env does not already exist
python manage.py migrate
```

If station geocoding is currently running, do **not** apply the new routing migrations to that active database. Let the job exit, take a database backup, then apply `stations.0005_station_dataset_version` and `routing.0001_routing_foundation` with `python manage.py migrate`. Development and automated tests use isolated databases.

Set the real API key in the ignored `.env` file:

```dotenv
GEOCODE_MAPS_API_KEY=
GEOCODE_MAPS_BASE_URL=https://geocode.maps.co
GEOCODE_REQUESTS_PER_SECOND=4
GEOCODE_INITIAL_REQUESTS_REMAINING=
GEOCODE_JOB_LEASE_SECONDS=120
GEOCODE_JOB_RENEWAL_SECONDS=30
OSRM_BASE_URL=https://router.project-osrm.org
OSRM_REQUESTS_PER_SECOND=1
ROUTE_CORRIDOR_MILES=5
ROUTE_CACHE_SECONDS=86400
ROUTE_PROVIDER_DEADLINE_SECONDS=60
USA_BOUNDARY_GEOJSON=
MAP_TILE_URL=https://tile.openstreetmap.org/{z}/{x}/{y}.png
MAP_TILE_ATTRIBUTION=&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>
```

| Environment variable | Description |
| --- | --- |
| `GEOCODE_MAPS_API_KEY` | Secret API key used to authenticate geocode.maps.co requests; never commit the populated value to source control. |
| `GEOCODE_MAPS_BASE_URL` | Base URL for the Geocode Maps search API, configurable for testing, proxies, or an alternative compatible deployment. |
| `GEOCODE_REQUESTS_PER_SECOND` | Maximum geocoding request rate used while initial provider allowance remains, capped internally at five requests per second. |
| `GEOCODE_INITIAL_REQUESTS_REMAINING` | Estimated unused initial-rate requests, used only when the SQLite provider counter does not already exist. |
| `GEOCODE_JOB_LEASE_SECONDS` | Duration in seconds before the exclusive station-geocoding job lease expires without successful renewal by its current owner. |
| `GEOCODE_JOB_RENEWAL_SECONDS` | Interval in seconds between ownership renewals while station geocoding waits, retries, or processes provider responses. |
| `OSRM_BASE_URL` | Base URL for OSRM driving-route requests, allowing the public demonstration service or a compatible private deployment. |
| `OSRM_REQUESTS_PER_SECOND` | Shared maximum OSRM request rate across application workers, capped internally at one outbound request each second. |
| `ROUTE_CORRIDOR_MILES` | Geographic distance in miles on either side of route geometry used to find eligible nearby stations. |
| `ROUTE_CACHE_SECONDS` | Lifetime in seconds for cached base routes, optimized responses, and saved map results before expiration. |
| `ROUTE_PROVIDER_DEADLINE_SECONDS` | Maximum elapsed seconds allowed for OSRM pacing, retries, backoff, and Retry-After waits during one journey. |
| `USA_BOUNDARY_GEOJSON` | Optional path to the bundled or replacement WGS84 GeoJSON containing boundaries for fifty states and DC. |
| `MAP_TILE_URL` | Leaflet raster tile URL template used by browsers when rendering saved route maps for low-volume staging. |
| `MAP_TILE_ATTRIBUTION` | HTML attribution displayed on Leaflet maps, including required visible link crediting OpenStreetMap contributors for supplied map data. |

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

Migration `0002` requires SQLite R*Tree support, backfills eligible matched stations, and installs synchronization triggers. Routing performs batched bounding-box queries around route segments, then calculates precise station-to-route distance and annotation-based route progress in application code. The default geographic corridor is five miles on either side; it is not a claim about driving-detour mileage.

## Route API

`POST /api/route/` accepts either representation independently for each endpoint. Direct string endpoints are not supported.

```json
{
  "start": {"address": "New York, NY"},
  "finish": {"latitude": 42.3601, "longitude": -71.0589},
  "initial_fuel_gallons": 50,
  "verify_detours": true
}
```

A successful response contains GeoJSON-order route coordinates (`[longitude, latitude]`), driven miles and duration, ordered purchase stops, consumption/purchases/remaining fuel, `total_fuel_cost_usd`, verification status, warnings, separate provider-call counts, actual request latency, cache indicators, and a saved `map_url`.

An address can carry explicit validation constraints, for example `{"address":"123 Main Street","city":"Boston","state":"MA"}`. Address and coordinate fields cannot be combined in one endpoint object. Coordinate inputs make no geocoding request. Uncached address inputs use geocode.maps.co with `addressdetails=1` and `countrycodes=us`, share the station job's persisted pacing and allowance counter, and do not acquire its exclusive batch lock. The response reports geocoding and OSRM calls separately.

USA membership is checked by point-in-polygon, not a final bounding box. The bundled `routing/data/us_states_dc_2025_500k.geojson` was converted to WGS84 GeoJSON from the U.S. Census Bureau 2025 state cartographic boundary KML at 1:500,000:

`https://www2.census.gov/geo/tiger/GENZ2025/kml/cb_2025_us_state_500k.zip`

It includes the 50 states and DC and excludes territories. Run `python scripts/convert_census_boundaries.py INPUT.zip OUTPUT.geojson` to reproduce the conversion. Census cartographic boundary files are U.S. government data and are public domain. Polygons load once per process; multipolygons, holes, boundary points, and Alaska's antimeridian are handled. Simplified cartographic coastlines/borders can misclassify points extremely close to a boundary.

The vehicle model is fixed at 50 gallons, 10 mpg, and 500 miles. Initial fuel defaults to 50 gallons and is already owned, so its historical cost is excluded. For the fixed OSRM route and eligible station set, the optimizer buys only enough to reach a cheaper reachable station; otherwise it carries the economical fuel needed subject to tank capacity. It detects insufficient starting fuel and unreachable gaps. This is minimum purchase cost for the fixed-route candidate model, not global road-route optimization.

Detour verification is enabled by default. An uncached journey normally makes one base OSRM call and, when fuel stops are selected, one further call containing all selected stops in travel order. Returned leg distances drive a second feasibility and cost calculation. Retries share a strict maximum of three outbound OSRM calls for the entire journey. `verify_detours=false` returns clearly marked estimates. A verified success is never returned after verification failure.

Base routes, optimized responses, and maps live for 24 hours by default. Optimized keys include normalized endpoints, initial fuel, optimizer configuration, OSRM configuration, and the persisted station-dataset version. SQLite triggers increment that version for station insertion/deletion and price, coordinate, or eligibility/status changes; attempt logs and review metadata do not invalidate it. Base-route keys intentionally do not depend on station data. Expired saved-map URLs return HTTP 410.

The map uses Leaflet and configurable OpenStreetMap standard raster tiles. It shows start, finish, route, ordered stops, prices, quantities, costs, and the saved calculation timestamp. Browser tile requests are separate from backend routing calls. For low-volume staging, retain visible “© OpenStreetMap contributors” attribution and follow the public tile policy: no bulk download, prefetch, offline collection, cache-busting parameters, or suppression of normal browser Referer/cache behavior.

OSRM uses reusable synchronous connections; connect/read/write/pool timeouts are 5/20/5/5 seconds. SQLite coordinates shared pacing at at most one request per second across workers. Each logical request allows two attempts for transient network failures or HTTP 429/502/503/504, with one-second backoff plus jitter and `Retry-After`. The whole journey has a three-outbound-call cap and a configurable deadline.

## Verification

Automated provider calls are mocked; do not perform live geocoding for test verification.

```bash
python manage.py makemigrations --check --dry-run
python manage.py check
python manage.py test stations routing optimization
```

To verify migrations on a fresh temporary SQLite database, use a temporary test-settings override rather than deleting, resetting, or recreating the development database.

All automated provider interactions are mocked and optimizer stations are synthetic test fixtures. No fabricated station is inserted into staging. No local performance figure is claimed because this implementation has not been executed by the implementer; responses report actual cold/cached latency when run.

## Safe staging rollout

After the active geocoding command has exited normally:

```bash
cd /usr/local/development/optimal-refuel-routes
source .venv/bin/activate
python manage.py migrate
python manage.py test stations routing optimization
python manage.py runserver 0.0.0.0:8000
```

For a production-facing DigitalOcean service, replace Django's development server with the deployment server/process manager selected for that VM. The code does not claim production-server validation yet.
