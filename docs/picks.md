# External picks

The morning job does not have to call a model. An external bot can pick the stories, and can name nearby shows when there is no Ticketmaster key. The generator only fetches and checks. It does not spend an API credit on this path.

Do this about 15 minutes before the 05:00 print, so the file is in place when Home Assistant fires.

## Export

```bash
python3 digest_generator.py --export-candidates --date YYYY-MM-DD
```

That writes `$DIGEST_CANDIDATES_DIR/YYYY-MM-DD.json`. The directory defaults to `output/` in the project. An explicit path still works:

```bash
python3 digest_generator.py --export-candidates output/candidates-YYYY-MM-DD.json --date YYYY-MM-DD
```

`--date` is optional. When it is omitted, the date is today in `DIGEST_TZ`. The command fetches the news feeds, writes one JSON file, and exits. It does not call a model, and it does not update `digest-history.json`. The 05:00 run reads the default path, so leave the file there.

BASTROP candidates use `DIGEST_LOCAL_FRESH_HOURS` (default 168, clamped to 6–168) as a hard cutoff. A story older than that window is left out even when the feed has nothing newer. When a picks file leaves BASTROP empty and nothing in the window qualifies, the section prints `No new Bastrop picks today.` Every other section uses `DIGEST_FRESH_HOURS` (default 36, same cap) and still keeps older rows when nothing inside that window is present. A story already in `digest-history.json` is left out of both.

The file is what the model would have been shown:

| Field | Meaning |
| --- | --- |
| `schema_version` | `"1"` |
| `digest_date` | The edition date, `YYYY-MM-DD` |
| `taste` | The taste note |
| `interests` | The interests profile, as a short brief |
| `recent_titles` | Up to 40 titles already printed on earlier days |
| `items_per_section` | How many items a section is budgeted. The export does not know yet whether NEARBY SHOWS will print, so this can be 2 even when the 05:00 run later prints 1. |
| `summary_max_chars` | The crowded summary budget, 200 unless `DIGEST_SUMMARY_CHARS_WITH_BOTS` changes it. Write to this limit. Shows or bot notes can shorten the receipt after the export. |
| `sections` | One object per news section |

Each section has `section` (the printed title, such as `GLOBAL` or `BASTROP`), `purpose` (the rubric the bot reads), and `candidates`. At most 12 candidates are listed per section. Obituaries, ads, and stories already printed are not listed.

`title` and `summary` are the clipped text the bot reads. The same row also stores `full_title`, `full_summary`, `feed_url`, and, when they exist, `source_label` and `topic`. The 05:00 print uses the full fields. The section title and the rubric printed or sent to a model come from settings, not from this file.

```json
{
  "schema_version": "1",
  "digest_date": "2026-11-26",
  "taste": "cerebral sci-fi, space, horror, and new local food and drink openings",
  "interests": "all sections likes: space",
  "recent_titles": ["Harbor ferry delayed after the storm"],
  "items_per_section": 2,
  "summary_max_chars": 200,
  "sections": [
    {
      "section": "GLOBAL",
      "purpose": "World news that will still matter by tonight.",
      "candidates": [
        {
          "url": "https://example.com/harbor",
          "title": "Harbor talks end with a written ceasefire",
          "summary": "Mediators said both sides signed the text before dawn.",
          "full_title": "Harbor talks end with a written ceasefire",
          "full_summary": "Mediators said both sides signed the text before dawn and set Friday for the first aid ship.",
          "feed_url": "https://news.google.com/rss/search?q=example",
          "published_at": "Thu, 26 Nov 2026 08:00:00 GMT"
        }
      ]
    }
  ]
}
```

## Picks file

```text
$DIGEST_PICKS_DIR/YYYY-MM-DD.json
```

`DIGEST_PICKS_DIR` defaults to `picks/` in the project directory. The directory is gitignored. The date in the name is the digest date. Write the file atomically (temporary file in the same directory, then rename) so 05:00 cannot read a half-written document.

`generated_at` is required. It must be an ISO-8601 timestamp with a timezone, and its calendar date in `DIGEST_TZ` must be the digest date. A timestamp from another local day is stale and the whole file is ignored. `picks` uses the same objects the model returns: `section` is the section title from the export, and each item has only `url` and `summary`. `shows` is optional.

A picked URL must be one of today's candidates. The comparison uses the raw string and `normalize_url`, so a `utm_*` parameter, a `www` host, or a trailing slash still matches. The same checks the model path uses then apply: hard excludes and de-dupe against history. A summary longer than the print budget is clipped at a sentence boundary, or at a word boundary when the sentence does not fit. It is not dropped for length alone. An unoffered URL is skipped. The other picks in the file are kept.

A Louisiana listing is dropped in the feed gate, before export, the model, or the fallback can see it. The check looks at the raw title, the display title, the summary, the outlet label, and the URL host. That covers `Bastrop, LA`, `Bastrop (LA)`, `Morehouse` with or without `Parish`, the outlet `Bastrop Daily Enterprise` (Google News moves that name into the source label), and `bastropenterprise.com`.

A section whose picks are all rejected is sent to the model when `DIGEST_LLM_API_KEY` is set. Otherwise that section uses the deterministic fallback. BASTROP's fallback is the newest candidate that names a home town (Bastrop, Smithville, Elgin, Cedar Creek, McDade, Paige, Red Rock, Del Valle, or east Austin), is not already in the digest history, and is not a Louisiana listing. "From Bastrop" or "the owner is from Bastrop" does not place the event; "in", "at", and "opens in" do. When none qualify, BASTROP prints `No new Bastrop picks today.` The line `Local picks unavailable today (AI offline).` is used only when there is no picks file and the model is not called. The section does not stop on the empty placeholder while one of those can still choose a story. Sections the file did fill are not sent to the model.

At 05:00 the generator reuses `$DIGEST_CANDIDATES_DIR/YYYY-MM-DD.json` when it is a regular file, UTF-8, at most 256 KiB, not a symlink, its `digest_date` is the edition date, and its modification time is less than 3 hours old. News feeds are not fetched again, so a Google News token or the 16-item cap cannot drop a URL the bot was shown. Weather is still fetched. Ticketmaster is still fetched when a key is set. A missing file is quiet and the feeds are fetched. A stale, symlinked, or invalid export is ignored and the feeds are fetched.

The export is untrusted. Every reused row goes through the same gates as a story just fetched: the configured feed's required phrases and `require_place`, `keep_local`, `keep_company`, the freshness cut, and the obituary, spam, and history checks. `feed_url` has to be one of that section's configured feeds, and the topic stamped on a company row comes from that feed, so the fallback still alternates SpaceX and Earth Funeral. A row that fails is dropped. A section name that is not one of today's sections is ignored. The file's `purpose` is not used.

A missing picks file is quiet. A symlink, a non-UTF-8 file, a file over 64 KiB, bad JSON, or a stale `generated_at` is ignored. The generator then calls the model if `DIGEST_LLM_API_KEY` is set, and otherwise uses the deterministic fallback. `picker.source` in the source file is `picks`, `llm`, `mixed`, or `fallback`. `picker.mode` is `picks` when the file supplied every printed news item.

```json
{
  "generated_at": "2026-11-26T04:40:00-06:00",
  "picks": [
    {
      "section": "GLOBAL",
      "items": [
        {
          "url": "https://example.com/harbor",
          "summary": "Mediators finished a written ceasefire before dawn and named Friday for the first aid ship."
        }
      ]
    },
    {
      "section": "BASTROP",
      "items": [
        {
          "url": "https://example.com/brewery",
          "summary": "The new taproom pours on Friday and keeps a small kitchen open until nine."
        }
      ]
    }
  ],
  "shows": [
    {
      "artist": "Sample Band",
      "venue": "Mohawk",
      "city": "Austin",
      "date": "2026-11-30",
      "url": "https://venue.example/e/1"
    }
  ]
}
```

## Check

After the picks file is in place, run the same build the 05:00 job will run. This writes nothing: no `digest-history.json`, no `output/digest-source-*`, no `state.json`, and no print.

```bash
python3 digest_generator.py --check --date YYYY-MM-DD
```

`--date` is optional. When it is omitted, the date is today in `DIGEST_TZ`. The command reads the same picks file, bot-updates file, digest history, and freshness windows as the morning run.

Stdout is one JSON object:

| Field | Meaning |
| --- | --- |
| `picker.mode`, `picker.reason` | What the morning run would record |
| `sections` | Each section's item count and titles, plus `fallback` and `offline` |
| `fallback_used` | A section used a fallback line or a newest-item fallback |
| `offline_used` | A section used `Local picks unavailable today (AI offline).` |
| `bot_updates` | How many bot notes would print |
| `picks_ignored` | The picks file was present and rejected |

Exit 0 means the picks file was accepted, no section would print the offline line, the edition is not the emergency payload, and at least one news or weather source was reached. Exit 1 means the offline line would print, the picks file was ignored (stale, unreadable, or invalid), the payload is an emergency or `generator_error` edition, or every news and weather source is unavailable. A missing picks file is not "ignored"; with no model it exits 1 because BASTROP would print the offline line.

If today's source file already froze a bad BASTROP line, regenerate it from the current picks and print:

```bash
./print_digest_safe.sh --rebuild
```

That moves `output/digest-source-YYYY-MM-DD.json` to `output/digest-source-YYYY-MM-DD.vN.json` and prints the new file. If generation fails after that move, the archived file is moved back to `output/digest-source-YYYY-MM-DD.json`, so the canonical file is never missing. `POST /v1/digest/rebuild` does the same through the trigger.

## Shows

`shows` is used only when `DIGEST_TICKETMASTER_KEY` is empty. Each object needs `artist`, `venue`, `city`, and `date` (`YYYY-MM-DD`). `url` is optional. A show with no URL, a past date, or a date past the usual horizon is left out. A show is printed once when it is first seen, and once more inside seven days, the same rule Ticketmaster uses. A username or password in the URL is removed. A URL that contains a control character, or any character the receipt cannot carry in XML, is left out. The other shows in the file are kept. When a Ticketmaster key is set, Ticketmaster wins and this array is not read.
