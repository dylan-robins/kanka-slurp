"""HTML and text parsers for Kanka Slurp."""

from html.parser import HTMLParser
from typing import Callable, Optional


class ImageLinkRewriter(HTMLParser):
    """HTML parser that rewrites image src attributes to local paths."""

    def __init__(self, local_resolver: Callable[[str], Optional[str]]):
        """Initialize the rewriter.

        Args:
            local_resolver: Callable that takes a URL and returns a local path or None.
        """
        super().__init__()
        self.local_resolver = local_resolver
        self.output = []

    def handle_starttag(self, tag: str, attrs: list):
        """Handle opening tags, rewriting src attributes if present."""
        if tag == "img":
            new_attrs = []
            for name, value in attrs:
                if name == "src" and value:
                    local = self.local_resolver(value)
                    new_attrs.append((name, local or value))
                else:
                    new_attrs.append((name, value))
            attrs_str = " ".join(f'{k}="{v}"' for k, v in new_attrs if v)
            self.output.append(f"<{tag} {attrs_str}/>")
        else:
            attrs_str = " ".join(f'{k}="{v}"' for k, v in attrs if v)
            self.output.append(f"<{tag}{' ' + attrs_str if attrs_str else ''}>")

    def handle_endtag(self, tag: str):
        """Handle closing tags."""
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str):
        """Handle text content."""
        self.output.append(data)

    def get_result(self) -> str:
        """Get the processed HTML result.

        Returns:
            Processed HTML string.
        """
        return "".join(self.output)
