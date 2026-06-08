Kanka Slurp
===============

Download a Kanka campaign's data into a local folder.

Quick start
-----------

1. Copy `.env.example` to `.env` and set your `KANKA_API_TOKEN` and `KANKA_CAMPAIGN_ID`.

2. Install dependencies (recommended in a venv):

```bash
python -m pip install -r requirements.txt  # or use your pyproject tooling
```

3. Run the slurp:

```bash
python -m kanka_slurp --out ./kanka_data
```

Files and output
----------------

- JSON files per endpoint are written to the output directory (e.g., `entities.json`).
- Any discovered image/file URLs are downloaded to subfolders like `entities_files/`.

Notes
-----

The script is defensive and may need endpoint adjustments depending on your Kanka campaign and installed modules. You can pass a custom comma-separated `--endpoints` list if needed.

