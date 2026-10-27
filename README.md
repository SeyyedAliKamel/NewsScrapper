# Iranian Market News Scraper (NewsScrapper)

A Python tool that collects news about **USD exchange rates, gold, coins, and cryptocurrencies in Iran**, scores it, and checks whether the news is followed by price moves.

It does three jobs:

1. **Collect and score** news from RSS/Atom feeds and Google News searches.
2. **Label** each relevant article with what the price did after it (+1h, +6h, +24h by default).
3. **Compare** new news with labeled history to estimate what may happen next, and **backtest** whether that estimate beats guessing.

It also has a local web UI. Everything is stored in a local SQLite database. There is no machine learning and no training: predictions come from similarity to past articles.

## Features

- Collects news from RSS and Atom feeds, with no API key. Date-range feeds allow backfilling history.
- Persian and English keywords (dollar, gold, coins, Tether, Bitcoin, etc.).
- Scores articles on relevance, source credibility, and buzzwords.
- Merges duplicate and same-story articles, and boosts stories covered by several sources.
- Optionally downloads article bodies (respects `robots.txt`) for better similarity matching.
- Downloads price candles (Nobitex UDF API by default), or imports your own CSV.
- Labels articles with the real price move that followed.
- Predicts from the most similar past articles, and backtests the predictor against simple baselines.
- Local web UI and CSV export.
- Standard library only, no `pip install` needed.

## Requirements

- Python 3.8 or later
- Internet connection
- Optional: `pip install googlenewsdecoder` (Python 3.11+) so article bodies can be scraped from Google News links

## Project Structure

```text
NewsScrapper/
├── News.py         # Entry point: command-line interface only
├── common.py       # Config, database, text helpers, article scoring
├── data.py         # Feeds, article bodies, price candles, labeling
├── analysis.py     # Similarity, predict, backtest, top list, CSV export, doctor
├── ui.py           # Local web UI (run through `News.py ui`)
├── config.json     # Feeds, keywords, weights, market, settings
└── output/         # Created automatically (ignore in Git)
    ├── news.db             # SQLite database: articles, candles, outcomes
    └── news_labeled.csv    # Created by `export`
```

`PriceLogger.py` from earlier versions is no longer used. Prices now come from candles (see below).

The database path is relative to the folder you run the command from. Always run everything from the same folder, or the UI and the commands will use different databases.

## Quick Start

```bash
python News.py collect            # store today's feeds
python News.py bodies             # optional: download article text
python News.py label              # attach price moves to articles
python News.py top                # best-scored stories
python News.py predict            # compare today's news with history
python News.py backtest           # does the predictor beat guessing?
python News.py ui                 # open the web UI
```

`python News.py update` runs collect, bodies, label, top, and predict in one go.

To build up history, backfill older dates (needs `date_feeds` in the config):

```bash
python News.py collect --from 2026-10-01 --to 2026-10-26
```

## Commands

| Command | What it does |
|---|---|
| `collect [--date D \| --from D --to D]` | Stores news. With no date it reads `feeds`; with dates it reads `date_feeds` once per day. |
| `bodies [--limit N] [--all]` | Downloads article text. Relevant articles only unless `--all`. |
| `rescore` | Recomputes all scores. Run after editing weights, trust, or topic terms. |
| `top [--hours 24] [--n 15] [--show-merges]` | Best-scored stories, with duplicates merged. |
| `label` | Looks up the price at news time and the move after each horizon. |
| `predict [--hours 24]` | Compares recent news with labeled history. |
| `backtest [--horizon H]` | Tests the predictor on history, using only what was known at the time. |
| `export [--show-merges]` | Writes scored and labeled stories to `output/news_labeled.csv`. |
| `update` | collect + bodies + label + top + predict. |
| `ui [--port 8765]` | Starts the local web UI. |
| `doctor` | Counts rows in each table and explains where the pipeline stops. |
| `probe` | Shows the raw price API response (for debugging the API). |
| `import-candles FILE.csv` | Uses your own price file instead of the API. |
| `fake-prices` | **Test only.** Stores random prices under the symbol `FAKE`. |

## Web UI

```bash
python News.py ui            # opens http://127.0.0.1:8765/
python News.py ui --port 9000
```

- **Stories:** best-scored stories with merged duplicates, sources, and labeled outcomes. Filter by period, relevance, or text.
- **Predict & backtest:** runs the same analysis as the command line.
- **Update data:** buttons for collect, backfill by date range, bodies, label, rescore, export, and update-everything.

It is a local app that reads your database, so it cannot be hosted online. It only listens on `127.0.0.1`, and actions need a custom header, so other websites cannot trigger jobs. Jobs run one at a time. It re-reads `config.json` on every action.

## How It Works

```text
config.json → collect → score → store (SQLite)
                                   │
              price candles ──→ label ──→ outcomes (up / down / flat per horizon)
                                   │
                    predict / backtest / top / export / UI
```

## Configuration (`config.json`)

| Section | Description |
|---|---|
| `feeds` | Live news feeds. Each entry is a URL or an object such as `{"url": "...", "iran_local": true}`. `iran_local` marks an inherently Iranian source, so articles need not mention Iran. |
| `date_feeds` | Feeds for backfilling. The URL contains `{after}` and `{before}`, replaced by each day in the range. |
| `source_trust` | Source credibility between 0 and 1. Matched against part of the publisher name (`bbc` matches `BBC News`). |
| `default_trust` | Credibility for sources not in `source_trust` (default `0.3`). |
| `topic` | Keyword groups: `iran`, `asset` (dollar, gold, coins), `crypto`, `up`, `down`. |
| `buzzwords` | Attention words such as `surge`, `plunge`, `crisis`. |
| `weights` | Weights for `relevancy`, `source`, `buzz` (defaults `0.5`, `0.3`, `0.2`). |
| `irrelevant_penalty` | Multiplier for irrelevant articles' scores (default `0.4`). |
| `min_score` | Minimum score for an article to be used. `0` includes everything. |
| `dedupe` | `window_hours` (default 48), `boost_per_source` (0.05), `max_boost` (0.2), plus custom `stopwords` and `synonyms`. |
| `market` | Price source. See below. |
| `horizons_hours` | How long after the news to measure the price move (default `[1, 6, 24]`). |
| `flat_pct` | Moves smaller than this percentage count as "flat" (default `0.1`). |
| `scrape_delay` | Seconds between article downloads (default `1.5`). |
| `user_agent` | User agent for news and article requests. |
| `predict` | Predictor settings. See below. |

**`market`**

| Key | Meaning |
|---|---|
| `symbol` | Market to track, e.g. `USDTIRT`. |
| `resolution` | Candle size in minutes, as a string (`"15"`), or `"D"` for daily. |
| `base_url` | Price API address (default `https://apiv2.nobitex.ir`). |
| `chunk_days` | How many days to request per API call. |
| `user_agent` | Nobitex asks bots to use `TraderBot/<name>-<version>`. |
| `fetch` | Set `false` to stop calling the API (for imported or fake prices). |

**`predict`**

| Key | Default | Meaning |
|---|---|---|
| `min_similarity` | `0.25` | A past article must be at least this similar (0 to 1). |
| `k` | `8` | At most this many neighbours per story. |
| `min_matches` | `3` | Fewer neighbours than this means no vote. |
| `cluster_similarity` | `0.6` | Articles this similar and close in time count as one story. |
| `title_weight` | `3` | Title words count this many times. |
| `body_chars` | `3000` | Only the start of the body is compared. |
| `use_scores` | `true` | Neighbours count in proportion to their score. |

**Example (minimal):**

```json
{
  "feeds": {
    "gnews": {"url": "https://news.google.com/rss/search?q=Iran+rial+dollar"}
  },
  "date_feeds": {
    "gnews": {"url": "https://news.google.com/rss/search?q=Iran+rial+dollar+after:{after}+before:{before}"}
  },
  "topic": {
    "iran":   ["iran", "ایران"],
    "asset":  ["dollar", "gold", "coin", "دلار", "طلا", "سکه"],
    "crypto": ["bitcoin", "tether", "تتر"],
    "up":     ["rise", "surg", "soar"],
    "down":   ["fall", "drop", "plung"]
  },
  "source_trust": {"reuters": 0.9, "bbc": 0.85},
  "market": {"symbol": "USDTIRT", "resolution": "15"}
}
```

**Matching note:** `iran`, `asset`, and `crypto` terms are matched as **whole words** (so the Persian word for gold, `طلا`, does not match the word for divorce, `طلاق`). `up`, `down`, and `buzzwords` are matched as **prefixes** (`surg` matches `surge` and `surging`). Topic terms are checked against the title and summary only, not the body.

## Scoring Method

### 1. Relevance (`relevancy`)

An article is relevant if it mentions Iran (or comes from an `iran_local` feed) and meets one of these:

- Its title contains an `asset` or `crypto` term.
- Its title and summary together contain at least two distinct asset or crypto terms.

Relevant articles get a score from `0.3` to `1`, depending on how many asset terms they contain, whether they contain up/down terms, and whether an asset term is in the title. Irrelevant articles get `0`.

### 2. Source Credibility (`source`)

The `source_trust` value for the publisher, or `default_trust`.

### 3. Buzzwords (`buzz`)

The number of buzzwords found, capped at 3, divided by 3.

### Final Score

```text
score = (w_relevancy × relevancy + w_source × source + w_buzz × buzz) / sum_of_weights

If the article is irrelevant:
    score = score × irrelevant_penalty
```

### Direction (`direction`)

Counts up and down keywords in the title and summary. This column describes **the wording of the headline**, not the market. "The dollar rises" is `up` even though it means the rial fell. The backtest includes a "headline-words-only" baseline for comparison.

## Duplicate Detection

Articles are stored once per URL and once per publisher and normalized title. Beyond that, stories are merged when they are **similar in wording and published close together**.

| Case | Behavior |
|---|---|
| Same story, same or different wording | Articles whose TF-IDF cosine similarity is at least `predict.cluster_similarity` (default `0.6`) and that were published within `dedupe.window_hours` (default 48) are merged into one story. |
| Repeated daily news | Titles that differ only by a date are detected (`series_key`). Daily editions are counted in `series_editions`, and count as one neighbour in predictions, so they do not get repeated votes. |

For relevant stories covered by several **distinct sources**, `0.05` is added per extra source, up to `0.2`. Repeats from one source add nothing.

Use `--show-merges` on `top` or `export` to see which titles were merged.

## Prices, Candles, and Labeling

A **candle** is a summary of a price over a fixed period (for example 15 minutes): open, high, low, and close. The program downloads candles for the configured `market`, stores the open and close, and uses the close.

`label` looks at every relevant, dated article and records:

- the price at news time: the close of the last candle that had **already finished** (it never looks ahead)
- the price after each horizon (+1h, +6h, +24h)
- the percentage change, and a direction: `up`, `down`, or `flat` (within `flat_pct`)

An outcome is only stored once its horizon has passed. Articles with no price data nearby are skipped.

Results are saved in the `outcomes` table. **Outcomes are not tagged with a symbol.** Fake and real outcomes would mix, so clear them before switching price sources (see below).

### Using your own prices

If the API does not work, export a CSV with `time` (epoch seconds, epoch milliseconds, or ISO) and `close` columns (optional `open`) and run:

```bash
python News.py import-candles prices.csv
```

The file's spacing must match `market.resolution` (an hourly file needs `"resolution": "60"`). Set `"fetch": false` so the program does not call the API.

### Testing without real prices

```bash
python News.py fake-prices
```

Then set `"market": {"symbol": "FAKE", "fetch": false, "resolution": "15"}` and run `label`. Run `fake-prices` **after** collecting your articles, with the same resolution you will use. The program prints a warning whenever the symbol is `FAKE`.

The fake prices are a random walk unrelated to the news. They are good for checking that the pipeline runs and that `backtest` lands near a coin flip. They tell you nothing about whether news predicts the real price.

To clear fake data before switching to real prices:

```bash
python -c "import sqlite3; db=sqlite3.connect('output/news.db'); db.execute('DELETE FROM outcomes'); db.execute(\"DELETE FROM candles WHERE symbol='FAKE'\"); db.commit(); print('cleared')"
```

## Prediction and Backtest

`predict` takes recent relevant articles, groups them into stories, and for each story finds the most similar **past** articles whose outcome is already known:

- Similarity is TF-IDF cosine similarity over the title (weighted) and the start of the article body or summary.
- Past neighbours must meet `min_similarity`, and there must be at least `min_matches` of them, otherwise the story gets no vote.
- Only one neighbour counts per series or story.
- Each neighbour is weighted by similarity and score. Each story gets one vote.
- The output shows the expected move, the share of up/down/flat, how many stories had evidence, and a confidence label (`moderate` needs at least 3 stories and 65% agreement, otherwise `low`).

`backtest` replays history. For each labeled article it predicts using only the history known at that moment, then compares the result with the real outcome. It reports accuracy against two baselines: always guessing the majority direction, and trusting the headline's own words. **Only trust the predictor if it clearly beats the majority guess on a decent sample.** If the prediction is close to the base rate and the neighbours are weak matches, the news is adding nothing.

## Output

The database `output/news.db` has three tables: `articles`, `candles`, and `outcomes`.

`export` writes `output/news_labeled.csv` (one row per merged story, sorted by score):

| Column | Description |
|---|---|
| `score` | Final score, including the multiple-source bonus. |
| `base_score` | Score before the bonus. |
| `relevant` | `yes` or `no`. |
| `repeat_type` | `exact`, `similar`, or empty. |
| `publisher` / `title` / `url` | Representative article for the story. |
| `relevancy` / `source_score` / `buzz` | Score components. |
| `matched` | Asset keywords found (useful for debugging). |
| `direction` | `up`, `down`, `mixed`, or empty (headline wording). |
| `repeat` / `repeat_sources` | Number of copies of the story / number of distinct sources. |
| `also_reported_by` | Other sources for the same story. |
| `series_editions` | Number of daily editions of this headline. |
| `published_utc` | Publication time in UTC. |
| `body_status` | `ok`, `short`, `robots`, `gnews_link`, `error:...`, or empty. |
| `price_at_news` | Price at news time (if labeled). |
| `pct_Nh` / `dir_Nh` | Percentage change and direction after N hours (one pair per horizon). |
| `summary` / `body` | Feed summary and downloaded article text. |

Column order is defined by the `cols` list in `export()` in `analysis.py`.

## Troubleshooting

Run:

```bash
python News.py doctor
```

It prints the exact database file, row counts for each table, your market settings, which candles are stored, and a diagnosis in pipeline order. Common causes of empty tables:

- **No articles:** no `feeds` in the config, or the feeds fail (`collect` prints `[!]` lines).
- **Nothing is relevant:** the `topic` block needs `iran` terms and `asset` or `crypto` terms. An article must match both. After editing, run `rescore`.
- **No outcomes:** the `market` symbol or resolution does not match the stored candles, or candles do not cover the article dates.
- **Different folders:** the database is relative to the folder you run from.

`python News.py probe` shows the raw price API response if the API is not working.

## Limitations

- Scores are rule-based estimates. Adjust them with real results.
- Similar-story detection relies on word overlap and can merge unrelated stories or miss duplicates (the default `cluster_similarity` of `0.6` can miss rewordings).
- Persian and English versions of the same news are treated as separate stories.
- Broad topic terms let unrelated assets (gold, oil, Bitcoin) count as relevant for one price. A tighter list gives fewer but better matches.
- Article bodies are only scraped when you run `bodies`. Google News wrapper links cannot be scraped without `googlenewsdecoder`. Some sites block scraping, and `robots.txt` is respected.
- Predictions need a lot of labeled history. Many articles come from the same days and stories, so the number of independent events is much smaller than the article count.
- Price moves have many causes. A pattern in past news is not proof that news causes later moves.
- Some feeds, such as Tehran Times and BBC Persian, have not been tested, and their URLs may have changed.
- The scraper only sees what feeds show at run time. Run `collect` regularly to avoid missing news.

## TODO

### Completed

- [x] Collect news from RSS, Atom, and Google News (live and by date range).
- [x] Score articles on relevance, source credibility, and buzzwords.
- [x] Configuration in `config.json`.
- [x] Cover cryptocurrency news (Tether, Bitcoin, Nobitex, etc.).
- [x] Detect duplicates (exact, similar wording, daily editions) and boost multi-source stories.
- [x] Store everything in SQLite and export labeled data to CSV.
- [x] Download full article text (standard library, respects `robots.txt`).
- [x] Price source: candles from the Nobitex API, or imported from CSV.
- [x] Time-range filter (`top --hours`, `predict --hours`).
- [x] Analyze the link between news timing and later price moves (`label`, `predict`, `backtest`).
- [x] Local web UI and `doctor` diagnostics.
- [x] Fake prices for pipeline testing.

### Pending

- [ ] Run the full pipeline on real prices and tune settings (`min_similarity`, `cluster_similarity`, topic terms) using `backtest`.
- [ ] Tag outcomes with the price symbol so fake and real data cannot mix.
- [ ] Fake prices that react to the news, to test that the predictor can find a planted signal.
- [ ] Test and fix the Tehran Times and BBC Persian feed URLs.
- [ ] Add reliable Persian-language news feeds.
- [ ] Detect duplicate stories across Persian and English.
- [ ] Determine price direction per asset (a rising dollar does not mean a rising rial).
- [ ] Write unit tests.
- [ ] Add detection of important global news.

## Disclaimer

This project is intended for news collection and categorization. Scores and predictions are approximate and **do not constitute financial advice**. Always verify information against original sources before making financial decisions.