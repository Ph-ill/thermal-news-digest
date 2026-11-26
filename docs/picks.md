# External picks

The morning job does not have to call a model. An external bot can pick the stories, and can name nearby shows when there is no Ticketmaster key. The generator only fetches and checks. It does not spend an API credit on this path.

Do this about 15 minutes before the 05:00 print, so the file is in place when Home Assistant fires.

## Export

```bash
python3 digest_generator.py --export-candidates output/candidates-YYYY-MM-DD.json --date YYYY-MM-DD
```

`--date` is optional. When it is omitted, the date is today in `DIGEST_TZ`. The command fetches the news feeds, writes one JSON file, and exits. It does not call a model, and it does not update `digest-history.json`.

The file is what the model would have been shown:

| Field | Meaning |
| --- | --- |
| `schema_version` | `"1"` |
| `digest_date` | The edition date, `YYYY-MM-DD` |
| `taste` | The taste note |
| `interests` | The interests profile, as a short brief |
| `recent_titles` | Up to 40 titles already printed on earlier days |
| `items_per_section` | How many items a section is budgeted. The export does not know yet whether NEARBY SHOWS will print, so this can be 2 even when the 05:00 run later prints 1. |
| `summary_max_chars` | The summary budget |
| `sections` | One object per news section |

Each section has `section` (the printed title, such as `GLOBAL` or `BASTROP`), `purpose` (the rubric), and `candidates`. Each candidate has only `url`, `title`, `summary`, and `published_at`. At most 12 candidates are listed per section. Obituaries, ads, and stories already printed are not listed.

```json
{
  "schema_version": "1",
  "digest_date": "2026-11-26",
  "taste": "cerebral sci-fi, space, horror, and new local food and drink openings",
  "interests": "all sections likes: space",
  "recent_titles": ["Harbor ferry delayed after the storm"],
  "items_per_section": 2,
  "summary_max_chars": 300,
  "sections": [
    {
      "section": "GLOBAL",
      "purpose": "World news that will still matter by tonight.",
      "candidates": [
        {
          "url": "https://example.com/harbor",
          "title": "Harbor talks end with a written ceasefire",
          "summary": "Mediators said both sides signed the text before dawn.",
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

A picked URL must be one of today's candidates. The same checks the model path uses then apply: hard excludes, a summary that fits the budget, and de-dupe against history. An unoffered URL is skipped. The other picks in the file are kept. A valid file is used instead of calling the model, even when `DIGEST_LLM_API_KEY` is set.

A missing file is quiet. A symlink, a non-UTF-8 file, a file over 64 KiB, bad JSON, or a stale `generated_at` is ignored. The generator then calls the model if `DIGEST_LLM_API_KEY` is set, and otherwise uses the deterministic fallback. `picker.source` in the source file is `picks`, `llm`, or `fallback`. `picker.mode` is `picks` when the file supplied the printed items.

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

## Shows

`shows` is used only when `DIGEST_TICKETMASTER_KEY` is empty. Each object needs `artist`, `venue`, `city`, and `date` (`YYYY-MM-DD`). `url` is optional. A show with no URL, a past date, or a date past the usual horizon is left out. A show is printed once when it is first seen, and once more inside seven days, the same rule Ticketmaster uses. A username or password in the URL is removed. When a Ticketmaster key is set, Ticketmaster wins and this array is not read.
