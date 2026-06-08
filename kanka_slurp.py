"""
kanka_slurp.py

Download all data from a Kanka campaign into a local folder.
Reads API key and campaign ID from a .env file (KANKA_API_TOKEN, KANKA_CAMPAIGN_ID).

Usage:
    python -m kanka_slurp --out data

This is intentionally defensive: Kanka's API responses vary slightly by resource.
"""
from __future__ import annotations
import os
import sys
import time
import json
import argparse
from typing import Dict, Any, List, Optional
import requests
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv
from tqdm import tqdm
import html2text


DEFAULT_API_BASE = "https://api.kanka.io/1.0/"


class KankaSlurp:
    def __init__(self, token: str, campaign_id: str, api_base: str = DEFAULT_API_BASE, out_dir: str = "data", verbose: bool = False):
        self.token = token
        self.campaign_id = campaign_id
        self.api_base = api_base.rstrip('/') + '/'
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        })
        self.out_dir = out_dir
        os.makedirs(self.out_dir, exist_ok=True)
        # Throttle configuration: minimum seconds between requests
        self.min_interval = float(os.getenv('KANKA_MIN_INTERVAL', '2'))
        self._last_request = 0.0
        # Verbose mode for debugging HTTP requests/responses
        self.verbose = bool(verbose)
        # index of saved html entries for index generation
        self._index: Dict[str, List[Dict[str, str]]] = {}

    def _throttle(self):
        """Ensure at least `min_interval` seconds have passed since last request."""
        if self._last_request <= 0:
            return
        elapsed = time.time() - self._last_request
        if elapsed < self.min_interval:
            to_sleep = self.min_interval - elapsed
            time.sleep(to_sleep)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = urljoin(self.api_base, path)
        while True:
            if self.verbose:
                print(f"GET {url} params={params}")
            self._throttle()
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                # Rate limited
                retry = int(resp.headers.get('Retry-After', '5'))
                print(f"Rate limited, sleeping {retry}s...", file=sys.stderr)
                if self.verbose:
                    print(f"Response headers: {resp.headers}")
                time.sleep(retry)
                self._last_request = time.time()
                continue
            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                if self.verbose:
                    text = resp.text if resp is not None else '<no response>'
                    print(f"HTTPError for URL: {url}\nStatus: {resp.status_code}\nHeaders: {resp.headers}\nBody (truncated): {text[:1000]}")
                raise
            # mark time of this successful request
            self._last_request = time.time()
            return resp

    def fetch_paginated(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        params = dict(params or {})
        params.setdefault('page', 1)
        params.setdefault('limit', 100)
        all_items: List[Dict[str, Any]] = []
        print(f"Fetching endpoint: {endpoint}")
        while True:
            path = f"campaigns/{self.campaign_id}/{endpoint}"
            resp = self._get(path, params=params)
            data = resp.json()
            # Kanka usually returns data in a 'data' key, but sometimes returns list directly
            items = data.get('data') if isinstance(data, dict) and 'data' in data else data
            if not isinstance(items, list):
                # If single object returned, wrap
                items = [items]
            if not items:
                break
            all_items.extend(items)
            # Check pagination meta
            meta = data.get('meta') if isinstance(data, dict) else None
            if meta and isinstance(meta, dict):
                pagination = meta.get('pagination') or meta.get('pagination', meta.get('pagination'))
                # Some endpoints return meta.pagination
                if pagination:
                    current = pagination.get('current_page')
                    last = pagination.get('last_page')
                    if current is not None and last is not None and current < last:
                        params['page'] = current + 1
                        continue
                    else:
                        break
            # Fallback: if response had fewer than requested, assume end
            if len(items) < params.get('limit', 100):
                break
            params['page'] = params.get('page', 1) + 1
        return all_items

    def save_json(self, endpoint: str, data: List[Dict[str, Any]]):
        out_path = os.path.join(self.out_dir, f"{endpoint}.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(data)} objects to {out_path}")

    def download_image(self, url: str, subdir: str = "files") -> Optional[str]:
        """Download an image/media URL without sending Authorization header.

        Useful for CDN hosts that reject Authorization headers.
        """
        try:
            if self.verbose:
                print(f"DOWNLOAD_IMAGE {url}")
            self._throttle()
            resp = requests.get(url, stream=True, timeout=60)
            try:
                resp.raise_for_status()
            except requests.HTTPError:
                if self.verbose:
                    print(f"Failed image download URL: {url}\nStatus: {getattr(resp, 'status_code', '<no status>')}\nBody (truncated): {getattr(resp, 'text', '')[:1000]}")
                raise
            self._last_request = time.time()
        except Exception as e:
            print(f"Failed to download image {url}: {e}", file=sys.stderr)
            return None
        os.makedirs(os.path.join(self.out_dir, subdir), exist_ok=True)
        filename = os.path.basename(url.split('?')[0])
        out_path = os.path.join(self.out_dir, subdir, filename)
        with open(out_path, 'wb') as f:
            for chunk in resp.iter_content(1024 * 64):
                if chunk:
                    f.write(chunk)
        return out_path

    def extract_and_download_files(self, items: List[Dict[str, Any]], endpoint: str):
        # Look for fields that appear to be file references or image dicts
        downloaded = 0
        for it in tqdm(items, desc=f"Files in {endpoint}"):
            # heuristics: keys named 'image', 'avatar', 'header', 'entry', 'file'
            for key, val in list(it.items()):
                if isinstance(val, dict):
                    # Kanka image structure may contain 'url' or 'full', 'thumb'
                    for candidate in ('full', 'url'):
                        url = val.get(candidate)
                        if url and isinstance(url, str) and url.startswith('http'):
                            # Always treat these as media and use download_image (CDN-friendly)
                            # Save early-discovered media to a flat media/ folder.
                            saved = self.download_image(url, subdir=os.path.join('media'))
                            if saved:
                                downloaded += 1
        if downloaded:
            print(f"Downloaded {downloaded} files for endpoint {endpoint}")

    def _save_item_json(self, endpoint: str, item_id: str, data: Dict[str, Any], entity_type: Optional[str] = None):
        # Simplified layout: save JSON under out/<entity_type>/
        base = os.path.join(self.out_dir, entity_type or endpoint)
        os.makedirs(base, exist_ok=True)
        out_path = os.path.join(base, f"{item_id}.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if self.verbose:
            print(f"Saved detail for {endpoint}/{item_id} -> {out_path}")

    def _save_item_html(self, endpoint: str, item_id: str, html: str, entity_type: Optional[str] = None) -> str:
        # Simplified layout: save HTML under out/<entity_type>/
        base = os.path.join(self.out_dir, entity_type or endpoint)
        os.makedirs(base, exist_ok=True)
        out_path = os.path.join(base, f"{item_id}.html")
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(html)
        if self.verbose:
            print(f"Saved HTML for {endpoint}/{item_id} -> {out_path}")
        # return path relative to out_dir for JSON reference
        return os.path.relpath(out_path, start=self.out_dir)

    def convert_html_to_markdown(self, html: str) -> str:
        """Convert HTML to Markdown."""
        h = html2text.HTML2Text()
        h.ignore_images = False
        h.body_width = 0
        md = h.handle(html)
        return md

    def _save_item_markdown(self, endpoint: str, item_id: str, html: str, metadata: Dict[str, Any], entity_type: Optional[str] = None) -> str:
        """Convert HTML to Markdown, prepend YAML frontmatter with metadata, and save .md file.

        Returns path relative to `out_dir`.
        """
        # Simplified layout: save Markdown under out/<entity_type>/
        base = os.path.join(self.out_dir, entity_type or endpoint)
        os.makedirs(base, exist_ok=True)
        md_body = self.convert_html_to_markdown(html)
        # Build current md relative path for computing relative links
        current_md_rel = os.path.join(entity_type or endpoint, f"{item_id}.md")
        md_body = self._rewrite_image_links(md_body, endpoint, entity_type, metadata, current_md_rel)

        # Build simple YAML frontmatter for LLM-oriented metadata
        def yaml_escape(val: Any) -> str:
            if val is None:
                return '""'
            if isinstance(val, (list, tuple)):
                return '\n'.join([f"  - {str(x)}" for x in val])
            s = str(val)
            s = s.replace('"', '\\"')
            return f'"{s}"'

        lines = ['---']
        # fields: id, name, image_full, tags, urls.view, updated_at
        if 'id' in metadata:
            lines.append(f"id: {yaml_escape(metadata.get('id'))}")
        if 'name' in metadata:
            lines.append(f"name: {yaml_escape(metadata.get('name'))}")
        if 'entity_type' in metadata:
            lines.append(f"entity_type: {yaml_escape(metadata.get('entity_type'))}")
        if 'image_full' in metadata:
            lines.append(f"image_full: {yaml_escape(metadata.get('image_full'))}")
        # tags may be list
        tags = metadata.get('tags')
        if tags:
            lines.append('tags:')
            if isinstance(tags, (list, tuple)):
                for t in tags:
                    lines.append(f"  - {t}")
            else:
                lines.append(f"  - {tags}")
        # urls.view
        urls = metadata.get('urls') or {}
        view = urls.get('view') if isinstance(urls, dict) else None
        if view:
            lines.append(f"urls_view: {yaml_escape(view)}")
        if 'updated_at' in metadata:
            lines.append(f"updated_at: {yaml_escape(metadata.get('updated_at'))}")
        lines.append('---')

        out_path = os.path.join(base, f"{item_id}.md")
        title = metadata.get('name') or ''
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
            f.write('\n\n')
            if title:
                f.write(f'# {title}\n\n')
            f.write(md_body)
        if self.verbose:
            print(f"Saved Markdown for {endpoint}/{item_id} -> {out_path}")
        return os.path.relpath(out_path, start=self.out_dir)

    def _rewrite_image_links(self, md: str, endpoint: str, entity_type: Optional[str], metadata: Dict[str, Any], current_md_rel: str) -> str:
        """Replace image URLs in markdown or inline HTML with local relative paths if downloaded.

        Looks for Markdown image syntax and <img src=> tags. Resolves filenames against
        known download locations and rewrites URLs to local relative paths.
        """
        import re

        def find_local_for_url(url: str) -> Optional[str]:
            if not url or not url.startswith('http'):
                return None
            filename = os.path.basename(url.split('?')[0])
            if not filename:
                return None
            candidates = []
            # prefer images saved next to entity markdown: out/<entity_type>/<filename>
            if entity_type:
                candidates.append(os.path.join(self.out_dir, entity_type, filename))
            # early downloads go to out/media/
            candidates.append(os.path.join(self.out_dir, 'media', filename))
            # fallback: top-level
            candidates.append(os.path.join(self.out_dir, filename))
            for p in candidates:
                if os.path.exists(p):
                    # return path relative to current md file directory
                    current_md_abs = os.path.join(self.out_dir, current_md_rel)
                    target_abs = os.path.abspath(p)
                    rel = os.path.relpath(target_abs, start=os.path.dirname(current_md_abs))
                    return rel
            # last resort: search under out_dir
            for root, dirs, files in os.walk(self.out_dir):
                if filename in files:
                    target_abs = os.path.abspath(os.path.join(root, filename))
                    current_md_abs = os.path.join(self.out_dir, current_md_rel)
                    rel = os.path.relpath(target_abs, start=os.path.dirname(current_md_abs))
                    return rel
            return None

        # replace markdown image links: ![alt](url "title")
        md_img_re = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

        def md_img_sub(match):
            alt = match.group(1)
            link = match.group(2).strip()
            parts = link.split()
            url = parts[0].strip('"')
            local = find_local_for_url(url)
            if local:
                title = ''
                if len(parts) > 1:
                    title = ' ' + ' '.join(parts[1:])
                return f'![{alt}]({local}{title})'
            return match.group(0)

        md = md_img_re.sub(md_img_sub, md)

        # replace regular markdown links [text](url) -> where possible point to local md files
        link_re = re.compile(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)')

        def link_sub(match):
            text = match.group(1)
            link = match.group(2).strip()
            parts = link.split()
            url = parts[0].strip('"')
            local = find_local_for_url(url)
            if local:
                return f'[{text}]({local})'
            return match.group(0)

        md = link_re.sub(link_sub, md)

        # replace inline HTML <img src="..."> tags
        html_img_re = re.compile(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)

        def html_img_sub(match):
            url = match.group(1)
            local = find_local_for_url(url)
            if local:
                return f'![]({local})'
            return match.group(0)

        md = html_img_re.sub(html_img_sub, md)

        return md

    def _determine_entity_type(self, endpoint: str, item: Dict[str, Any], payload: Dict[str, Any], api_url: Optional[str]) -> str:
        # prefer explicit payload type
        t = (payload.get('type') if isinstance(payload, dict) else None) or item.get('type') or None
        if t:
            # normalize to plural folder name if looks singular
            return str(t).lower()
        # try to parse from api_url path (e.g. '/campaigns/363035/races/900275')
        if api_url:
            try:
                p = urlparse(api_url).path
                parts = [p for p in p.split('/') if p]
                # look for resource name after campaign id
                if 'campaigns' in parts:
                    idx = parts.index('campaigns')
                    if len(parts) > idx + 2:
                        return parts[idx + 2]
                # fallback to last non-id segment
                for seg in reversed(parts):
                    if not seg.isdigit():
                        return seg
            except Exception:
                pass
        # fallback to endpoint name
        return endpoint

    def fetch_items_details(self, endpoint: str, items: List[Dict[str, Any]]):
        """Fetch detailed page for each item and save it.

        Uses the item's `urls.api` when present, otherwise constructs
        campaigns/{campaign_id}/{endpoint}/{id_or_child}.
        """
        print(f"Fetching details for {len(items)} items in {endpoint}")
        for it in tqdm(items, desc=f"Details {endpoint}"):
            # Prefer explicit API URL
            api_url = None
            urls = it.get('urls') or {}
            api_url = urls.get('api') if isinstance(urls, dict) else None
            item_id = None
            # determine an identifier to name the file
            if api_url and isinstance(api_url, str) and api_url.startswith('http'):
                # try to extract a stable id from the URL path (last segment)
                item_id = os.path.basename(api_url.rstrip('/').split('?')[0])
            # fallbacks
            if not item_id:
                item_id = str(it.get('child_id') or it.get('id') or '')
            if not item_id:
                # give up on this item
                if self.verbose:
                    print(f"Skipping item without id: {it}")
                continue

            try:
                # If we have a full api_url, GET it directly; otherwise construct path
                if api_url and api_url.startswith('http'):
                    self._throttle()
                    if self.verbose:
                        print(f"GET detail {api_url}")
                    resp = self.session.get(api_url, timeout=30)
                    try:
                        resp.raise_for_status()
                    except requests.HTTPError:
                        if self.verbose:
                            print(f"Detail HTTP error for {api_url}: {resp.status_code} {getattr(resp, 'text', '')[:500]}")
                        continue
                    data = resp.json()
                    self._last_request = time.time()
                else:
                    path = f"campaigns/{self.campaign_id}/{endpoint}/{item_id}"
                    resp = self._get(path)
                    data = resp.json()

                # normalize data payload
                payload = data.get('data') if isinstance(data, dict) and 'data' in data else data
                # If the payload contains HTML entry fields, save them to separate files
                if isinstance(payload, dict):
                    entry_html = payload.pop('entry', None)
                    entry_parsed = payload.pop('entry_parsed', None)
                    # prefer parsed HTML if available
                    html_to_save = entry_parsed or entry_html
                    # determine entity type for grouping
                    entity_type = self._determine_entity_type(endpoint, it, payload, api_url)
                    # Download the primary image (image_full) next to the html/json and update JSON to local path
                    image_full = payload.get('image_full')
                    if image_full and isinstance(image_full, str) and image_full.startswith('http'):
                        # always use download_image for CDN-hosted media
                        # save primary image next to the entity markdown: out/<entity_type>/
                        saved_image = self.download_image(image_full, subdir=os.path.join(entity_type or ''))
                        if saved_image:
                            rel_image = os.path.relpath(saved_image, start=self.out_dir)
                            payload['image_full'] = rel_image
                            payload['image_file'] = rel_image

                    # Ensure we always write a Markdown document (may be empty body)
                    body_html = html_to_save or ''
                    metadata = {
                        'id': payload.get('id') or item_id,
                        'name': payload.get('name') or it.get('name') or item_id,
                        'entity_type': entity_type,
                        'image_full': payload.get('image_full'),
                        'tags': payload.get('tags'),
                        'urls': payload.get('urls'),
                        'updated_at': payload.get('updated_at'),
                    }
                    md_rel = self._save_item_markdown(endpoint, item_id, body_html, metadata, entity_type=entity_type)
                    # reference the saved markdown file in the JSON payload and index
                    payload['entry_file'] = md_rel
                    self._index.setdefault(endpoint, []).append({'name': metadata.get('name') or item_id, 'path': md_rel, 'type': entity_type})
                    # download any files referenced in this detailed object
                    self.extract_and_download_files([payload], endpoint + '_detail')
            except Exception as e:
                print(f"Failed to fetch detail for {endpoint}/{item_id}: {e}", file=sys.stderr)
                if self.verbose:
                    import traceback
                    traceback.print_exc()

    def slurp(self, fetch_details: bool = False):
        """Auto-discovery mode — always fetch `entities`, discover module endpoints
        from each entity's `urls.api`, then fetch each discovered module endpoint.
        This is the only behaviour (ignores any user-provided endpoint lists).
        """
        summary = {}

        # Step 1: fetch entities (discovery source)
        try:
            print("Auto-discovery: fetching 'entities' to discover module endpoints")
            entities = self.fetch_paginated('entities')
            # Do not save endpoint-level JSON; produce only Markdown files for details
            self.extract_and_download_files(entities, 'entities')
            if fetch_details and entities:
                self.fetch_items_details('entities', entities)
            summary['entities'] = len(entities)
        except Exception as e:
            print(f"Error fetching entities for discovery: {e}", file=sys.stderr)
            if self.verbose:
                import traceback
                traceback.print_exc()
            entities = []

        # Step 2: discover endpoints from entities' urls.api values
        # Do not fetch other endpoints — exporting `entities` is sufficient and
        # other endpoints contain largely duplicated data. Finish after entities.
        print("Skipping discovery of other endpoints; exported `entities` only.")

        print("Done. Summary:")
        for k, v in summary.items():
            print(f" - {k}: {v} items")

        # generate index.html if we have any indexed entries
        try:
            if self._index:
                self._generate_index()
        except Exception:
            if self.verbose:
                import traceback
                traceback.print_exc()

    def _generate_index(self):
        """Generate a simple index.md under the output directory linking to saved Markdown files."""
        out_index = os.path.join(self.out_dir, 'index.md')
        lines: List[str] = []
        lines.append(f'# Index for {self.campaign_id}')
        lines.append('')
        for endpoint, items in sorted(self._index.items()):
            lines.append(f'## {endpoint}')
            # group by type
            by_type: Dict[str, List[Dict[str, str]]] = {}
            for it in items:
                by_type.setdefault(it.get('type') or 'unknown', []).append(it)
            for t, its in sorted(by_type.items()):
                lines.append(f'### {t}')
                for it in sorted(its, key=lambda x: x.get('name') or ''):
                    href = it['path']
                    name = it['name']
                    lines.append(f'- [{name}]({href})')
                lines.append('')
        with open(out_index, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        print(f"Wrote index to {out_index}")


def load_config_from_env(dotenv_path: Optional[str] = None) -> Dict[str, str]:
    if dotenv_path:
        load_dotenv(dotenv_path)
    else:
        load_dotenv()
    token = os.getenv('KANKA_API_TOKEN')
    campaign = os.getenv('KANKA_CAMPAIGN_ID') or os.getenv('KANKA_CAMPAIGN')
    api_base = os.getenv('KANKA_API_BASE') or DEFAULT_API_BASE
    if not token or not campaign:
        raise RuntimeError('KANKA_API_TOKEN and KANKA_CAMPAIGN_ID must be set in your .env file')
    return {'token': token, 'campaign': campaign, 'api_base': api_base}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog='kanka-slurp')
    parser.add_argument('--out', '-o', default='data', help='Output directory')
    parser.add_argument('--dotenv', default='.env', help='Path to dotenv file')
    parser.add_argument('--api-base', help='Override API base URL')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose output for debugging')
    parser.add_argument('--details', action='store_true', help='Fetch per-item detailed pages for entities')
    args = parser.parse_args(argv)

    cfg = load_config_from_env(args.dotenv)
    if args.api_base:
        cfg['api_base'] = args.api_base
    slurper = KankaSlurp(cfg['token'], cfg['campaign'], api_base=cfg.get('api_base', DEFAULT_API_BASE), out_dir=args.out, verbose=args.verbose)
    slurper.slurp(fetch_details=bool(args.details))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
