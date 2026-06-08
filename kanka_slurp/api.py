"""Core API client for Kanka Slurp."""

import json
import time
import os
import re
import unicodedata
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from tqdm import tqdm
import html2text
import yaml

from .constants import (
    DEFAULT_API_BASE,
    DEFAULT_API_TIMEOUT,
    IMAGE_DOWNLOAD_TIMEOUT,
    DEFAULT_PAGE_LIMIT,
    MAX_IMAGE_SIZE,
    DEFAULT_RETRY_DELAY,
    DEFAULT_MIN_INTERVAL,
    ALLOWED_MIME_TYPES,
    FIELD_ENTRY_HTML,
    FIELD_ENTRY_PARSED,
    FIELD_IMAGE_FULL,
    FIELD_URLS,
    FIELD_URLS_API,
    FIELD_CHILD_ID,
    CHECKPOINT_FILENAME,
)
from .logging_config import setup_logging
from .models import EntityMetadata
from .parsers import ImageLinkRewriter


class KankaSlurp:
    """Download all data from a Kanka campaign into a local folder."""
    
    def __init__(
        self,
        token: str,
        campaign_id: str,
        api_base: str = DEFAULT_API_BASE,
        out_dir: str = "data",
        verbose: bool = False,
    ):
        """Initialize KankaSlurp client.
        
        Args:
            token: Kanka API token.
            campaign_id: Kanka campaign ID (numeric string).
            api_base: Base URL for Kanka API.
            out_dir: Output directory for downloaded data.
            verbose: Enable verbose logging.
            
        Raises:
            ValueError: If token, campaign_id, or other inputs are invalid.
        """
        # Setup logging first
        self.logger = setup_logging(verbose)
        
        # Validate inputs
        if not token or not isinstance(token, str):
            raise ValueError("token must be a non-empty string")
        if not campaign_id or not isinstance(campaign_id, str):
            raise ValueError("campaign_id must be a non-empty string")
        if not campaign_id.isdigit():
            raise ValueError(f"campaign_id must be numeric, got: {campaign_id}")
        
        self.token = token
        self.campaign_id = campaign_id
        self.api_base = api_base.rstrip('/') + '/'
        self.out_path = Path(out_dir)
        self.out_path.mkdir(parents=True, exist_ok=True)
        
        # Setup HTTP session
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        })
        
        # Throttle configuration
        self.min_interval = float(os.getenv('KANKA_MIN_INTERVAL', DEFAULT_MIN_INTERVAL))
        self._last_request = 0.0
        
        # Index for generating index.md
        self._index: Dict[str, List[Dict[str, str]]] = {}
        
        # Checkpoint for resume capability
        self.checkpoint_file = self.out_path / CHECKPOINT_FILENAME
        self.checkpoint = self._load_checkpoint()
        
        # Cache for local file lookups (optimization)
        self._file_lookup_cache: Dict[str, Optional[str]] = {}
        
        self.logger.info(f"Initialized KankaSlurp for campaign {campaign_id}")

    # ========================================================================
    # Checkpoint Management
    # ========================================================================

    def _load_checkpoint(self) -> Dict[str, Any]:
        """Load checkpoint from file if it exists.
        
        Returns:
            Dictionary with checkpoint data, or empty dict if file doesn't exist.
        """
        if self.checkpoint_file.exists():
            try:
                return json.loads(self.checkpoint_file.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, IOError) as e:
                self.logger.warning(f"Failed to load checkpoint: {e}")
                return {}
        return {}

    def _save_checkpoint(self):
        """Save current checkpoint to file."""
        try:
            self.checkpoint_file.write_text(
                json.dumps(self.checkpoint, indent=2),
                encoding='utf-8'
            )
        except IOError as e:
            self.logger.error(f"Failed to save checkpoint: {e}")

    def _mark_processed(self, endpoint: str, item_id: str):
        """Mark an item as processed in checkpoint.
        
        Args:
            endpoint: API endpoint name.
            item_id: Item identifier.
        """
        endpoint_key = f"details__{endpoint}"
        processed = self.checkpoint.setdefault(endpoint_key, [])
        if item_id not in processed:
            processed.append(item_id)
            self._save_checkpoint()

    def _is_processed(self, endpoint: str, item_id: str) -> bool:
        """Check if an item has already been processed.
        
        Args:
            endpoint: API endpoint name.
            item_id: Item identifier.
            
        Returns:
            True if item has been processed, False otherwise.
        """
        endpoint_key = f"details__{endpoint}"
        return item_id in self.checkpoint.get(endpoint_key, [])

    # ========================================================================
    # Rate Limiting & Throttling
    # ========================================================================

    def _throttle(self):
        """Ensure at least min_interval seconds have passed since last request."""
        if self._last_request <= 0:
            return
        elapsed = time.time() - self._last_request
        if elapsed < self.min_interval:
            to_sleep = self.min_interval - elapsed
            time.sleep(to_sleep)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        """Make a GET request with automatic rate limit handling and retry logic.
        
        Records the request time BEFORE checking status to ensure consistent throttling.
        
        Args:
            path: API endpoint path (relative to base).
            params: Query parameters.
            
        Returns:
            Successful HTTP response.
            
        Raises:
            requests.HTTPError: If HTTP error occurs.
            requests.RequestException: If request fails.
        """
        url = urljoin(self.api_base, path)
        
        while True:
            self.logger.debug(f"GET {url} params={params}")
            self._throttle()
            
            try:
                resp = self.session.get(url, params=params, timeout=DEFAULT_API_TIMEOUT)
                self._last_request = time.time()  # Record timing immediately
                
                if resp.status_code == 429:
                    retry = int(resp.headers.get('Retry-After', DEFAULT_RETRY_DELAY))
                    self.logger.warning(f"Rate limited, sleeping {retry}s...")
                    time.sleep(retry)
                    continue
                
                resp.raise_for_status()
                return resp
                
            except requests.HTTPError as e:
                self.logger.error(f"HTTP {resp.status_code} for {url}: {resp.text[:500]}")
                raise
            except requests.RequestException as e:
                self.logger.error(f"Request failed for {url}: {e}")
                raise

    # ========================================================================
    # Pagination
    # ========================================================================

    def fetch_paginated(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Fetch all paginated results from an endpoint.
        
        Args:
            endpoint: API endpoint name.
            params: Query parameters.
            
        Returns:
            List of all items from all pages.
        """
        params = dict(params or {})
        params.setdefault('page', 1)
        params.setdefault('limit', DEFAULT_PAGE_LIMIT)
        all_items: List[Dict[str, Any]] = []
        
        self.logger.info(f"Fetching endpoint: {endpoint}")
        
        while True:
            path = f"campaigns/{self.campaign_id}/{endpoint}"
            resp = self._get(path, params=params)
            data = resp.json()
            
            # Kanka usually returns data in a 'data' key, but sometimes returns list directly
            items = data.get('data') if isinstance(data, dict) and 'data' in data else data
            if not isinstance(items, list):
                items = [items]
            
            if not items:
                break
            
            all_items.extend(items)
            
            # Check pagination meta
            meta = data.get('meta') if isinstance(data, dict) else None
            if meta and isinstance(meta, dict):
                pagination = meta.get('pagination')
                if pagination:
                    current = pagination.get('current_page')
                    last = pagination.get('last_page')
                    if current is not None and last is not None and current < last:
                        params['page'] = current + 1
                        continue
            
            # Fallback: if response had fewer than requested, assume end
            if len(items) < params.get('limit', DEFAULT_PAGE_LIMIT):
                break
            
            params['page'] = params.get('page', 1) + 1
        
        return all_items

    # ========================================================================
    # File Operations
    # ========================================================================

    def save_json(self, endpoint: str, data: List[Dict[str, Any]]):
        """Save data as JSON to endpoint file.
        
        Args:
            endpoint: Endpoint name (used for filename).
            data: List of items to save.
        """
        out_file = self.out_path / f"{endpoint}.json"
        out_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        self.logger.info(f"Saved {len(data)} objects to {out_file}")

    def _extract_filename_from_url(self, url: str) -> Optional[str]:
        """Safely extract filename from URL, ignoring query params.
        
        Args:
            url: URL to extract filename from.
            
        Returns:
            Filename or None if extraction failed.
        """
        if not url:
            return None
        parsed = urlparse(url)
        filename = Path(parsed.path).name
        return filename if filename else None

    def _extract_id_from_url(self, url: str) -> Optional[str]:
        """Extract stable ID from API URL path.
        
        Args:
            url: API URL.
            
        Returns:
            ID or None if extraction failed.
        """
        if not url:
            return None
        parsed = urlparse(url)
        return Path(parsed.path).name if Path(parsed.path).name else None

    def _find_local_markdown_candidates(self, item_id: str) -> List[Path]:
        """Find Markdown files that belong to a given item ID."""
        candidates: List[Path] = []
        for path in self.out_path.rglob("*.md"):
            if path.name == "index.md":
                continue
            stem = path.stem
            if stem == item_id or stem.startswith(f"{item_id}-"):
                candidates.append(path)
        return candidates

    def _find_local_markdown(self, item_id: str) -> Optional[Path]:
        """Find the best local Markdown file for an item ID."""
        candidates = self._find_local_markdown_candidates(item_id)
        if not candidates:
            return None
        exact = next((path for path in candidates if path.stem == item_id), None)
        if exact:
            return exact
        if len(candidates) == 1:
            return candidates[0]
        self.logger.debug(
            f"Multiple Markdown files match item {item_id}; using newest: "
            f"{[str(path.relative_to(self.out_path)) for path in candidates]}"
        )
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _read_markdown_frontmatter(self, md_path: Path) -> Dict[str, Any]:
        """Read YAML frontmatter from a Markdown file."""
        try:
            content = md_path.read_text(encoding='utf-8')
        except OSError:
            return {}

        if not content.startswith('---\n'):
            return {}

        end = content.find('\n---\n', 4)
        if end == -1:
            return {}

        raw = content[4:end]
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            return {}

        return data if isinstance(data, dict) else {}

    def _get_local_updated_at(self, item_id: str) -> Optional[str]:
        """Return the stored updated_at for a local entity document, if any."""
        md_path = self._find_local_markdown(item_id)
        if not md_path:
            return None
        data = self._read_markdown_frontmatter(md_path)
        updated_at = data.get('updated_at')
        return str(updated_at) if updated_at is not None else None

    def _remove_stale_markdown_files(self, item_id: str, keep_path: Path):
        """Remove stale Markdown files for an item after rewriting it."""
        for path in self._find_local_markdown_candidates(item_id):
            if path != keep_path:
                try:
                    path.unlink()
                except OSError:
                    self.logger.warning(f"Failed to remove stale Markdown file: {path}")

    def _find_local_for_url(self, url: str) -> Optional[str]:
        """Find local file path for a remote URL, returns relative path.
        
        Checks multiple locations: media dir, and falls back to directory walk.
        Results are cached for performance.
        
        Args:
            url: Remote URL to find locally.
            
        Returns:
            Relative path to local file or None if not found.
        """
        if not url or not url.startswith('http'):
            return None
        
        # Check cache first
        if url in self._file_lookup_cache:
            return self._file_lookup_cache[url]
        
        filename = self._extract_filename_from_url(url)
        if not filename:
            return None
        
        # Check common locations
        candidates = [
            self.out_path / 'media' / filename,
            self.out_path / filename,
        ]
        
        for candidate in candidates:
            if candidate.exists():
                result = str(candidate.relative_to(self.out_path))
                self._file_lookup_cache[url] = result
                return result
        
        # Last resort: walk directory tree, but avoid guessing when filenames collide.
        matches = [item for item in self.out_path.rglob(filename) if item.is_file()]
        if len(matches) == 1:
            result = str(matches[0].relative_to(self.out_path))
            self._file_lookup_cache[url] = result
            return result
        if len(matches) > 1:
            self.logger.debug(
                f"Ambiguous local match for {url}: {[str(item.relative_to(self.out_path)) for item in matches]}"
            )
        
        self._file_lookup_cache[url] = None
        return None

    def download_image(self, url: str, subdir: str = "files") -> Optional[str]:
        """Download an image/media file with validation.
        
        Validates MIME type and file size before downloading.
        
        Args:
            url: URL to download.
            subdir: Subdirectory to save to (relative to out_path).
            
        Returns:
            Path relative to out_dir, or None if download failed.
        """
        if not url:
            return None
        
        resp = None
        try:
            self.logger.debug(f"Downloading image from {url}")
            self._throttle()
            
            resp = requests.get(url, stream=True, timeout=IMAGE_DOWNLOAD_TIMEOUT)
            self._last_request = time.time()
            
            # Validate MIME type
            content_type = resp.headers.get('content-type', '').split(';')[0]
            if content_type and content_type not in ALLOWED_MIME_TYPES:
                self.logger.warning(f"Skipping image with invalid MIME type {content_type}: {url}")
                return None
            
            # Validate size before downloading
            content_length = resp.headers.get('content-length')
            if content_length:
                try:
                    size = int(content_length)
                    if size > MAX_IMAGE_SIZE:
                        self.logger.warning(f"Skipping oversized image ({size} bytes): {url}")
                        return None
                except ValueError:
                    pass
            
            try:
                resp.raise_for_status()
            except requests.HTTPError:
                self.logger.warning(f"Failed to download image: {resp.status_code} {url}")
                return None
            
            # Download with size validation
            subdir_path = self.out_path / subdir
            subdir_path.mkdir(parents=True, exist_ok=True)
            
            filename = self._extract_filename_from_url(url)
            if not filename:
                self.logger.warning(f"Could not extract filename from URL: {url}")
                return None
            
            out_file = subdir_path / filename
            downloaded = 0
            
            with open(out_file, 'wb') as f:
                for chunk in resp.iter_content(1024 * 64):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > MAX_IMAGE_SIZE:
                            self.logger.error(f"File exceeded max size during download: {url}")
                            out_file.unlink()
                            return None
            
            result = str(out_file.relative_to(self.out_path))
            self._file_lookup_cache[url] = result
            self.logger.debug(f"Downloaded image to {result}")
            return result
            
        except Exception as e:
            self.logger.warning(f"Failed to download image {url}: {e}")
            return None
        finally:
            if resp is not None:
                resp.close()

    def extract_and_download_files(self, items: List[Dict[str, Any]], endpoint: str):
        """Extract and download file references from items.
        
        Args:
            items: List of items to scan for file references.
            endpoint: Endpoint name (for progress description).
        """
        downloaded = 0
        
        for it in tqdm(items, desc=f"Files in {endpoint}", leave=False):
            for key, val in list(it.items()):
                if isinstance(val, dict):
                    for candidate in ('full', 'url'):
                        url = val.get(candidate)
                        if url and isinstance(url, str) and url.startswith('http'):
                            saved = self.download_image(url, subdir='media')
                            if saved:
                                downloaded += 1
        
        if downloaded:
            self.logger.info(f"Downloaded {downloaded} files for endpoint {endpoint}")

    # ========================================================================
    # HTML/Markdown Processing
    # ========================================================================

    def convert_html_to_markdown(self, html: str) -> str:
        """Convert HTML to Markdown using html2text.
        
        Args:
            html: HTML content to convert.
            
        Returns:
            Markdown string.
        """
        try:
            h = html2text.HTML2Text()
            h.ignore_images = False
            h.body_width = 0
            return h.handle(html or '')
        except Exception as e:
            self.logger.warning(f"Failed to convert HTML to Markdown: {e}")
            return html or ''

    def _rewrite_image_links(self, md: str, entity_type: Optional[str]) -> str:
        """Rewrite image links in Markdown to use local files.
        
        Uses regex for Markdown syntax and HTMLParser for embedded HTML tags.
        
        Args:
            md: Markdown content to rewrite.
            entity_type: Entity type (for logging).
            
        Returns:
            Markdown with rewritten links.
        """
        import re
        
        # Replace markdown image links: ![alt](url "title")
        md_img_re = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
        
        def md_img_sub(match):
            alt = match.group(1)
            link = match.group(2).strip()
            parts = link.split()
            url = parts[0].strip('"')
            local = self._find_local_for_url(url)
            if local:
                title = ' ' + ' '.join(parts[1:]) if len(parts) > 1 else ''
                return f'![{alt}]({local}{title})'
            return match.group(0)
        
        md = md_img_re.sub(md_img_sub, md)
        
        # Replace regular markdown links [text](url)
        link_re = re.compile(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)')
        
        def link_sub(match):
            text = match.group(1)
            link = match.group(2).strip()
            parts = link.split()
            url = parts[0].strip('"')
            local = self._find_local_for_url(url)
            if local:
                return f'[{text}]({local})'
            return match.group(0)
        
        md = link_re.sub(link_sub, md)
        
        # Replace inline HTML img tags using HTMLParser
        if '<img' in md:
            rewriter = ImageLinkRewriter(self._find_local_for_url)
            try:
                rewriter.feed(md)
                md = rewriter.get_result()
            except Exception as e:
                self.logger.warning(f"Failed to parse HTML in Markdown: {e}")
        
        return md

    def _build_markdown_filename(self, item_id: str, name: str) -> str:
        """Build a stable, readable markdown filename for an entity."""
        cleaned_name = unicodedata.normalize("NFKD", name.strip()).encode("ascii", "ignore").decode("ascii")
        cleaned_name = cleaned_name.lower()
        cleaned_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", cleaned_name)
        cleaned_name = re.sub(r"[^a-z0-9]+", "-", cleaned_name)
        cleaned_name = re.sub(r"-{2,}", "-", cleaned_name).strip("-_. ")

        if cleaned_name:
            return f"{item_id}-{cleaned_name}.md"
        return f"{item_id}.md"

    def _save_item_markdown(
        self,
        endpoint: str,
        item_id: str,
        html: str,
        metadata: EntityMetadata,
    ) -> str:
        """Save item as Markdown with YAML frontmatter.
        
        Args:
            endpoint: API endpoint.
            item_id: Item identifier.
            html: HTML content to convert.
            metadata: EntityMetadata object.
            
        Returns:
            Path relative to out_dir.
        """
        entity_type = metadata.entity_type
        base = self.out_path / entity_type
        base.mkdir(parents=True, exist_ok=True)
        
        # Convert HTML to Markdown
        md_body = self.convert_html_to_markdown(html)
        
        # Rewrite image links
        md_body = self._rewrite_image_links(md_body, entity_type)
        
        # Create file
        out_file = base / self._build_markdown_filename(item_id, metadata.name or "")
        
        # Build YAML frontmatter
        frontmatter = metadata.to_yaml_frontmatter()
        
        # Write file
        title = metadata.name or ''
        content = frontmatter + '\n'
        if title:
            content += f'# {title}\n\n'
        content += md_body
        
        out_file.write_text(content, encoding='utf-8')
        self.logger.debug(f"Saved Markdown for {endpoint}/{item_id} -> {out_file}")
        
        return str(out_file.relative_to(self.out_path))

    # ========================================================================
    # Detail Fetching
    # ========================================================================

    def _determine_entity_type(self, endpoint: str, item: Dict[str, Any], payload: Dict[str, Any], api_url: Optional[str]) -> str:
        """Determine the entity type for an item.
        
        Args:
            endpoint: Endpoint name.
            item: Item from list response.
            payload: Full payload object.
            api_url: API URL if available.
            
        Returns:
            Entity type string.
        """
        # Prefer explicit payload type
        t = (payload.get('type') if isinstance(payload, dict) else None) or item.get('type')
        if t:
            return str(t).lower()
        
        # Try to parse from API URL
        if api_url:
            try:
                parsed = urlparse(api_url)
                parts = [p for p in parsed.path.split('/') if p]
                if 'campaigns' in parts:
                    idx = parts.index('campaigns')
                    if len(parts) > idx + 2:
                        return parts[idx + 2]
                for seg in reversed(parts):
                    if not seg.isdigit():
                        return seg
            except Exception:
                pass
        
        return endpoint

    def fetch_items_details(self, endpoint: str, items: List[Dict[str, Any]], update_mode: bool = False):
        """Fetch detailed page for each item and save it.
        
        Args:
            endpoint: API endpoint name.
            items: List of items to fetch details for.
            update_mode: If True, only refresh files whose updated_at changed.
        """
        self.logger.info(f"Fetching details for {len(items)} items in {endpoint}")
        updated = 0
        skipped = 0
        
        for it in tqdm(items, desc=f"Details {endpoint}"):
            # Get API URL
            urls = it.get(FIELD_URLS) or {}
            api_url = urls.get(FIELD_URLS_API) if isinstance(urls, dict) else None
            
            # Get item ID
            item_id = None
            if api_url and isinstance(api_url, str) and api_url.startswith('http'):
                item_id = self._extract_id_from_url(api_url)
            
            if not item_id:
                item_id = str(it.get(FIELD_CHILD_ID) or it.get('id') or '')
            
            if not item_id:
                self.logger.debug(f"Skipping item without id: {it}")
                continue
            
            # Check if already processed
            if not update_mode and self._is_processed(endpoint, item_id):
                self.logger.debug(f"Skipping already processed {endpoint}/{item_id}")
                continue

            if update_mode:
                local_updated_at = self._get_local_updated_at(item_id)
                server_updated_at = it.get('updated_at')
                if local_updated_at and server_updated_at and str(local_updated_at) == str(server_updated_at):
                    self.logger.debug(f"Skipping unchanged {endpoint}/{item_id}")
                    skipped += 1
                    continue
            
            try:
                # Fetch the detail
                if api_url and api_url.startswith('http'):
                    self.logger.debug(f"GET detail {api_url}")
                    self._throttle()
                    resp = self.session.get(api_url, timeout=DEFAULT_API_TIMEOUT)
                    self._last_request = time.time()
                    try:
                        resp.raise_for_status()
                    except requests.HTTPError:
                        self.logger.warning(f"Detail HTTP error for {api_url}: {resp.status_code}")
                        continue
                    data = resp.json()
                else:
                    path = f"campaigns/{self.campaign_id}/{endpoint}/{item_id}"
                    resp = self._get(path)
                    data = resp.json()
                
                # Normalize payload
                payload = data.get('data') if isinstance(data, dict) and 'data' in data else data
                
                if not isinstance(payload, dict):
                    self.logger.warning(f"Unexpected payload format for {endpoint}/{item_id}")
                    continue
                
                # Extract HTML content
                entry_html = payload.pop(FIELD_ENTRY_HTML, None)
                entry_parsed = payload.pop(FIELD_ENTRY_PARSED, None)
                html_to_save = entry_parsed or entry_html or ''
                
                # Determine entity type
                entity_type = self._determine_entity_type(endpoint, it, payload, api_url)
                
                # Download primary image
                image_full = payload.get(FIELD_IMAGE_FULL)
                if image_full and isinstance(image_full, str) and image_full.startswith('http'):
                    saved_image = self.download_image(image_full, subdir=entity_type)
                    if saved_image:
                        payload[FIELD_IMAGE_FULL] = saved_image
                        payload['image_file'] = saved_image
                
                # Build metadata
                metadata = EntityMetadata(
                    id=payload.get('id') or item_id,
                    name=payload.get('name') or it.get('name') or item_id,
                    entity_type=entity_type,
                    image_full=payload.get(FIELD_IMAGE_FULL),
                    tags=payload.get('tags'),
                    urls_view=urls.get('view') if isinstance(urls, dict) else None,
                    updated_at=payload.get('updated_at'),
                )
                
                # Download embedded files before saving Markdown so link rewriting can resolve them.
                self.extract_and_download_files([payload], f"{endpoint}_detail")
                
                # Save Markdown
                md_rel = self._save_item_markdown(endpoint, item_id, html_to_save, metadata)
                payload['entry_file'] = md_rel
                updated += 1

                if update_mode:
                    self._remove_stale_markdown_files(item_id, self.out_path / md_rel)
                
                # Add to index
                self._index.setdefault(endpoint, []).append({
                    'name': metadata.name,
                    'path': md_rel,
                    'type': entity_type,
                })
                
                # Mark as processed
                self._mark_processed(endpoint, item_id)
                
            except Exception as e:
                self.logger.error(f"Failed to fetch detail for {endpoint}/{item_id}: {e}")

        return {
            'updated': updated,
            'skipped': skipped,
            'total': len(items),
        }

    # ========================================================================
    # Index Generation
    # ========================================================================

    def _generate_index(self):
        """Generate index.md from collected entries."""
        out_index = self.out_path / 'index.md'
        lines = [f'# Index for {self.campaign_id}', '']
        
        for endpoint, items in sorted(self._index.items()):
            lines.append(f'## {endpoint}')
            
            # Group by type
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
        
        out_index.write_text('\n'.join(lines), encoding='utf-8')
        self.logger.info(f"Wrote index to {out_index}")

    # ========================================================================
    # Main Slurp Method
    # ========================================================================

    def slurp(self, update: bool = False):
        """Auto-discovery mode — fetch entities and always fetch entity details.
        
        Args:
            update: If True, refresh changed entity pages based on updated_at.
        """
        summary = {}
        
        try:
            self.logger.info("Auto-discovery: fetching 'entities'")
            entities = self.fetch_paginated('entities')
            
            # Extract and download embedded files
            self.extract_and_download_files(entities, 'entities')
            
            # Fetch details for all entities.
            if update and entities:
                summary['updated'] = self.fetch_items_details('entities', entities, update_mode=True)
            elif entities:
                summary['details'] = self.fetch_items_details('entities', entities)
            
            summary['entities'] = len(entities)
            
        except Exception as e:
            self.logger.error(f"Error fetching entities: {e}")
            entities = []
        
        self.logger.info("Skipping discovery of other endpoints; exported `entities` only.")
        
        # Generate index
        try:
            if self._index:
                self._generate_index()
        except Exception as e:
            self.logger.error(f"Failed to generate index: {e}")
        
        # Print summary
        self.logger.info("Done. Summary:")
        for k, v in summary.items():
            self.logger.info(f" - {k}: {v}")
