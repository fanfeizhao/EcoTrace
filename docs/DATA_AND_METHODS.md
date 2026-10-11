# Data and methods

## Heat and weather

Current air temperature comes from an available nearby NWS observation. Its station and timestamp matter. The separate 0–100 relative heat score blends preset city reference values by inverse distance. It is not satellite surface temperature, a measured personal exposure, or a health-risk probability.

## Route comparison

OpenRouteService supplies route geometry. EcoTrace evaluates mapped trees, building geometry, sun position, and profile detour limits. With the GIS extension, routes are split into sections at most 15 m long and sampled at section midpoints; route averages are weighted by section length. Available alternatives may be identical, less shaded, or outside the detour limit.

Prepared 2022 San Jose and SF–Oakland canopy layers supply mapped crown evidence where valid pixels exist. Available county canopy-height rasters supplement it. Class 1 is positive canopy evidence; NoData remains unknown. The vegetation-shadow approximation uses flat ground, a 1.5 m receptor, a 120 m search limit, and assumed attenuation. Missing or unconfirmed heights use an 8 m assumption. Survey years differ.

Experimental walking MRT/UTCI uses available weather and an assumed sunny radiation scenario. These are modeled estimates, not a measurement of body temperature. Fallback route heat scores and sun/shade minutes are separate quantities.

## Candidate review

The locally reviewed dataset last contained 5,617 records. Candidate markers may overlap existing crowns. Nearby planting space, soil, accessibility, ownership, local utilities, and permission require further review. Spacing checks do not establish unique tree counts. Transmission-line screening does not cover every local overhead or underground utility.

QGIS was used to overlay imagery, inspect alignment and resolution, clip canopy/height data, and inspect dated surface-temperature evidence. The separately inspected August 26, 2026 heat raster does not replace the app's preset heat-score model. A separate 120-point scan was rejected and never imported.

## Local files needed for the full application

The original installation supplies `ecotrace_reviewed_tree_sites.json`, the mapped canopy/building model (normally `canopy_model_county_buildings.json`), biking infrastructure and other dataset/configuration files used by its backend modules. The priority extension also requires `ecotrace_priority_data.json` (and retains its baseline backup), while the canopy editor uses its local review configuration and optional visual-review results. The GIS preparation tool writes a manifest pointing to locally available rasters. Paths from another machine must be regenerated. Image-evidence folders and user-upload folders are not distributed in this export. Empty folders cannot replace required evidence or data.

No candidate, account, activity, or user-upload database is included here. Never copy a database containing accounts or private community records into a public repository. Dataset reconstruction and a redistributable sample-data bundle remain future packaging work.

## Source entry points

- California urban canopy: https://www.fs.usda.gov/r05/state-private-tribal/california-urban-canopy-data
- Pacific Vegetation Map downloads: https://pacificvegmap.org/data-downloads/
- NWS API: https://www.weather.gov/documentation/services-web-API
- OpenRouteService: https://openrouteservice.org/
- QGIS: https://qgis.org/

Check each provider's metadata, units, survey year, coverage, attribution, and reuse terms before distributing derived data.
