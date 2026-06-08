# Kanka Slurp

[![CI](https://github.com/dylan-robins/kanka-slurp/actions/workflows/ci.yml/badge.svg)](https://github.com/dylan-robins/kanka-slurp/actions/workflows/ci.yml)

Download a Kanka campaign into a local folder suitable for RAG.

## Quick Start

1. Copy `.env.example` to `.env` and set `KANKA_API_TOKEN` and `KANKA_CAMPAIGN_ID`.
2. Install dependencies in a virtual environment.

```bash
uv sync
```

3. Run the exporter.

```bash
uv run python -m kanka_slurp --out ./kanka_data --verbose
```

## Usage

- Normal mode exports the `entities` endpoint and fetches entity detail pages.
- `--update` still fetches details, but only rewrites files when `updated_at` changed on the server.

## Update Mode Behavior

When you run with `--update`:

- Matching `updated_at` values skip the file.
- Changed timestamps rewrite the file.
- Missing local files are created.
- Renamed entities replace the old filename after writing the new one.

## Output

- `index.md` - global Markdown index linking to saved documents.
- Per-entity Markdown files under `<out>/<entity_type>/<id>-<entity-name>.md`.
- YAML frontmatter includes `id`, `name`, `entity_type`, `image_full`, `tags`, `urls_view`, and `updated_at`.
- The body is the entity HTML converted to Markdown, with a top-level `# Name` heading when available.
- Images and other media are downloaded into the output tree and rewritten to local relative paths.

## Behavior Notes

- The exporter currently only uses the `entities` endpoint because the other endpoints tend to duplicate data.
- Requests are throttled to respect API limits. The default is one request every two seconds, and you can override it with `KANKA_MIN_INTERVAL`.
- `--verbose` enables request and download diagnostics.

## Example

```bash
python -m kanka_slurp --out ./kanka_data --update --verbose
```
