from dataclasses import dataclass, field
import re
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from .goodreads import Book

CATALOG_BASE = "https://sierra-app.virl.bc.ca"
ISBN_SEARCH = CATALOG_BASE + "/search/?searchtype=i&searcharg={isbn}&searchscope=45"
KEYWORD_SEARCH = CATALOG_BASE + "/search/?searchtype=X&SORT=D&searcharg={query}&searchscope=45"
TITLE_SEARCH = CATALOG_BASE + "/search/?searchtype=t&SORT=D&searcharg={query}&searchscope=45"
RECORD_URL = CATALOG_BASE + "/record={bib_id}"

_STATUS_PATTERN = re.compile(
    r"^(Available|DUE|In Transit|On Holdshelf|MISSING|ON ORDER|LOST|CLAIMS RETURNED)",
    re.IGNORECASE,
)


def _normalize(text: str) -> set[str]:
    """Return lowercase words of 4+ chars, stripped of punctuation."""
    return {w for w in re.sub(r"[^\w\s]", "", text.lower()).split() if len(w) >= 4}


_MARC_RELATORS = {
    "author", "editor", "translator", "illustrator", "narrator",
    "contributor", "adapter", "compiler", "auteur",
}


def _normalize_author(text: str) -> set[str]:
    """Like _normalize but strips MARC relator terms and falls back to 2+ char words."""
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    words = [w for w in words if w not in _MARC_RELATORS]
    long_words = {w for w in words if len(w) >= 4}
    return long_words if long_words else {w for w in words if len(w) >= 2}


def _bib_matches(cells: list[str], book: Book) -> bool:
    """Check that a bib record's title/author loosely matches the expected book."""
    record_title = ""
    record_author = ""
    for i, cell in enumerate(cells):
        if cell == "Title" and i + 1 < len(cells):
            record_title = cells[i + 1].lower()
        if cell == "Author" and i + 1 < len(cells):
            record_author = cells[i + 1].lower()

    title_words = _normalize(book.title)
    author_words = _normalize_author(book.author)

    # Need at least one significant title word and one author word to match
    title_ok = bool(title_words & _normalize(record_title))
    author_ok = bool(author_words & _normalize_author(record_author))
    return title_ok and author_ok


@dataclass
class CopyStatus:
    branch: str
    status: str

    @property
    def is_available(self) -> bool:
        return self.status.strip().lower() == "available"


@dataclass
class LibraryResult:
    book: Book
    bib_id: str
    catalog_url: str
    copies: list[CopyStatus] = field(default_factory=list)

    @property
    def available_branches(self) -> list[str]:
        # Deduplicate while preserving order (multiple copies at same branch)
        seen: set[str] = set()
        branches = []
        for c in self.copies:
            if c.is_available and c.branch not in seen:
                seen.add(c.branch)
                branches.append(c.branch)
        return branches

    @property
    def is_available(self) -> bool:
        return bool(self.available_branches)


class VIRLCatalog:
    _UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, delay: float = 0.3):
        # Search session: accumulates scope cookies from catalog searches
        self._search_session = requests.Session()
        self._search_session.headers["User-Agent"] = self._UA
        self._delay = delay
        self._last_request = 0.0

    def _get(self, url: str, *, fresh: bool = False) -> requests.Response:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        # Record-page fetches must use a cookie-free session to get full holdings.
        # The catalog restricts to a scoped view when SESSION_SCOPE=1 is present.
        session = requests.Session() if fresh else self._search_session
        session.headers["User-Agent"] = self._UA
        resp = session.get(url, timeout=15)
        self._last_request = time.monotonic()
        resp.raise_for_status()
        return resp

    def check(self, book: Book) -> LibraryResult | None:
        seen: set[str] = set()

        def try_bibs(bibs: list[str]) -> LibraryResult | None:
            for bib_id in bibs:
                if bib_id in seen:
                    continue
                seen.add(bib_id)
                record_url = RECORD_URL.format(bib_id=bib_id)
                try:
                    resp = self._get(record_url, fresh=True)
                except requests.RequestException:
                    continue
                cells = _extract_cells(resp.text)
                if not _bib_matches(cells, book):
                    continue
                return LibraryResult(
                    book=book, bib_id=bib_id, catalog_url=record_url,
                    copies=_parse_copies(cells),
                )
            return None

        if book.isbn and book.isbn not in ("", "0"):
            bib = self._isbn_lookup(book.isbn)
            if bib:
                result = try_bibs([bib])
                if result:
                    return result

        result = try_bibs(self._keyword_lookup(book.title, book.author))
        if result:
            return result

        return try_bibs(self._title_lookup(book.title))

    def _isbn_lookup(self, isbn: str) -> str | None:
        url = ISBN_SEARCH.format(isbn=isbn)
        try:
            resp = self._get(url)
        except requests.RequestException:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=re.compile(r"/record=b\d+")):
            m = re.search(r"/record=(b\d+)", a["href"])
            if m:
                return m.group(1)

        return _first_bib(soup)

    def _title_lookup(self, title: str) -> list[str]:
        query = quote_plus(title)
        url = TITLE_SEARCH.format(query=query)
        try:
            resp = self._get(url)
        except requests.RequestException:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=re.compile(r"/record=b\d+")):
            m = re.search(r"/record=(b\d+)", a["href"])
            if m:
                return [m.group(1)]

        return _all_bibs(soup, limit=5)

    def _keyword_lookup(self, title: str, author: str) -> list[str]:
        query = quote_plus(f"{title} {author}")
        url = KEYWORD_SEARCH.format(query=query)
        try:
            resp = self._get(url)
        except requests.RequestException:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        # Single result — landed directly on a record page
        for a in soup.find_all("a", href=re.compile(r"/record=b\d+")):
            m = re.search(r"/record=(b\d+)", a["href"])
            if m:
                return [m.group(1)]

        return _all_bibs(soup, limit=5)


# --- module-level helpers (no network) ---

def _extract_cells(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [
        c for c in (
            td.get_text(separator=" ", strip=True).replace("\xa0", "").strip()
            for td in soup.find_all("td")
        )
        if c
    ]


def _parse_copies(cells: list[str]) -> list[CopyStatus]:
    copies: list[CopyStatus] = []
    for i, cell in enumerate(cells):
        if _STATUS_PATTERN.match(cell) and i >= 2:
            branch = cells[i - 2].strip(" -")
            if branch:
                copies.append(CopyStatus(branch=branch, status=cell))
    return copies


def _first_bib(soup: BeautifulSoup) -> str | None:
    for a in soup.find_all("a", href=re.compile(r"requestbrowse~b\d+")):
        m = re.search(r"requestbrowse~(b\d+)", a["href"])
        if m:
            return m.group(1)
    return None


def _all_bibs(soup: BeautifulSoup, limit: int = 50) -> list[str]:
    seen: set[str] = set()
    bibs: list[str] = []
    for a in soup.find_all("a", href=re.compile(r"requestbrowse~b\d+")):
        m = re.search(r"requestbrowse~(b\d+)", a["href"])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            bibs.append(m.group(1))
            if len(bibs) >= limit:
                break
    return bibs
