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
                            saved = self.download_image(url, subdir=endpoint + '_files')
                            if saved:
                                downloaded += 1
        if downloaded:
            print(f"Downloaded {downloaded} files for endpoint {endpoint}")

    def _save_item_json(self, endpoint: str, item_id: str, data: Dict[str, Any], entity_type: Optional[str] = None):
        base = os.path.join(self.out_dir, endpoint + '_details')
        if entity_type:
            base = os.path.join(base, entity_type)
        os.makedirs(base, exist_ok=True)
        out_path = os.path.join(base, f"{item_id}.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if self.verbose:
            print(f"Saved detail for {endpoint}/{item_id} -> {out_path}")

    def _save_item_html(self, endpoint: str, item_id: str, html: str, entity_type: Optional[str] = None) -> str:
        base = os.path.join(self.out_dir, endpoint + '_details')
        if entity_type:
            base = os.path.join(base, entity_type)
        os.makedirs(base, exist_ok=True)
        out_path = os.path.join(base, f"{item_id}.html")
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(html)
        if self.verbose:
            print(f"Saved HTML for {endpoint}/{item_id} -> {out_path}")
        # return path relative to out_dir for JSON reference
        return os.path.relpath(out_path, start=self.out_dir)

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
                    if html_to_save:
                        html_rel = self._save_item_html(endpoint, item_id, html_to_save, entity_type=entity_type)
                        # reference the saved file in the JSON payload
                        payload['entry_file'] = html_rel
                        # add to index for index.html generation
                        self._index.setdefault(endpoint, []).append({'name': payload.get('name') or it.get('name') or item_id, 'path': html_rel, 'type': entity_type})
                    # Download the primary image (image_full) next to the html/json and update JSON to local path
                    image_full = payload.get('image_full')
                    if image_full and isinstance(image_full, str) and image_full.startswith('http'):
                        saved_image = self.download_image(image_full, subdir=os.path.join(endpoint + '_details', entity_type))
                        if saved_image:
                            rel_image = os.path.relpath(saved_image, start=self.out_dir)
                            payload['image_full'] = rel_image
                            payload['image_file'] = rel_image
                    else:
                        # still add to index pointing to json if no html
                        self._index.setdefault(endpoint, []).append({'name': payload.get('name') or it.get('name') or item_id, 'path': os.path.relpath(os.path.join(endpoint + '_details', entity_type, f"{item_id}.json"), start=self.out_dir), 'type': entity_type})
                    # download any files referenced in this detailed object
                    self.extract_and_download_files([payload], endpoint + '_detail')
                # save per-item JSON (without inlined HTML)
                self._save_item_json(endpoint, item_id, payload, entity_type=entity_type if isinstance(payload, dict) else None)
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
            self.save_json('entities', entities)
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
        discovered: set = set()
        for it in entities:
            urls = it.get('urls') or {}
            api = urls.get('api') if isinstance(urls, dict) else None
            if api and isinstance(api, str):
                try:
                    p = urlparse(api).path
                    parts = [seg for seg in p.split('/') if seg]
                    # find 'campaigns' and take the resource name after the campaign id
                    if 'campaigns' in parts:
                        idx = parts.index('campaigns')
                        if len(parts) > idx + 2:
                            discovered.add(parts[idx + 2])
                    else:
                        # fallback: last non-numeric segment
                        for seg in reversed(parts):
                            if not seg.isdigit():
                                discovered.add(seg)
                                break
                except Exception:
                    if self.verbose:
                        print(f"Failed to parse api url for discovery: {api}")

        # remove generic endpoints that we've already processed or shouldn't re-fetch
        discovered.discard('entities')
        discovered.discard('campaigns')

        # Step 3: fetch each discovered module endpoint
        for endpoint in sorted(discovered):
            try:
                items = self.fetch_paginated(endpoint)
                self.save_json(endpoint, items)
                self.extract_and_download_files(items, endpoint)
                if fetch_details and items:
                    self.fetch_items_details(endpoint, items)
                summary[endpoint] = len(items)
            except requests.HTTPError as e:
                print(f"HTTP error fetching {endpoint}: {e}", file=sys.stderr)
                if self.verbose:
                    import traceback
                    traceback.print_exc()
            except Exception as e:
                print(f"Error fetching {endpoint}: {e}", file=sys.stderr)
                if self.verbose:
                    import traceback
                    traceback.print_exc()

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
        """Generate a simple index.html under the output directory linking to saved HTML files."""
        out_index = os.path.join(self.out_dir, 'index.html')
        lines = ['<!doctype html>', '<html><head><meta charset="utf-8"><title>Kanka Slurp Index</title></head><body>']
        lines.append(f'<h1>Index for {self.campaign_id}</h1>')
        for endpoint, items in sorted(self._index.items()):
            lines.append(f'<h2>{endpoint}</h2>')
            # group by type
            by_type: Dict[str, List[Dict[str, str]]] = {}
            for it in items:
                by_type.setdefault(it.get('type') or 'unknown', []).append(it)
            for t, its in sorted(by_type.items()):
                lines.append(f'<h3>{t}</h3>')
                lines.append('<ul>')
                for it in sorted(its, key=lambda x: x.get('name') or ''):
                    # link path is relative to out_dir
                    href = it['path']
                    name = it['name']
                    lines.append(f'<li><a href="{href}">{name}</a></li>')
                lines.append('</ul>')
        lines.append('</body></html>')
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
