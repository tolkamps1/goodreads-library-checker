from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET

import requests

SHELF_RSS = "https://www.goodreads.com/review/list_rss/{user_id}?shelf={shelf}&per_page=200&page={page}"


@dataclass
class Book:
    title: str
    author: str
    isbn: str
    goodreads_id: str
    goodreads_url: str

    def __str__(self) -> str:
        return f"{self.title} — {self.author}"


def _clean(text: str) -> str:
    text = re.sub(r"\s*\(.*?\)\s*$", "", text)  # strip series info like "(Series, #1)"
    return text.strip()


def fetch_tbr(user_id: str, shelf: str = "to-read") -> list[Book]:
    books: list[Book] = []
    page = 1

    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    while True:
        url = SHELF_RSS.format(user_id=user_id, shelf=shelf, page=page)
        resp = session.get(url, timeout=15)
        resp.raise_for_status()

        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            break

        items = channel.findall("item")
        if not items:
            break

        for item in items:
            title_el = item.find("title")
            author_el = item.find("author_name")
            isbn_el = item.find("isbn")
            book_id_el = item.find("book_id")
            link_el = item.find("link")

            if title_el is None or author_el is None:
                continue

            title = _clean(title_el.text or "")
            author = (author_el.text or "").strip()
            isbn = (isbn_el.text or "").strip() if isbn_el is not None else ""
            goodreads_id = (book_id_el.text or "").strip() if book_id_el is not None else ""
            url = (link_el.text or "").strip() if link_el is not None else ""

            if title:
                books.append(Book(title=title, author=author, isbn=isbn,
                                  goodreads_id=goodreads_id, goodreads_url=url))

        # Goodreads caps TBR lists; stop if we got a full page's worth
        if len(items) < 200:
            break
        page += 1

    return books
