"""
ScrapeExecutor: Fetch and extract website content for downstream use.

Extracts main text and optional metadata from a URL. Uses Python stdlib
(urllib + html.parser) — no external dependencies required.
"""

import re
import urllib.request
import urllib.error
from html.parser import HTMLParser
from typing import Any, ClassVar, Dict, Optional

from pydantic import BaseModel, Field

from apflow.core.base import BaseTask
from apflow.core.execution.errors import ValidationError
from apflow.core.extensions.decorators import executor_register
from apflow.logger import get_logger

logger = get_logger(__name__)


class ScrapeInputSchema(BaseModel):
    url: str = Field(description="Target website URL to scrape")
    max_chars: int = Field(
        default=5000, description="Maximum number of characters to extract (default: 5000)"
    )
    extract_metadata: bool = Field(
        default=True,
        description="Whether to extract metadata like title and description (default: True)",
    )


class ScrapeOutputSchema(BaseModel):
    result: str = Field(description="Scraped website content as string")


class _TextExtractor(HTMLParser):
    """Simple HTML-to-text extractor using stdlib html.parser."""

    def __init__(self) -> None:
        super().__init__()
        self._text: list[str] = []
        self._title: str = ""
        self._description: str = ""
        self._in_title = False
        self._skip_tags = {"script", "style", "nav", "footer", "header"}
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip_tags:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            attr_dict = dict(attrs)
            if attr_dict.get("name", "").lower() == "description":
                self._description = attr_dict.get("content", "") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self._title = text
        elif self._skip_depth == 0:
            self._text.append(text)

    def get_text(self) -> str:
        return "\n".join(self._text)


def _fetch_and_extract(url: str, max_chars: int = 5000, extract_metadata: bool = True) -> str:
    """Fetch URL and extract text content using stdlib only."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (compatible; apflow/0.20; +https://aiperceivable.com)"),
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        html = response.read().decode("utf-8", errors="replace")

    parser = _TextExtractor()
    parser.feed(html)

    parts: list[str] = []
    if extract_metadata:
        if parser._title:
            parts.append(f"Title: {parser._title}")
        if parser._description:
            parts.append(f"Description: {parser._description}")
        parts.append(f"URL: {url}")
        parts.append("")

    body = parser.get_text()
    # Collapse multiple blank lines
    body = re.sub(r"\n{3,}", "\n\n", body)
    parts.append("---\n\nMain Text:")
    parts.append(body)

    full = "\n".join(parts)

    if len(full) > max_chars:
        full = full[:max_chars]
        last_period = full.rfind(".")
        if last_period > max_chars * 0.8:
            full = full[: last_period + 1]
        else:
            full += "..."

    return full


@executor_register()
class ScrapeExecutor(BaseTask):
    """Scrape website content and extract text with optional metadata."""

    id = "scrape_executor"
    name = "Website Scraper Executor"
    description = (
        "Scrape website content and metadata from a given URL. "
        "Returns main text with configurable character limits."
    )
    tags = ["scrape", "website", "content"]
    cancelable: bool = False
    inputs_schema: ClassVar[type[BaseModel]] = ScrapeInputSchema
    outputs_schema: ClassVar[type[BaseModel]] = ScrapeOutputSchema

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        url = inputs.get("url")
        if not url:
            raise ValidationError("'url' is required for scraping.")

        max_chars = inputs.get("max_chars", 5000)
        extract_metadata = inputs.get("extract_metadata", True)

        logger.info(f"Scraping website: {url} (max_chars={max_chars})")

        try:
            content = _fetch_and_extract(url, max_chars, extract_metadata)
            return {"result": content}
        except urllib.error.URLError as e:
            raise ValidationError(f"Failed to access {url}: {e}") from e
        except Exception as e:
            logger.error(f"Failed to scrape {url}: {e}")
            raise ValidationError(f"Failed to scrape {url}: {e}") from e

    def get_demo_result(self, task: Any, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return {
            "result": (
                "Title: Example Demo\nDescription: Example description.\n"
                "URL: https://example.com/demo\n\n---\n\nMain Text:\n"
                "This is a demo of the website scraping executor."
            ),
        }
