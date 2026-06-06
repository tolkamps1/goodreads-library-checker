from dataclasses import dataclass, field
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from .goodreads import Book
from .library import _normalize, _normalize_author

GVPL_BASE = "https://www.gvpl.ca"
RSS_SEARCH = GVPL_BASE + "/client/rss/hitlist/default/qu={query}"
CATALOG_SEARCH = GVPL_BASE + "/client/en_US/default/search/results?qu={query}&te=ILS"

_ATOM_NS = "http://www.w3.org/2005/Atom"

_FORMAT_PHYSICAL = re.compile(r"^(Book|CD|DVD|Blu-ray|Audiobook|Magazine)", re.IGNORECASE)


@dataclass
class GVPLCopy:
    branch: str
    material_type: str
    status: str  # "Unknown" when JS hasn't run; real status if available


@dataclass
class GVPLResult:
    book: Book
    has_physical: bool
    has_digital: bool
    catalog_url: str
    copies: list[GVPLCopy] = field(default_factory=list)

    @property
    def branches(self) -> list[str]:
        seen: set[str] = set()
        result = []
        for c in self.copies:
            if c.branch not in seen:
                seen.add(c.branch)
                result.append(c.branch)
        return result


def _entry_matches(entry_title: str, entry_author: str, book: Book) -> bool:
    title_words = _normalize(book.title)
    author_words = _normalize_author(book.author)
    title_ok = bool(title_words & _normalize(entry_title))
    author_ok = bool(author_words & _normalize_author(entry_author))
    return title_ok and author_ok


def _parse_content(content_html: str) -> tuple[str, str]:
    """Return (author, format) from RSS entry content HTML."""
    author = re.search(r"by(?:&#160;|\s)+([^<]+)", content_html)
    fmt = re.search(r"(?:Format[:\s]|Electronic Format[:\s])(?:&#160;|\s)*([^<]+)", content_html)
    return (
        re.sub(r"\s+", " ", author.group(1)).strip() if author else "",
        fmt.group(1).strip() if fmt else "",
    )


def _parse_item_table(html: str) -> list[GVPLCopy]:
    """Parse copies from the detail page availability table.

    Branch names are server-rendered in hidden default divs.
    Status is populated by JS and will be 'Unknown' from raw HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="detailItemTable")
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    copies = []
    for row in tbody.find_all("tr"):
        lib_divs = row.find_all("div", class_="asyncFieldLIBRARY")
        status_divs = row.find_all("div", class_="asyncFieldSD_ITEM_STATUS")
        itype_td = row.find("td", class_="detailItemsTable_ITYPE")

        # Hidden default divs have server-rendered branch names (status is "Unknown")
        lib_hidden = next((d for d in lib_divs if "hidden" in d.get("class", [])), None)
        status_hidden = next((d for d in status_divs if "hidden" in d.get("class", [])), None)

        if lib_hidden:
            copies.append(GVPLCopy(
                branch=lib_hidden.get_text(strip=True),
                material_type=itype_td.get_text(strip=True) if itype_td else "",
                status=status_hidden.get_text(strip=True) if status_hidden else "Unknown",
            ))

    return copies


class GVPLCatalog:
    _UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, delay: float = 0.3):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = self._UA
        self._delay = delay
        self._last_request = 0.0

    def _get(self, url: str) -> requests.Response:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        resp = self._session.get(url, timeout=15)
        self._last_request = time.monotonic()
        resp.raise_for_status()
        return resp

    def check(self, book: Book) -> GVPLResult | None:
        query = quote_plus(f"{book.title} {book.author}")
        try:
            resp = self._get(RSS_SEARCH.format(query=query))
        except requests.RequestException:
            return None

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return None

        has_physical = False
        has_digital = False
        first_physical_url: str | None = None
        first_digital_url: str | None = None

        for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
            title_el = entry.find(f"{{{_ATOM_NS}}}title")
            id_el = entry.find(f"{{{_ATOM_NS}}}id")
            link_el = entry.find(f"{{{_ATOM_NS}}}link")
            content_el = entry.find(f"{{{_ATOM_NS}}}content")

            if title_el is None or id_el is None:
                continue

            entry_title = title_el.text or ""
            record_id = id_el.text or ""
            link = link_el.get("href", "") if link_el is not None else ""
            content_html = content_el.text or "" if content_el is not None else ""

            entry_author, fmt = _parse_content(content_html)

            if not _entry_matches(entry_title, entry_author, book):
                continue

            is_physical = record_id.startswith("ent://SD_ILS") and bool(
                _FORMAT_PHYSICAL.match(fmt)
            )

            if is_physical:
                has_physical = True
                if first_physical_url is None:
                    first_physical_url = link
            else:
                has_digital = True
                if first_digital_url is None:
                    first_digital_url = link

        if not has_physical and not has_digital:
            return None

        copies: list[GVPLCopy] = []
        if first_physical_url:
            try:
                detail_resp = self._get(first_physical_url)
                copies = _parse_item_table(detail_resp.text)
            except requests.RequestException:
                pass

        catalog_url = CATALOG_SEARCH.format(query=quote_plus(f"{book.title} {book.author}"))
        return GVPLResult(
            book=book,
            has_physical=has_physical,
            has_digital=has_digital,
            catalog_url=catalog_url,
            copies=copies,
        )
