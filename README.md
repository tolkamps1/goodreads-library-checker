# goodreads-library-checker

Check your Goodreads TBR (to-read) shelf against the [Vancouver Island Regional Library (VIRL)](https://virl.bc.ca) catalog and see which books are available to pick up right now — no holds required.

## How it works

1. Fetches your Goodreads `to-read` shelf via the public RSS feed
2. Looks up each book in the VIRL catalog by ISBN (falling back to keyword and title search)
3. Parses copy availability across all VIRL branches
4. Displays a prioritized table: available at your preferred branch first, then checked-out copies with due dates, then available elsewhere

## Setup

```bash
git clone https://github.com/yourusername/goodreads-library-checker.git
cd goodreads-library-checker
pip install -e .
```

Copy `.env.example` to `.env` and fill in your details:

```bash
cp .env.example .env
```

Edit `.env`:

```
GOODREADS_USER_ID=123456789   # the number in your Goodreads profile URL
VIRL_BRANCH=Sooke             # optional: your preferred branch for prioritized results
```

## Usage

```bash
# Basic — uses values from .env
check-library

# Pass values directly
check-library --user-id 123456789 --branch Sooke

# Show all TBR books including checked-out ones
check-library --all

# Check a different shelf
check-library --shelf currently-reading
```

Your Goodreads shelf must be **public** for the RSS feed to work. You can check this in Goodreads → Settings → Privacy.

## Requirements

- Python 3.10+
- A public Goodreads profile

## Notes

- Only physical copies are checked by default
- Add `--digital` flag to see eBooks/audiobooks via OverDrive/Libby
- Availability is live at time of running; it won't auto-refresh
- Requests are rate-limited to be polite to VIRL's servers
