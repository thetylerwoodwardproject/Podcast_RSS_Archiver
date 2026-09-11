# Podcast RSS archiver

Python 3.9 or newer. No pip packages required.

## Run

```bash
python3 podcast_archive.py 'https://YOUR-PODCAST-RSS-FEED' --output ./podcast-backup
```

Replace the placeholder with your RSS URL. The XML snapshot is captured when you run the script. Each run creates a new UTC timestamped folder inside the output directory and downloads a complete new snapshot. Existing backups are preserved. Interrupted runs retain completed downloads; rerunning starts a fresh archive, not a resume.

## Files

| Location | Example |
| --- | --- |
| Original RSS response body, unchanged | `feed.xml` |
| Additional feed pages, if linked | `feed-page002.xml` |
| Episode media | `audio/ep001.mp3` |
| Episode artwork or show artwork fallback | `images/ep001.jpg` |
| Linked transcripts | `transcripts/ep001.vtt` |
| Validated or converted chapter JSON | `chapters/ep001.json` |
| Images referenced by chapter JSON | `chapters/images/ep001-chapter001.jpg` |
| Bonus media and matching sidecars | `audio/bonus001.mp3`, `images/bonus001.jpg`, etc. |
| Trailer media and matching sidecars | `audio/trailer001.mp3`, etc. |
| Titles, original numbers, URLs, checksums, missing assets and errors | `manifest.json` |

Chapter files are always saved as `.json`. The script parses the content rather than relying on the URL extension or declared MIME type. Existing JSON keeps all publisher fields, including `version`, `startTime`, `endTime`, `title`, `img`, and `url`, and is formatted for readability. Invalid chapter data is reported as a failure instead of being saved with a misleading `.json` extension.

When linked chapter JSON is available, it takes priority over embedded XML. Otherwise, embedded Podlove XML chapters are converted to JSON: timestamps become seconds, `image` becomes `img`, and `href` becomes `url`. End times are not invented when XML does not provide them. The original XML feed stays unchanged.

Each chapter's `img` URL is downloaded into `chapters/images`, with names such as `ep001-chapter001.jpg` and `bonus001-chapter002.png`. The chapter number is its position in that JSON array, starting at 1. Chapters without an image create no image file. Original URLs remain in the JSON. Relative image URLs in feed downloads are resolved against the chapter download URL, or against the feed URL for embedded chapters. Failed chapter image downloads are recorded in the manifest and return a nonzero exit status.

Other assets retain extensions based on declared MIME type or source URL; an unidentifiable extension is saved as `.bin`. Chapter image downloads also check response MIME type when the URL lacks a recognizable image extension. The script does not transcode audio or convert transcripts. Multiple assets with the same extension get suffixes such as `ep001-02.txt` or `ep001-02.json`.

## Fix chapters from an older download

Point this command at an existing backup's **chapters directory**:

```bash
python3 podcast_archive.py --convert-chapters './podcast-backup/YOUR-TIMESTAMP/chapters'
```

This reads the `.bin`, `.xml`, and `.json` files directly in that directory and creates validated JSON copies in `chapters/converted-json`. It also downloads their chapter images into `chapters/images`. It does not download audio or fetch the feed. Original files and the original archive manifest remain unchanged. An image download report is saved in `chapters/converted-json/image-download-report.json`.

Legacy names such as `ep001.embedded01.xml` become `ep001.json`. Different content using the same name receives a suffix such as `ep001-02.json`; existing identical JSON copies are reused. Running conversion again may download chapter images again. Conversion requires absolute image URLs because standalone files do not retain their original web location; unresolved relative URLs are reported as image failures.

## Numbering

- Regular episodes use `itunes:episode`, falling back to `podcast:episode`. Episode 42 becomes `ep042`, even if it is the oldest episode available.
- Bonus episodes are identified by `itunes:episodeType=bonus` and numbered separately, oldest first: `bonus001`, `bonus002`, etc. They do not reuse the main episode number.
- Trailers get their own `trailer001` sequence.
- Regular episodes missing a positive integer number receive the lowest unused positive number, in publication order. A warning identifies every fallback. Fractional episode numbers also use this fallback.
- Missing or invalid publication dates sort after valid dates. Ties and missing dates retain feed order, which may not be chronological. Warnings flag missing dates.
- Duplicate regular episode numbers stop the run before media downloads. This can happen when numbering restarts each season. Use `--sequential` to create a single chronological sequence across seasons.
- Numbering uses at least three digits, so episode 1000 becomes `ep1000`.

Force chronological numbering for all regular episodes:

```bash
python3 podcast_archive.py 'https://YOUR-PODCAST-RSS-FEED' --sequential
```

If bonus episodes are labeled only in their titles, optionally add a case-insensitive matching expression:

```bash
python3 podcast_archive.py 'https://YOUR-PODCAST-RSS-FEED' --bonus-regex '^bonus\b'
```

Review the manifest before relying on fallback numbering. The script does not guess episode numbers from titles.

## Scope and reliability

The script downloads every RSS enclosure, episode artwork from supported image tags (iTunes, Podcasting 2.0 and Media RSS thumbnails), and linked Podcasting 2.0 transcripts and chapters. If an episode has no artwork, it uses channel artwork when available. It follows channel-level Atom `rel="next"` pagination and deduplicates episodes by GUID, falling back to enclosure URL.

It can only archive episodes and assets exposed by the supplied feed and its linked pages. A host may limit a feed to recent episodes. Increase the host's feed episode limit before running if necessary. It cannot retrieve deleted episodes, subscriber content absent from the feed, or files available only in your hosting dashboard. It does not scrape show-note HTML, follow arbitrary archive links, or extract chapters/transcripts embedded inside audio files. All original show notes and metadata present in the feed remain in the XML snapshot.

Use the canonical RSS URL, not an Apple Podcasts or Spotify show page. Direct tokenized/private feed URLs work when they are accessible without extra login headers. Feed and manifest files retain source URLs, including any private feed tokens. Keep those archives private.

Downloads stream to disk, retry three times, check HTTP Content-Length when provided, and use `.part` files until complete. SHA-256 checksums are recorded for saved files, with separate source-download metadata for reformatted or converted chapter files; they describe the downloaded bytes, not verification against publisher-provided hashes. Failed assets are logged and processing continues. Missing optional sidecars are listed per episode and are not errors. Missing enclosures are errors.

Exit status: `0` for completed downloads, `1` for a fatal error or failed required/downloaded asset, `130` for interruption. Check `manifest.json` for details. Network exceptions can contain source URLs.

```bash
python3 podcast_archive.py 'https://YOUR-PODCAST-RSS-FEED' --timeout 120 --attempts 5 --max-pages 200
```

Validated using a local HTTP fixture covering raw XML preservation, pagination, episode and bonus naming, multiple transcripts, artwork fallback, chapter files, failed download cleanup, duplicate-number handling, chapter JSON validation, XML-to-JSON conversion, chapter image downloads, and conversion of existing chapter files. Not tested against your feed because no feed URL was supplied.

## Format references

- [Apple: episode types](https://podcasters.apple.com/support/825-how-to-create-an-episode)
- [Apple: episode, season, and episodeType metadata](https://podcasters.apple.com/support/899-set-up-your-show-for-a-subscription)
- [Podlove Simple Chapters XML specification](https://podlove.org/simple-chapters/)
- [Podcasting 2.0 namespace specification](https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/1.0.md)

A free, open-source script from The Tyler Woodward Project, [tylerwoodward.me](https://tylerwoodward.me)
