Kanka Slurp
===============

Download a Kanka campaign's wiki into a local folder suitable for RAG (retrieval-augmented generation).

Quick start
-----------

1. Copy `.env.example` to `.env` and set `KANKA_API_TOKEN` and `KANKA_CAMPAIGN_ID`.

2. Install dependencies (recommended in a venv):

```bash
python -m pip install -r requirements.txt
```

3. Run the slurp (entities-only export by default):

```bash
python -m kanka_slurp --out ./kanka_data --details --verbose
```

What it writes
--------------

- `index.md` — global index in Markdown linking to all saved documents.
- Per-entity Markdown files under `<out>/entities_details/<entity_type>/<id>.md`.
	- Each `.md` contains a YAML frontmatter with metadata: `id`, `name`, `entity_type`, `image_full` (local path), `tags`, `urls_view`, and `updated_at`.
	- The document body is the entity's HTML converted to Markdown; the first line after frontmatter is an `# Name` H1.
- Images and other media are downloaded into the output tree and links in the Markdown are rewritten to local relative paths.

Behavior notes
--------------

- The tool now exports only the `entities` endpoint (other endpoints tend to duplicate data).
- Use `--details` to fetch each entity's detailed page (recommended).
- `--verbose` prints request and download diagnostics.
- Requests are throttled to respect API limits (default 1 request per 2 seconds, overridable with `KANKA_MIN_INTERVAL`).

If you'd like different output (JSON dumps, or include additional endpoints), tell me and I can add options.

