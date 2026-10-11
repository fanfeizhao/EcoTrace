# EcoTrace

Local heat, route-shade comparison, and tree-planting candidate review for California's 17th Congressional District.

## What it does

- Shows nearby National Weather Service observations alongside a separate relative heat score.
- Compares walking and cycling route distance and modeled shade, using OpenRouteService route geometry and mapped tree/building evidence.
- Adds prepared canopy-cover and vegetation-height rasters where available.
- Provides a tree-candidate map and an editor for reviewing, adding, relocating, and deleting candidate records.
- Includes environmental community activities, e-waste tools, and a Gemini assistant.

## Repository scope

This source export contains application code and the available V19 editor and GIS extensions from the developer's local installation. It excludes credentials, account/community databases, user uploads, candidate records, cached imagery, and large GIS datasets. HTML files larger than 5 MB are set aside for manual review because they may embed datasets; their paths are recorded in `export_report.json`. Check that list for any runtime assets needed by the app. It is **not a self-contained runnable demo** until the required local data and configuration are provided. GitHub Pages cannot run the Python backend.

## Technology

Python, FastAPI, JavaScript, HTML/CSS, Tailwind CSS, and MapLibre. QGIS was used to prepare and visually inspect spatial inputs. External services include OpenRouteService, National Weather Service, and Google Gemini.

## Local setup

Use Python 3.10 or newer. The requirements file, when exported successfully, records installed package versions; it is an environment snapshot rather than a minimal dependency list.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Configure `ORS_API_KEY` and `GEMINI_API_KEY` privately as environment variables. Never commit their values. Populate the local spatial data and configuration described in `docs/DATA_AND_METHODS.md`.

With the original data files installed in `app/` and the GIS manifest prepared for this machine:

```bash
python run_local.py --port 8098
```

Open `http://127.0.0.1:8098/app?mode=guest`. The server is intended for local use. A public deployment needs a separate review of authentication, editor permissions, storage, and operational configuration.

## Project layout

- `app/`: source from the installed EcoTrace project.
- `editor/`: V19 review and methods extension, if found.
- `gis/`: local GIS preparation and route integration, if found.
- `docs/`: methods and data requirements.
- `export_report.json`: file counts and dependency inventory; no API-key values.

## Publication preparation

Historical standalone app builds and update backups were omitted from this copy. The legacy `/tree-review` endpoint redirects to the current `/canopy-review` editor when its generated 34 MB HTML is absent. The original Mac installation is unchanged. Python syntax and the redirect behavior were checked; the full application has not been executed from this data-free export.

## Development status

The last dataset supplied for review contained 5,617 candidate records. Counts can change with edits and are read from the installed database. Candidates are possible locations to investigate, not planted trees or approved planting sites. The additional 120-point QGIS trial was rejected and was not imported.

See `docs/DATA_AND_METHODS.md` for modeling assumptions. A public repository makes source visible; third-party data and imagery retain their own usage terms. No blanket data-redistribution license is granted by this export.
