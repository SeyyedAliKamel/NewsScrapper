# Iranian Market News Scraper (NewsScrapper)

A simple Python tool for collecting news from RSS feeds and Google News searches, specifically targeting news about **USD exchange rates, gold, coins, and cryptocurrencies in Iran**. Each news item is scored based on relevance, source credibility, and attention-grabbing keywords. Duplicate articles are merged, and the results are saved to a CSV file.

## Features

- Collects news from RSS and Atom feeds without requiring an API key.
- Topic-based searches using Google News RSS (e.g., `Iran rial dollar`).
- Supports Persian and English keywords (dollar, gold, coins, Tether, Bitcoin, etc.).
- Scores news based on three criteria: relevance, source credibility, and buzzwords.
- Detects duplicate articles and boosts scores for stories covered by multiple sources.
- Saves output to the `output` directory using filenames containing the execution date and time.
- All settings are stored in a single `config.json` file, with no code changes required.
- Uses only the Python standard library (no `pip` installation required).

## Requirements

- Python 3.8 or later
- Internet connection

## Project Structure

```text
NewsScrapper/
├── News.py            # Main program
├── PriceLogger.py     # USD and gold price logging (optional, currently disabled)
├── config.json        # Feeds, keywords, weights, and settings
├── .gitignore
└── output/            # Output files (ignored by Git)
    └── news_2026-10-20_14-35-10.csv
```

## Usage

Run the scraper:

```bash
python News.py
```

To see which news articles were merged:

```bash
python News.py --show-merges
```

Each execution creates a new file in `output/`, with the results sorted by score.

## How It Works

```text
config.json (feeds) → Fetch and Parse → Scoring → Deduplication → CSV Output
```

## Configuration (`config.json`)

| Section | Description |
|---|---|
| `feeds` | News feeds. Each entry can be a URL or an object such as `{"url": "...", "iran_local": true}`. The `iran_local` option indicates that the source is inherently Iranian, so the article does not need to mention Iran explicitly. |
| `source_trust` | Source credibility multiplier between 0 and 1. Matching is performed against part of the publisher's name (e.g., `bbc` also matches `BBC News`). |
| `default_trust` | Default credibility score for sources not listed in `source_trust`. |
| `topic` | Keyword groups: `iran` (Iran-related terms), `asset` (dollar, gold, coins, etc.), `crypto` (cryptocurrencies), `up`, and `down` (price increase and decrease terms). |
| `buzzwords` | Attention-grabbing keywords such as `surge`, `plunge`, and `crisis`. |
| `weights` | Weights for the three scoring criteria: `relevancy`, `source`, and `buzz`. |
| `irrelevant_penalty` | Multiplier applied to the scores of irrelevant articles (default: `0.4`). |
| `min_score` | Minimum score required for an article to be saved. A value of `0` allows all articles to be included. |
| `dedupe` | Duplicate detection settings: `similarity_threshold`, `min_shared_words`, `window_hours`, `boost_per_source`, `max_boost`, as well as custom `synonyms` and `stopwords`. |
| `prices` | Price logging settings. Currently disabled, as explained below. |

**Note:** The `iran`, `asset`, and `crypto` keywords are matched as **whole words** to prevent false matches, such as confusing the Persian word for gold (`طلا`) with the word for divorce (`طلاق`). The `up`, `down`, and `buzzwords` keywords are matched as **prefixes**. For example, `surg` matches both `surge` and `surging`.

## Scoring Method

### 1. Relevance (`relevancy`)

A news article is considered relevant if it mentions Iran and meets at least one of the following conditions:

- Its title contains a financial keyword.
- Its title and summary together contain at least two distinct financial keywords.

Relevant articles receive a relevance score between `0.3` and `1`, depending on the number of financial keywords, the presence of upward or downward movement terms, and whether a financial keyword appears in the title.

Irrelevant articles receive a relevance score of `0`.

### 2. Source Credibility (`source`)

The source score is determined by the publisher's `source_trust` value.

### 3. Buzzwords (`buzz`)

The number of detected buzzwords, capped at `3`, divided by `3`.

### Final Score

```text
score = (w_relevancy × relevancy
       + w_source × source
       + w_buzz × buzz) / sum_of_weights

If the article is irrelevant:
    score = score × irrelevant_penalty
```

### Price Direction (`direction`)

The direction is determined by counting upward and downward keywords in the article text.

This column indicates **the wording and sentiment of the headline**, not the actual change in market prices. For example, "The dollar rises" is classified as `up`, even though it may indicate a decline in the value of the rial.

## Duplicate Detection

| Type | Description | Behavior |
|---|---|---|
| Exact title | Titles are identical after normalization, including character normalization, punctuation removal, Persian/Arabic character normalization, and digit normalization. | Merged into one row. |
| Different wording, same story | After removing extra words and normalizing synonyms, titles share at least 50% of their main words, and their publication times are less than 48 hours apart. | Merged into one row using approximate matching. |
| Repeated daily news | Titles differ only by their date, such as "Exchange rates for October 20." | Only the **latest** version is retained, without an additional score boost. |

For relevant stories covered by multiple **distinct sources**, an additional `0.05` points are awarded for each additional source, up to a maximum boost of `0.2`.

Repeated coverage from the same source does not increase this bonus.

## Output Columns

| Column | Description |
|---|---|
| `score` | Final score, including the duplicate coverage bonus. |
| `base_score` | Score before the duplicate coverage bonus is applied. |
| `relevant` | `yes` or `no`. |
| `direction` | `up`, `down`, `mixed`, or empty. |
| `repeat` | Number of copies of the story (1 means unique). |
| `repeat_sources` | Number of distinct sources covering the story. |
| `repeat_type` | `exact`, `similar`, or empty. |
| `series_editions` | Number of daily editions merged together. |
| `publisher` | Representative publisher for the story. |
| `also_reported_by` | Other sources that reported the same story. |
| `title` / `url` | Article title and link. |
| `published` | Raw publication date provided by the feed. |
| `published_utc` | Publication timestamp converted to UTC. |
| `relevancy` / `source_score` / `buzz` | Individual scoring components. |
| `matched` | Detected financial keywords, useful for debugging. |
| `price_gap_min` | Time difference to the nearest recorded price, available only when price logging is enabled. |

The output column order is defined by the `BASE_COLUMNS` list near the top of `News.py`. To change the order or remove a column, edit that list.

## Price Logging (Currently Disabled)

`PriceLogger.py` can record prices at regular intervals in `output/prices.csv`. This allows `News.py` to associate each news article with the nearest recorded price.

A price source has not yet been selected. Therefore, price-related columns are not generated while the `fields` section in `prices` is empty or the entire section is absent.

## Limitations

- Scores are rule-based estimates and should be adjusted based on actual results.
- Similar-story detection relies on word overlap and may produce false positives or miss duplicates.
- Identical news articles in Persian and English are currently treated as separate stories.
- Only feed titles and summaries are analyzed, not the full article text.
- Some feeds, such as Tehran Times and BBC Persian, have not yet been tested and their URLs may have changed.
- The scraper only sees what is available in the feeds at execution time. Regular execution is necessary to reduce the risk of missing news.

## TODO

### Completed

- [x] Collect news from RSS, Atom, and Google News.
- [x] Score articles based on relevance, source credibility, and buzzwords.
- [x] Separate configuration into `config.json`.
- [x] Generate CSV output with filenames containing the execution date and time.
- [x] Cover cryptocurrency news (Tether, Bitcoin, Nobitex, etc.).
- [x] Detect duplicates using exact titles, similar wording, and daily news patterns.
- [x] Add the `repeat` column and award score bonuses for coverage by multiple sources.

### Pending

- [ ] Select a price source and enable USD and gold price logging.
- [ ] Add a time-range filter (e.g., only the last 24 hours).
- [ ] Test and fix the Tehran Times and BBC Persian feed URLs.
- [ ] Add reliable Persian-language news feeds.
- [ ] Detect duplicate stories across Persian and English.
- [ ] Determine price direction separately for each asset (a rising dollar does not mean a rising rial).
- [ ] Extract full article text (e.g., using `trafilatura`).
- [ ] Analyze the relationship between news publication times and subsequent price movements.
- [ ] Write unit tests.
- [ ] Add functionality for detecting important global news.
 
## Disclaimer

This project is intended solely for news collection and categorization. Scores are approximate and **do not constitute financial advice**. Always verify information against original sources before making financial decisions.