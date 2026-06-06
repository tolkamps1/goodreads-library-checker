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

OD_AVAILABILITY = "https://thunder.api.overdrive.com/v2/libraries/virl/media/{title_id}/availability"
OD_MEDIA_URL = "https://virl.overdrive.com/media/{title_id}"

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
class DigitalResult:
    book: Book
    title_id: str
    format: str          # "ebook" or "audiobook"
    is_available: bool
    available_copies: int
    owned_copies: int
    holds_count: int
    estimated_wait_days: int | None
    libby_url: str


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

    def check_digital(self, book: Book) -> list[DigitalResult]:
        """Return ebook/audiobook availability from OverDrive/Libby."""
        query = quote_plus(f"{book.title} {book.author}")
        url = KEYWORD_SEARCH.format(query=query)
        try:
            resp = self._get(url)
        except requests.RequestException:
            return []

        title_ids = _extract_digital_title_ids(resp.text, book)
        results: list[DigitalResult] = []
        seen_formats: set[str] = set()

        for title_id, fmt in title_ids:
            if fmt in seen_formats:
                continue
            result = self._od_availability(title_id, fmt, book)
            if result is None:
                continue
            # If we already have an available result for this format, skip worse ones
            existing = next((r for r in results if r.format == result.format), None)
            if existing is None:
                results.append(result)
                if result.is_available:
                    seen_formats.add(result.format)
            elif not existing.is_available and result.is_available:
                results.remove(existing)
                results.append(result)
                seen_formats.add(result.format)

        return results

    def _od_availability(self, title_id: str, fmt: str, book: Book) -> DigitalResult | None:
        url = OD_AVAILABILITY.format(title_id=title_id)
        try:
            resp = requests.get(
                url, timeout=20, headers={"User-Agent": self._UA}
            )
            resp.raise_for_status()
            d = resp.json()
        except (requests.RequestException, ValueError):
            return None

        formats = [f["id"] for f in d.get("formats", [])]
        actual_fmt = "audiobook" if any("audiobook" in f for f in formats) else "ebook"

        return DigitalResult(
            book=book,
            title_id=title_id,
            format=actual_fmt,
            is_available=d.get("isAvailable", False),
            available_copies=d.get("availableCopies", 0),
            owned_copies=d.get("ownedCopies", 0),
            holds_count=d.get("holdsCount", 0),
            estimated_wait_days=d.get("estimatedWaitDays"),
            libby_url=OD_MEDIA_URL.format(title_id=title_id),
        )

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


# --- module-level helpers ---

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


def _extract_digital_title_ids(html: str, book: Book) -> list[tuple[str, str]]:
    """Return (titleID, format) pairs from Sierra search results for digital items."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    title_words = _normalize(book.title)
    author_words = _normalize_author(book.author)

    for row in soup.find_all("tr"):
        cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
        if not cells:
            continue
        row_text = " ".join(cells).lower()
        is_ebook = "ebook" in row_text
        is_audio = "eaudio" in row_text
        if not is_ebook and not is_audio:
            continue
        # Verify title and author appear in the row
        if not (title_words & _normalize(row_text)):
            continue
        if not (author_words & _normalize_author(row_text)):
            continue
        for a in row.find_all("a", href=re.compile(r"overdrive\.com|link\.overdrive")):
            href = a["href"]
            m = re.search(r"titleID=(\d+)", href) or re.search(r"/media/(\d+)", href)
            if m:
                title_id = m.group(1)
                if title_id not in seen:
                    seen.add(title_id)
                    fmt = "audiobook" if is_audio else "ebook"
                    results.append((title_id, fmt))
                    if len(results) >= 6:
                        return results
    return results


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
