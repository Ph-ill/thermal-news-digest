# Daily digest

A morning news receipt for an 80 mm, 203 dpi thermal printer. One edition is built for the local date, rendered to raw TSPL2, and sent through a CUPS queue with `lp -o raw`. Home Assistant calls a loopback trigger at 05:00. Two people read the same strip.

The generator collects candidates from each feed, then asks an OpenAI-compatible chat model to pick the items worth printing and to write a one- or two-sentence summary. If the model cannot be used, a deterministic picker still drops obituaries, ads, investing spam, headlines that repeat themselves, and stories printed in the last week. The print is not cancelled because the model failed.

Personal values stay in the environment. See `.env.example`. Do not commit names, addresses, hostnames, or keys.

## Architecture

| Piece | Role |
| --- | --- |
| `digest_generator.py` | Fetches weather, news, and calendars. Writes `output/digest-source-YYYY-MM-DD.json` once. |
| `digest_settings.py` | Reads env settings: feeds, section order, titles, model, history, bot updates. |
| `digest_picker.py` | Scores candidates, calls the model, validates JSON, and remembers recent URLs and titles. |
| `receipt_digest.py` | Checks the section list, draws an SVG, rasterizes it, and packs a TSPL2 bitmap. |
| `print_digest_safe.sh` | Holds `print.lock` with `flock`, builds the source file if today's is missing, then renders or submits. |
| `receipt_digest_trigger.py` | Listens on `127.0.0.1` only. `POST /v1/digest/dry-run` or `POST /v1/digest/submit`. |
| `digest-history.json` | URLs and normalized titles from recent editions. Gitignored. |
| `bot-updates/YYYY-MM-DD.json` | Optional notes from household bots. Gitignored. Contract in [docs/bot-updates.md](docs/bot-updates.md). |

Pillow wraps text and measures the bitmap. `rsvg-convert` (librsvg) turns the SVG into a PNG. DejaVu Sans Mono is the face on the strip. No other third-party Python packages are required.

## Run flow

1. At 05:00 Home Assistant posts to `http://127.0.0.1:8793/v1/digest/submit` with an empty body.
2. The trigger starts `print_digest_safe.sh --submit`. A second press while the lock is held gets HTTP 409.
3. The shell script takes an exclusive non-blocking `flock` on `print.lock`.
4. If `output/digest-source-YYYY-MM-DD.json` is missing, empty, or cannot be rendered, `digest_generator.py` builds it. The date is the calendar date in `DIGEST_TZ` (default `America/Chicago`), in both the shell and the generator. The generator stops fetching after about 75 seconds and prints whatever it already has, so the whole trigger stays inside 120 seconds.
   - NOAA daytime forecast for the digest date (the overnight period is not used).
   - Up to 16 candidates from each news feed.
   - Verified events from the neighborhood calendar, then the chamber calendar.
   - The bot-updates file, if it is valid and newer than about 30 hours.
   - One chat-completions call to pick and summarize the news. Any failure uses the fallback picker.
   - Recent URLs and titles are skipped, then today's picks are appended to `digest-history.json`.
5. `receipt_digest.py` checks the section order and draws a strip whose height follows the text, up to 6000 dots. Past that, lines are shortened until the strip fits. Characters XML cannot carry are removed, so a damaged source file can still print. A bitmap that touches the edge, or is almost blank or almost solid, is still refused.
6. `--submit` sends the `.tspl` file with `lp -d "$DIGEST_PRINTER_QUEUE" -o raw`. The job id is stored in `state.json`. A `completed` or still-`submitted` job is not sent again. A `failed` job is sent on the next `--submit`.
7. Later presses the same day reuse the source file, so the model is not called again. `--reprint` (or `POST /v1/digest/reprint`) prints another physical copy of that same file. The trigger returns a status code and the renderer's JSON. It does not return stderr. If the trigger's 120 seconds run out, it kills the whole process group so `print.lock` is not left held.

`--dry-run` renders the files and does not call `lp`.

Default section order:

1. WEATHER
2. GLOBAL
3. LOCAL
4. EVENTS TODAY / SOON
5. SPACEX/SPACE STOCK
6. UK
7. FILM/TV
8. BOT UPDATES, only when the day's file is valid

BOT UPDATES is optional. The other sections are required unless `DIGEST_SECTION_ORDER` removes them. A missing required section, a section out of order, or the old MUSIC section is rejected before anything is sent to the printer. MUSIC was removed on purpose: music notes arrive as a bot update instead of a daily placeholder.

When the bot section is present, each news section prints one item and a shorter summary. When it is absent, each news section prints two items and a longer summary. Events stay at most three. The strip still stops at 6000 dots (750 mm).

Sample strips, rendered from fixture data:

![Receipt without bot updates](docs/samples/without-bot-updates.png)

![Receipt with bot updates](docs/samples/with-bot-updates.png)

## Printer setup

The target is a 4BARCODE-style 80 mm printer that speaks TSPL2 at 203 dpi. 203 dpi is 8 dots/mm, so the printable width is 640 dots.

- Install the printer in CUPS as a raw queue. The host queue can forward to an IPP raw queue on a print server that owns the USB device.
- `DIGEST_PRINTER_QUEUE` (default `receipt`) must already exist. The shell script checks `lpstat -p`.
- The bitmap is TSPL `BITMAP` mode 0. In the packed bytes, bit 0 prints black and bit 1 leaves the paper white. Direction is 1, tear is on, and the label height follows the rendered strip.
- The renderer needs `rsvg-convert`, DejaVu Sans Mono (`fonts-dejavu-core`), and Pillow.

```bash
sudo apt-get install librsvg2-bin fonts-dejavu-core
python3 -m pip install pillow
```

A manual render, with no printer:

```bash
python3 digest_generator.py --out output/digest-source-$(date +%F).json
python3 receipt_digest.py --dry-run --source output/digest-source-$(date +%F).json
```

## Settings

Copy `.env.example` to `.env`. Empty values fall back to the defaults below. The systemd unit loads `.env` with `EnvironmentFile=`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DIGEST_HOME` | script directory | Where `output/`, `state.json`, and the history file live. |
| `DIGEST_PRINTER_QUEUE` | `receipt` | CUPS raw queue. |
| `DIGEST_TRIGGER_PORT` | `8793` | Loopback port. |
| `DIGEST_TZ` | `America/Chicago` | Edition date, event times, and the forecast date. The host zone can differ. |
| `DIGEST_LOCATION_LABEL` | `near Springfield` | Line under the title. |
| `DIGEST_WEATHER_LAT`, `DIGEST_WEATHER_LON` | Springfield, Illinois | NOAA point. |
| `DIGEST_LOCAL_NEWS_QUERY` | `Springfield` | Google News query for the local section. |
| `DIGEST_LOCAL_FEED` | unset | Full local feed URL. When set, the local query is not used. |
| `DIGEST_WEATHER_POINT` | NOAA point for the lat/lon | Override the forecast point URL. |
| `DIGEST_LOCAL_CITY` | `Springfield` | City name used when filtering chamber events. |
| `DIGEST_NEIGHBORHOOD_EVENTS_URL` | example neighborhood calendar | Primary events page. |
| `DIGEST_NEIGHBORHOOD_NAME` | `Neighborhood` | Label for events that match the markers. |
| `DIGEST_NEIGHBORHOOD_MARKERS` | `neighborhood` | Comma-separated words that mark a neighborhood event. |
| `DIGEST_CHAMBER_EVENTS_URL` | example chamber calendar | Fallback events page. |
| `DIGEST_GLOBAL_FEED` | BBC world RSS | Global section. |
| `DIGEST_UK_FEED` | BBC UK RSS | UK section. |
| `DIGEST_FILM_FEED` | Hollywood Reporter movie news | Film and TV section. |
| `DIGEST_MARKETS_NEWS_QUERY` | `SpaceX OR Rocket Lab OR ASTS stock` | Google News query for the markets section. |
| `DIGEST_MARKETS_FEED` | unset | Full feed URL. When set, the markets query is not used. |
| `DIGEST_FILM_TERMS` | `anime,thriller,western,animation` | Preferred words for the fallback film picker. They are not a hard filter. |
| `DIGEST_TASTE` | character-driven thrillers, anime, westerns, offbeat animation | Taste note sent to the model. |
| `DIGEST_SECTION_ORDER` | the eight ids listed above | Comma-separated ids. Unknown or repeated ids are ignored and the default order is used. |
| `DIGEST_SECTION_TITLES` | unset | JSON object of id to header, for example `{"markets":"MARKETS"}`. |
| `DIGEST_SECTION_TITLE_*` | the headers in the sample | One variable per section (`WEATHER`, `GLOBAL`, `LOCAL`, `EVENTS`, `MARKETS`, `UK`, `FILM`, `BOT_UPDATES`). Overrides the JSON object. Max 32 characters. |
| `DIGEST_LLM_BASE_URL` | `https://api.x.ai/v1` | Chat-completions origin, without `/chat/completions`. |
| `DIGEST_LLM_API_KEY` | empty | Bearer token. Empty means fallback picker only. |
| `DIGEST_LLM_MODEL` | `grok-4.7` | Model id. Change this when the provider renames the model. |
| `DIGEST_LLM_TIMEOUT` | `45` | Seconds for one chat call, clamped to 5–90, and also cut to the time left in the 75s generator budget. A HTTP 400 retry uses that same budget. |
| `DIGEST_LLM_REASONING_EFFORT` | `low` | `low`, `medium`, `high`, `xhigh`, or `off`. |
| `DIGEST_CANDIDATES_PER_SECTION` | `16` | Newest feed items considered, clamped to 10–20. |
| `DIGEST_ITEMS_WITH_BOTS` | `1` | News items per section when bot updates are printed. |
| `DIGEST_ITEMS_WITHOUT_BOTS` | `2` | News items per section when they are not. |
| `DIGEST_SUMMARY_CHARS_WITH_BOTS` | `200` | Summary budget with a bot section. |
| `DIGEST_SUMMARY_CHARS_WITHOUT_BOTS` | `300` | Summary budget without one. |
| `DIGEST_HISTORY_PATH` | `$DIGEST_HOME/digest-history.json` | Recent-item file. |
| `DIGEST_HISTORY_DAYS` | `7` | How many days back a URL or title stays blocked. |
| `DIGEST_BOT_UPDATES_DIR` | `$DIGEST_HOME/bot-updates` | Directory of `YYYY-MM-DD.json` files. |
| `DIGEST_READER_IDS` | `reader-a,reader-b` | Ids allowed in a bot update's `for` field, besides `both`. |
| `DIGEST_BOT_UPDATES_MAX_AGE_HOURS` | `30` | Older bot files are ignored. |
| `DIGEST_BOT_UPDATES_FUTURE_SKEW_MINUTES` | `120` | How far ahead `generated_at` may be. |
| `DIGEST_BOT_UPDATES_MAX` | `4` | Maximum updates in one file (hard cap 6). |

## LLM setup

The generator sends one `POST` to `{DIGEST_LLM_BASE_URL}/chat/completions` using `urllib`. The body is the usual chat-completions shape: `model`, `messages`, `response_format: {"type": "json_object"}`, plus `temperature`, `max_tokens`, and `reasoning_effort` when the server accepts them. A HTTP 400 is retried once without those optional fields, because some compatible servers reject them.

The default model id is `grok-4.7` on `https://api.x.ai/v1`. That id is a setting, not a promise that the name will stay. Set `DIGEST_LLM_MODEL` to pin or to move.

The model sees each section's purpose, the taste note, and the candidates (title, url, time, and any summary that is not just the headline). It must return:

```json
{"picks":[{"section":"GLOBAL","items":[{"url":"https://example.com/story","summary":"One or two plain sentences."}]}]}
```

Validation is strict: the only keys are the ones above, every url must be one of the candidates, and each summary must be one or two plain sentences inside the character budget. Obituaries, ads, clickbait, and investing spam are removed before the call as well, and the prompt tells the model to reject them.

These failures all fall back to the deterministic picker, and the source file records a short reason:

- no API key, or a key that contains whitespace
- a base URL that is not `http` or `https`
- timeout or network error
- a non-200 response
- JSON that is not the schema above
- picks that do not match the candidates

The fallback keeps the newest remaining items. It prefers the film terms when the film feed has them, and it still fills the section from the rest of that feed so a quiet keyword list does not leave film blank. A summary that only repeats the headline plus the outlet is replaced with `From {outlet}, {date}.` Google News titles that end in ` - Outlet` use the outlet as the source line instead of `news.google.com`.

The API key is sent only as an `Authorization` header. It is not written into the digest, the history file, or the error strings. Stored URLs have any username or password removed. The taste note is sent to the model and is not copied into the source file. Picker failures are stored as a short code.

## Bot updates

See [docs/bot-updates.md](docs/bot-updates.md) for the file name, the JSON schema, the staleness rules, and the receipt layout. The short version: an external process writes `bot-updates/YYYY-MM-DD.json` before 05:00. A bad or stale file is ignored. A good file becomes the BOT UPDATES section, and each news section drops from two items to one so the notes fit under the length cap.

## Deploy

The trigger is a systemd user unit so it can see the user's CUPS queue.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/receipt-digest-trigger.service ~/.config/systemd/user/
# Edit ExecStart, EnvironmentFile, and ReadWritePaths if the clone is not ~/daily-digest.
systemctl --user daemon-reload
systemctl --user enable --now receipt-digest-trigger.service
loginctl enable-linger "$USER"
```

`loginctl enable-linger` keeps the user service running when nobody is logged in, which is what 05:00 needs. Check it with `systemctl --user status receipt-digest-trigger.service` and `curl -s http://127.0.0.1:8793/health`.

Home Assistant must use host networking so it can reach that loopback port. The automation is in `deploy/homeassistant.example.yaml`: a 05:00 time trigger calls `shell_command.print_digest_safe_submit`.

## Development

```bash
python3 -m unittest discover -v
```

GitHub Actions runs the same command on `ubuntu-latest` after installing `librsvg2-bin`, `fonts-dejavu-core`, and Pillow (`.github/workflows/test.yml`).

The tests cover the fallback picker, mocked chat-completions responses (including bad JSON, a timeout, and a dropped socket), the daytime weather choice (including a split day and the digest timezone), cross-day de-dupe, bot-file parsing (including a bad encoding, a symlink, and an oversized file), layout with and without the bot section, and `print_digest_safe.sh` with fake `lp`, `lpstat`, and a local HTTP server. They do not call the public network.

Regenerate the sample PNGs from the fixture payloads:

```bash
python3 -c 'from pathlib import Path; import test_receipt_digest as t; t.write_sample_pngs(Path("docs/samples"))'
```
