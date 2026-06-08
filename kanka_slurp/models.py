"""Data models for Kanka Slurp."""

from dataclasses import dataclass, asdict
from typing import Optional, List
import yaml


@dataclass
class EntityMetadata:
    """Metadata for a Kanka entity with YAML frontmatter generation."""
    
    id: str
    name: str
    entity_type: str
    image_full: Optional[str] = None
    tags: Optional[List[str]] = None
    urls_view: Optional[str] = None
    updated_at: Optional[str] = None
    
    def to_yaml_frontmatter(self) -> str:
        """Generate valid YAML frontmatter from dataclass.
        
        Returns:
            YAML frontmatter string with --- delimiters.
        """
        # Filter out None values and convert to dict
        data = {k: v for k, v in asdict(self).items() if v is not None}
        yaml_str = yaml.dump(data, default_flow_style=False, allow_unicode=True)
        return f"---\n{yaml_str}---\n"
