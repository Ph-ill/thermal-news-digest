# Nearby shows

NEARBY SHOWS lists upcoming concerts by artists in a private list, within about an hour's drive of Bastrop, Texas. The default search is a 50 mile radius around the public city center, which covers Austin. A venue whose coordinates fall outside that radius is dropped. A venue with no coordinates is kept only when its city is inside that radius, including Bastrop, Austin, Elgin, Smithville, Cedar Creek, Lockhart, San Marcos, Round Rock, and the smaller towns Ticketmaster may name (McDade, Paige, Red Rock, Rockne, Dale, Taylor, Hutto, Del Valle, Luling, Seguin, and the rest of that list). Houston and Dallas are dropped. When a venue has coordinates, those decide, and the city name does not override them. It is omitted when there is nothing to print. The other sections then keep their usual space, the same way they do when GROK BOT UPDATES is absent.

## Artist file

`DIGEST_ARTISTS_FILE` points at a text file, one artist per line. Blank lines and `#` comments are ignored. The generator reads at most eight names so the morning job stays inside its 75 second budget. Copy `docs/artists.example.txt` and keep the real list out of git. The example names are placeholders.

## Ticketmaster

The lookup is the Ticketmaster Discovery API:

`GET https://app.ticketmaster.com/discovery/v2/events.json`

Set `DIGEST_TICKETMASTER_KEY` to a free Discovery API key. The request sends `classificationName=music`, `radius` in miles, `keyword` for one artist, a date window, and both `latlong` and `geoPoint`. Ticketmaster documents `latlong` as deprecated and asks for a geohash in `geoPoint`. The client sends a precision-7 geohash and still sends `latlong`, because radius filtering is documented for both and a removal of `latlong` should not blank the section.

The key is sent only as the `apikey` query parameter. It is not written into the digest, the history file, or error strings. A failure cites `https://app.ticketmaster.com/discovery/v2/events.json` with no query. The printed link is the public event URL from the response, with any username or password removed.

Each request uses the same fetch deadline as the news feeds. If the 75 second budget is already gone, the remaining artists are skipped and the section is left out when nothing was found.

## Why not Bandsintown or SeatGeek

Bandsintown's public artist-events feed is not a radius search. It returns an artist's dates worldwide, so Austin would have to be filtered after the fact, and tribute listings are common. It also wants an app id. SeatGeek's event search can filter by location, but it wants a client id and mostly overlaps Ticketmaster for ticketed concerts. A second live source would spend more of the 75 second budget for the same shows. Ticketmaster is the only live source.

## Matching and repeats

Names are compared after lowercasing, stripping punctuation, and dropping a leading "The", "A", or "An". "The Sample Band" matches "Sample Band" and does not match "Prince Royce" or "Sample Band Tribute". An event whose name says tribute, cover band, or "the music of" is dropped unless the wanted name itself contains that word.

A show is printed once when it is first seen, and once more when it is within seven days. History stores the Ticketmaster id and which of those two mentions already ran. The same morning can be rebuilt after a crash. Past dates are skipped.

No key, no artist file, or no matching show: the section is not printed and no placeholder is invented.

When `DIGEST_TICKETMASTER_KEY` is empty, the same section can be filled from the optional `shows` array in `picks/YYYY-MM-DD.json`. Each entry names an artist, a venue, a city, a date, and an optional event URL. The date window and the once-plus-seven-days repeat rule are the same as Ticketmaster. Usernames and passwords are removed from the URL. A show with no URL is left out, because the receipt needs a link. If a Ticketmaster key is set, Ticketmaster is the only source and the array is ignored. The file contract is [picks.md](picks.md).
