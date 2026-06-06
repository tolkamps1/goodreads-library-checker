import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn
from rich.table import Table

from .goodreads import Book, fetch_tbr
from .library import DigitalResult, LibraryResult, VIRLCatalog

_WORKERS = 3

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check your Goodreads TBR against the VIRL library catalog",
    )
    parser.add_argument(
        "--user-id",
        default=os.environ.get("GOODREADS_USER_ID"),
        help="Goodreads user ID (or set GOODREADS_USER_ID env var)",
    )
    parser.add_argument(
        "--shelf",
        default="to-read",
        help="Goodreads shelf name (default: to-read)",
    )
    parser.add_argument(
        "--branch",
        default=os.environ.get("VIRL_BRANCH"),
        metavar="NAME",
        help="Preferred VIRL branch (partial match, e.g. 'Sooke'). "
             "Books available there are listed first. Also set via VIRL_BRANCH env var.",
    )
    parser.add_argument(
        "--digital",
        action="store_true",
        help="Check ebook/audiobook availability on Libby instead of physical copies",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Also show books that are checked out / unavailable (physical mode only)",
    )
    args = parser.parse_args()

    if not args.user_id:
        console.print("[red]Error:[/red] Goodreads user ID required. "
                      "Pass --user-id or set GOODREADS_USER_ID.")
        sys.exit(1)

    console.print(f"[bold]Fetching '{args.shelf}' shelf for user {args.user_id}...[/bold]")
    try:
        books = fetch_tbr(args.user_id, shelf=args.shelf)
    except Exception as e:
        console.print(f"[red]Failed to fetch Goodreads shelf:[/red] {e}")
        sys.exit(1)

    if not books:
        console.print("[yellow]No books found on shelf.[/yellow]")
        sys.exit(0)

    console.print(f"Found [bold]{len(books)}[/bold] books. Checking VIRL catalog...\n")
    if args.digital:
        _run_virl_digital(books)
    else:
        _run_virl(books, branch=args.branch, show_all=args.show_all)


def _run_virl(books: list[Book], branch: str | None, show_all: bool) -> None:
    results: list[LibraryResult] = []
    not_in_catalog: list[Book] = []
    no_physical_copies: list[Book] = []

    def check_one(book: Book) -> tuple[Book, LibraryResult | None]:
        return book, VIRLCatalog().check(book)

    with Progress(
        SpinnerColumn(), BarColumn(), MofNCompleteColumn(),
        TextColumn("{task.description}"),
        console=console, transient=True,
    ) as progress:
        task = progress.add_task(f"Checking {len(books)} books...", total=len(books))
        lock = Lock()
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            for future in as_completed(pool.submit(check_one, b) for b in books):
                book, result = future.result()
                with lock:
                    if result is None:
                        not_in_catalog.append(book)
                    elif not result.copies:
                        no_physical_copies.append(book)
                    else:
                        results.append(result)
                    progress.advance(task)

    _print_virl_results(
        results,
        not_in_catalog=not_in_catalog,
        no_physical_copies=no_physical_copies,
        branch=branch,
        show_all=show_all,
    )


def _run_virl_digital(books: list[Book]) -> None:
    all_results: list[DigitalResult] = []
    not_found: list[Book] = []

    def check_one(book: Book) -> tuple[Book, list[DigitalResult]]:
        return book, VIRLCatalog().check_digital(book)

    with Progress(
        SpinnerColumn(), BarColumn(), MofNCompleteColumn(),
        TextColumn("{task.description}"),
        console=console, transient=True,
    ) as progress:
        task = progress.add_task(f"Checking {len(books)} books...", total=len(books))
        lock = Lock()
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            for future in as_completed(pool.submit(check_one, b) for b in books):
                book, results = future.result()
                with lock:
                    if results:
                        all_results.extend(results)
                    else:
                        not_found.append(book)
                    progress.advance(task)

    _print_digital_results(all_results, not_found)


def _print_digital_results(results: list[DigitalResult], not_found: list[Book]) -> None:
    available = [r for r in results if r.is_available]
    on_hold = [r for r in results if not r.is_available]

    if not available and not on_hold:
        console.print("[yellow]No books from your TBR are in the VIRL digital catalog.[/yellow]")
    else:
        if available:
            table = Table(title="Available now on Libby", show_lines=True)
            table.add_column("Title", style="bold")
            table.add_column("Author")
            table.add_column("Format", style="cyan")
            table.add_column("Copies", style="green")
            table.add_column("Libby URL", style="dim")
            for r in sorted(available, key=lambda r: r.format):
                copies = f"{r.available_copies}/{r.owned_copies}"
                table.add_row(r.book.title, r.book.author, r.format, copies, r.libby_url)
            console.print(table)
            console.print()

        if on_hold:
            table = Table(title="On hold / waitlist on Libby", show_lines=True)
            table.add_column("Title", style="bold")
            table.add_column("Author")
            table.add_column("Format", style="cyan")
            table.add_column("Copies", style="dim")
            table.add_column("Holds", style="yellow")
            table.add_column("Est. wait", style="yellow")
            table.add_column("Libby URL", style="dim")
            for r in sorted(on_hold, key=lambda r: (r.estimated_wait_days or 999, r.format)):
                copies = f"0/{r.owned_copies}"
                holds = str(r.holds_count)
                wait = f"~{r.estimated_wait_days}d" if r.estimated_wait_days else "—"
                table.add_row(
                    r.book.title, r.book.author, r.format, copies, holds, wait, r.libby_url
                )
            console.print(table)
            console.print()

    if not_found:
        console.print(
            f"\n[dim]{len(not_found)} book(s) not found in VIRL digital catalog:[/dim]"
        )
        for book in not_found:
            console.print(f"  [dim]• {book}[/dim]")


def _matches_branch(branch_name: str, preferred: str) -> bool:
    return preferred.lower() in branch_name.lower()


def _branch_copies(result: LibraryResult, branch: str) -> list:
    return [c for c in result.copies if _matches_branch(c.branch, branch)]


def _print_virl_results(
    results: list[LibraryResult],
    not_in_catalog: list[Book],
    no_physical_copies: list[Book],
    branch: str | None,
    show_all: bool,
) -> None:
    at_preferred: list[LibraryResult] = []
    due_at_preferred: list[LibraryResult] = []
    at_other: list[LibraryResult] = []
    unavailable: list[LibraryResult] = []

    for r in results:
        if branch and any(_matches_branch(b, branch) for b in r.available_branches):
            at_preferred.append(r)
        elif branch and _branch_copies(r, branch):
            due_at_preferred.append(r)
        elif r.is_available:
            at_other.append(r)
        else:
            unavailable.append(r)

    has_anything = at_preferred or due_at_preferred or at_other or (show_all and unavailable)
    if not has_anything:
        console.print("[yellow]No books from your TBR are currently available at VIRL.[/yellow]")
    else:
        if branch and at_preferred:
            _print_virl_table(
                at_preferred,
                title=f"Available at {branch}",
                branch_filter=branch,
                highlight_branch=True,
            )
            console.print()

        if branch and due_at_preferred:
            _print_due_table(due_at_preferred, branch=branch)
            console.print()

        if at_other:
            _print_virl_table(
                at_other,
                title="Available at other VIRL branches",
                branch_filter=None,
                highlight_branch=False,
            )
            console.print()

        if show_all and unavailable:
            _print_virl_table(
                unavailable,
                title="Checked out / unavailable",
                branch_filter=None,
                highlight_branch=False,
                dim=True,
            )
            console.print()

    if no_physical_copies:
        console.print(
            f"\n[dim]{len(no_physical_copies)} book(s) found in catalog but no physical copies "
            f"(likely digital/OverDrive only):[/dim]"
        )
        for book in no_physical_copies:
            console.print(f"  [dim]• {book}[/dim]")

    if not_in_catalog:
        console.print(
            f"\n[dim]{len(not_in_catalog)} book(s) not found in VIRL catalog at all:[/dim]"
        )
        for book in not_in_catalog:
            console.print(f"  [dim]• {book}[/dim]")


def _print_due_table(results: list[LibraryResult], branch: str) -> None:
    table = Table(title=f"Checked out / due at {branch}", show_lines=True)
    table.add_column("Title", style="bold")
    table.add_column("Author")
    table.add_column(f"Status at {branch}", style="yellow")
    table.add_column("Also available elsewhere", style="green")
    table.add_column("Catalog URL", style="dim")

    for r in results:
        branch_statuses = "\n".join(
            f"{c.branch}: {c.status}" for c in _branch_copies(r, branch)
        )
        elsewhere = "\n".join(r.available_branches) if r.available_branches else "—"
        table.add_row(r.book.title, r.book.author, branch_statuses, elsewhere, r.catalog_url)

    console.print(table)


def _print_virl_table(
    results: list[LibraryResult],
    title: str,
    branch_filter: str | None,
    highlight_branch: bool,
    dim: bool = False,
) -> None:
    table = Table(title=title, show_lines=True)
    table.add_column("Title", style="bold" if not dim else "dim bold")
    table.add_column("Author", style="" if not dim else "dim")
    table.add_column("Available at", style="green" if not dim else "dim")
    table.add_column("Catalog URL", style="dim")

    for r in results:
        avail = r.available_branches
        if avail:
            lines = []
            for b in avail:
                if highlight_branch and branch_filter and _matches_branch(b, branch_filter):
                    lines.append(f"[bold green]{b}[/bold green]")
                else:
                    lines.append(b)
            branch_text = "\n".join(lines)
        else:
            branch_text = "[yellow]checked out / on hold[/yellow]"

        table.add_row(r.book.title, r.book.author, branch_text, r.catalog_url)

    console.print(table)
