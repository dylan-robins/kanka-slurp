"""Command-line interface for Kanka Slurp."""

import argparse
from typing import Dict, List, Optional
from dotenv import load_dotenv
import os

from .api import KankaSlurp
from .constants import DEFAULT_API_BASE


def load_config_from_env(dotenv_path: Optional[str] = None) -> Dict[str, str]:
    """Load configuration from environment variables.
    
    Args:
        dotenv_path: Path to .env file. If provided, loads from that file;
                    otherwise loads from current environment.
                    
    Returns:
        Dictionary with keys 'token', 'campaign', and 'api_base'.
        
    Raises:
        RuntimeError: If required environment variables are not set.
    """
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
    """Main entry point for Kanka Slurp CLI.
    
    Args:
        argv: Command-line arguments. If None, uses sys.argv.
        
    Returns:
        Exit code (0 for success).
    """
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
    
    slurper = KankaSlurp(
        cfg['token'],
        cfg['campaign'],
        api_base=cfg.get('api_base', DEFAULT_API_BASE),
        out_dir=args.out,
        verbose=args.verbose,
    )
    slurper.slurp(fetch_details=bool(args.details))
    return 0
