"""Constants for Kanka Slurp application."""

# API Configuration
DEFAULT_API_BASE = "https://api.kanka.io/1.0/"
DEFAULT_API_TIMEOUT = 30
IMAGE_DOWNLOAD_TIMEOUT = 60
DEFAULT_PAGE_LIMIT = 100
DEFAULT_RETRY_DELAY = 5
DEFAULT_MIN_INTERVAL = 2.0

# File Size & Content
MAX_IMAGE_SIZE = 50 * 1024 * 1024  # 50 MB
ALLOWED_MIME_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/svg+xml'}

# HTML Field Names from Kanka API
FIELD_ENTRY_HTML = 'entry'
FIELD_ENTRY_PARSED = 'entry_parsed'
FIELD_IMAGE_FULL = 'image_full'
FIELD_URLS = 'urls'
FIELD_URLS_API = 'api'
FIELD_CHILD_ID = 'child_id'

# Checkpoint File
CHECKPOINT_FILENAME = '.checkpoint.json'
