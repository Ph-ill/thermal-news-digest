# Bot updates

An external process, not this repository, can leave a JSON file for the morning receipt. The generator reads it while building the day's source file. When the file is valid and fresh, the receipt gains a **GROK BOT UPDATES** section. When the file is missing, empty, stale, or invalid, that section is left out and the news sections use the extra room.

Film, television, and music notes belong here. Those daily sections were removed on purpose. NEARBY SHOWS is a separate optional section for concerts from the artist file, not a place for these notes.

## Where the file goes

```text
$DIGEST_BOT_UPDATES_DIR/YYYY-MM-DD.json
```

`DIGEST_BOT_UPDATES_DIR` defaults to `bot-updates/` inside the project directory (`DIGEST_HOME`, or the repository directory when that is unset). The date in the file name is the digest date, which is the local date of the run. A file for another day is not read.

Write the file atomically (write a temporary file in the same directory, then rename it) so a run at 05:00 cannot see a half-written document. The file must be in place before the Home Assistant trigger fires.

The directory is gitignored. Do not commit the JSON. It is allowed to name people, because it never enters this public repository.

## Schema

The document is one JSON object with exactly these two keys.

| Field | Type | Rule |
| --- | --- | --- |
| `generated_at` | string | ISO-8601 timestamp **with a timezone**, for example `2026-11-26T04:40:00-06:00`. `Z` is accepted. A naive timestamp is rejected. |
| `updates` | array | Zero to `DIGEST_BOT_UPDATES_MAX` objects (default 4, hard cap 6). An empty array is valid and simply prints no section. |

Each update object uses these keys. `url` and `avatar` are optional. A file that omits `avatar` is still valid.

| Field | Type | Rule |
| --- | --- | --- |
| `bot` | string | 1–40 characters. The source line, and the first word is printed under the picture. |
| `topic` | string | 1–48 characters. |
| `text` | string | 8–360 characters, one line, written in first person by the bot. This is the receipt summary inside the speech bubble. |
| `for` | string | `both`, or one id from `DIGEST_READER_IDS`. |
| `url` | string | Optional. If present, it must be an `https://` URL with no spaces, at most 300 characters. |
| `avatar` | string | Optional slug, 1–40 characters: a lowercase letter or digit, then lowercase letters, digits, or hyphens. Names the picture file. When omitted, the slug is the bot name (`Popcorn` becomes `popcorn`). |

`bot`, `topic`, and `text` are plain text: no HTML, no newlines, no other control characters. Unknown keys at either level are rejected. The whole file is ignored when any update is invalid. The generator does not keep the valid ones and drop the rest. A bad or missing picture does not reject the file; the note still prints.

`DIGEST_READER_IDS` defaults to `reader-a,reader-b`. A private deployment sets it to its own ids (lowercase letters, digits, and hyphens). Those ids are how a note is marked for one reader. `both` means the note is for the shared receipt without a reader tag.

## Example

```json
{
  "generated_at": "2026-11-26T04:40:00-06:00",
  "updates": [
    {
      "bot": "Popcorn",
      "topic": "Film picks",
      "text": "I found a quiet anime about a night courier. It opens Friday, and I think the review is the one worth your time.",
      "for": "both",
      "avatar": "popcorn"
    },
    {
      "bot": "Listener",
      "topic": "Morning queue",
      "text": "I queued three new songs from the usual corner of the shelf. None of them are loud.",
      "for": "reader-a"
    }
  ]
}
```

With the default reader ids, the stored title of the second note is `Listener: Morning queue (reader-a)`. A `both` note is stored as `Popcorn: Film picks`. On the receipt the reader tag is left off. The topic is the small heading, and the first word of the bot name is printed under the picture. The source line stays the full bot name. `text` is what the bot would say out loud, so write it in first person.

## Pictures

Each note is a square picture on the left, 144 dots (18 mm), with the bot's first name centered underneath in a pixel font. `DIGEST_BOT_UPDATES_STYLE` chooses the words beside it. `bubble` (the default) draws a rounded comic balloon with an arched top and bottom and a curved, tapered tail pointing at the picture. The topic and the first-person text sit inside, with padding past the stroke and a gap between notes. `plain` skips the balloon. The same words sit beside the picture, and a short dashed line, centered on the page, sits halfway between one note and the next. Any other value of the setting is treated as `bubble`.

Pictures live in `DIGEST_AVATARS_DIR` (default `avatars/` inside the project directory). That directory is gitignored. The file name is the slug plus `.png`, `.jpg`, `.jpeg`, or `.webp`, for example `avatars/popcorn.png`. The loader tries those extensions in that order and uses the first one that decodes.

A picture has to be a regular file. Symlinks are ignored. The file must be at most 512 KiB, and Pillow must decode it as at most one megapixel. Anything else — missing, truncated, too big, or not an image — is skipped. The note then uses a drawn circle with the bot's initial.

Before printing, the picture is cropped to a square, contrast-boosted, and dithered to 1-bit with Floyd-Steinberg so the thermal head can hold a face. Use a square PNG with a transparent background and about 12% padding on each side, so the face is not cut by the crop. A picture with any transparency gets a thin black ring around its silhouette. That keeps a bright face, such as a white render, from vanishing into the paper. A fully opaque file is left unchanged. `DIGEST_AVATAR_THRESHOLD` (0–255) is optional. Set it to slide the dither midpoint; a higher value prints more black. Leave it unset for the default midpoint after the contrast boost.

The section heading is `GROK BOT UPDATES` unless `DIGEST_SECTION_TITLE_BOT_UPDATES` sets another title. `DIGEST_BOT_UPDATES_DIR` and the `YYYY-MM-DD.json` file name are unchanged. `avatar` is optional, so an older file that does not have it still loads.

## Freshness

`generated_at` is compared with the generator clock, not with the file's modification time.

- Older than `DIGEST_BOT_UPDATES_MAX_AGE_HOURS` (default 30) is stale.
- More than `DIGEST_BOT_UPDATES_FUTURE_SKEW_MINUTES` ahead (default 120) is rejected.
- The file must be UTF-8 JSON and at most 64 KiB.
- The loader opens the date file with `O_NOFOLLOW` and reads at most 64 KiB + 1 from that descriptor. A symlink, a non-UTF-8 file, a nested document that will not parse, or anything over the cap is ignored. The size is enforced on the read, not on a separate `stat`.

## What the receipt does

| File | Result |
| --- | --- |
| Missing, or `updates` is `[]` | No bot section. No error. |
| Valid and fresh | `GROK BOT UPDATES` is printed in its place in the section order (last, unless `DIGEST_SECTION_ORDER` moves it). |
| Stale, invalid, unreadable, or too large | No bot section. The source file records a short error that does not quote the file body. |

News sections print `DIGEST_ITEMS_WITH_BOTS` items when the bot section is present (default 1) and `DIGEST_ITEMS_WITHOUT_BOTS` items when it is not (default 2). Summary length follows the same split, so a morning with no bot file gives the other sections the space. The receipt height cap still applies.
