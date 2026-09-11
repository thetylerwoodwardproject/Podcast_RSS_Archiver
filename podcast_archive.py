#!/usr/bin/env python3
"""Archive RSS podcast media and sidecars. Python 3.9+, standard library only.
Run: python3 podcast_archive.py 'https://example.com/feed.xml'
See README.md for numbering, limitations, and recovery instructions.
"""
import argparse
import datetime as dt
import email.utils
import hashlib
import json
import mimetypes
import math
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

IT = 'http://www.itunes.com/dtds/podcast-1.0.dtd'
POD = ('https://podcastindex.org/namespace/1.0',
       'https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/1.0.md')
ATOM = 'http://www.w3.org/2005/Atom'
PSC = 'http://podlove.org/simple-chapters'
MEDIA = 'http://search.yahoo.com/mrss/'
MIME = {'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/x-m4a': '.m4a',
        'audio/ogg': '.ogg', 'audio/aac': '.aac', 'image/jpeg': '.jpg',
        'image/png': '.png', 'image/webp': '.webp', 'text/vtt': '.vtt',
        'application/x-subrip': '.srt', 'application/srt': '.srt',
        'application/json': '.json', 'application/chapters+json': '.json',
        'text/plain': '.txt', 'text/html': '.html', 'application/pdf': '.pdf'}


def field(el, tag):
    return (el.findtext(tag) or '').strip()


def podnodes(el, name):
    return [node for ns in POD for node in el.findall(f'{{{ns}}}{name}')]


def open_url(url, timeout):
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('Only HTTP and HTTPS URLs are supported')
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        'User-Agent': 'PodcastArchive/1.0', 'Accept-Encoding': 'identity'}), timeout=timeout)


def transfer(url, target, timeout, attempts):
    """Stream to a temporary file; never leave a partial final file."""
    temp = target.with_name(target.name + '.part')
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(attempts):
        try:
            with open_url(url, timeout) as response, temp.open('wb') as out:
                count, digest = 0, hashlib.sha256()
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    count += len(chunk)
                length = response.headers.get('Content-Length')
                if length and count != int(length):
                    raise IOError('Incomplete response body')
                if not count:
                    raise IOError('Empty response body')
                meta = {'bytes': count, 'sha256': digest.hexdigest(),
                        'content_type': response.headers.get_content_type(),
                        'resolved_url': response.geturl()}
            temp.replace(target)
            return meta
        except Exception:
            temp.unlink(missing_ok=True)
            if attempt + 1 == attempts:
                raise
            time.sleep(min(2 ** attempt, 8))


def extension(url, mime, fallback='.bin'):
    mime = (mime or '').split(';')[0].lower().strip()
    if mime in MIME:
        return MIME[mime]
    suffix = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).suffix.lower()
    if re.fullmatch(r'\.[a-z0-9]{1,8}', suffix):
        return suffix
    return mimetypes.guess_extension(mime) or fallback


def chapter_seconds(value):
    """Convert Podlove Normal Play Time to numeric seconds."""
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?::\d+){0,2}(?:\.\d+)?', value):
        raise ValueError(f'Invalid chapter time: {value!r}')
    parts = value.split(':')
    if len(parts) > 1 and any(float(v) >= 60 for v in parts[1:]):
        raise ValueError(f'Invalid chapter time: {value!r}')
    result = 0.0
    for part in parts:
        result = result * 60 + float(part)
    if not math.isfinite(result):
        raise ValueError('Chapter time must be finite')
    return int(result) if result.is_integer() else result


def chapter_json(raw):
    """Validate chapter JSON, or convert a Podlove chapters XML document."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        if re.search(br'<!\s*(DOCTYPE|ENTITY)\b', raw.replace(b'\x00', b''), re.I):
            raise ValueError('DTD/entity declarations are not supported')
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError('Chapters are neither valid JSON nor Podlove XML') from exc
        if root.tag not in (f'{{{PSC}}}chapters', 'chapters'):
            raise ValueError('Expected a Podlove chapters XML root')
        chapters = []
        for node in root:
            if node.tag not in (f'{{{PSC}}}chapter', 'chapter'):
                raise ValueError('Unsupported element in chapter XML')
            if node.get('title') is None:
                raise ValueError('XML chapter is missing its title')
            chapter = {'startTime': chapter_seconds(node.get('start')),
                       'title': node.get('title')}
            for source, target in [('href', 'url'), ('image', 'img')]:
                if node.get(source):
                    chapter[target] = node.get(source)
            chapters.append(chapter)
        chapters.sort(key=lambda chapter: chapter['startTime'])
        data = {'version': '1.2.0', 'chapters': chapters}
    if not isinstance(data, dict) or not isinstance(data.get('chapters'), list):
        raise ValueError('Chapter JSON must be an object containing a chapters array')
    for chapter in data['chapters']:
        if not isinstance(chapter, dict):
            raise ValueError('Each chapter must be an object')
        start = chapter.get('startTime')
        if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0:
            raise ValueError('Each chapter needs a finite, nonnegative numeric startTime')
    # Keep all publisher JSON fields, including endTime, img, url and version.
    # XML has no end times, so conversion does not invent them.
    return (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def save_chapters(raw, target):
    output = chapter_json(raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.part')
    try:
        temp.write_bytes(output)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return {'bytes': len(output), 'sha256': hashlib.sha256(output).hexdigest(),
            'content_type': 'application/json'}


def download_chapters(url, target, timeout, attempts):
    staged = target.with_name(target.name + '.download')
    try:
        source = transfer(url, staged, timeout, attempts)
        return {**save_chapters(staged.read_bytes(), target), 'source_download': source}
    finally:
        staged.unlink(missing_ok=True)


def download_chapter_images(chapter_path, images_dir, base_url, timeout, attempts):
    """Download each chapter's img URL; preserve the JSON's original URLs."""
    data = json.loads(chapter_path.read_bytes())
    results = []
    for index, chapter in enumerate(data['chapters'], 1):
        image_url = chapter.get('img')
        if not image_url:
            continue
        asset = {'kind': 'chapter_image', 'chapter_index': index,
                 'chapter_file': chapter_path.name, 'url': image_url}
        results.append(asset)
        try:
            if not isinstance(image_url, str):
                raise ValueError('Chapter img must be a URL string')
            image_url = urllib.parse.urljoin(base_url, image_url)
            asset['url'] = image_url
            # Use response MIME when the URL has no recognizable image extension.
            ext = extension(image_url, '')
            if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif', '.svg'):
                ext = '.bin'
            target = images_dir / f'{chapter_path.stem}-chapter{index:03d}{ext}'
            meta = transfer(image_url, target, timeout, attempts)
            if ext == '.bin':
                real_ext = extension('', meta['content_type'])
                if real_ext != '.bin':
                    renamed = target.with_suffix(real_ext)
                    target.replace(renamed)
                    target = renamed
            asset.update(meta, file=str(target), status='downloaded')
        except Exception as exc:
            asset.update(status='failed', error=str(exc))
            print(f'  FAILED: chapter image {index}: {exc}', file=sys.stderr)
    return results


def convert_existing_chapters(directory, timeout=60, attempts=3):
    """Create JSON copies of old .bin/.xml chapter files without downloading audio."""
    folder = Path(directory)
    if not folder.is_dir():
        raise ValueError('Pass the existing chapters directory to --convert-chapters')
    sources = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.xml', '.bin', '.json'))
    if not sources:
        raise ValueError('No .xml, .bin or .json files found in the specified chapters directory')
    output = folder / 'converted-json'
    failures = 0
    image_failures = 0
    image_records = []
    for source in sources:
        # Legacy ep001.embedded01.xml becomes ep001.json if available.
        stem = re.sub(r'\.embedded\d+$', '', source.stem)
        try:
            content = chapter_json(source.read_bytes())
            target = output / (stem + '.json')
            serial = 2
            while target.exists() and target.read_bytes() != content:
                target = output / f'{stem}-{serial:02d}.json'
                serial += 1
            if not target.exists():
                save_chapters(content, target)
            print(f'{source.name} -> {target}', flush=True)
            image_assets = download_chapter_images(target, folder / 'images', '', timeout, attempts)
            image_records.extend(image_assets)
            image_failures += sum(a['status'] == 'failed' for a in image_assets)
        except Exception as exc:
            failures += 1
            print(f'FAILED: {source.name}: {exc}', file=sys.stderr)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'image-download-report.json').write_text(
        json.dumps(image_records, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'Converted {len(sources) - failures} files; {failures} conversion failures; '
          f'{image_failures} image failures. Originals retained.')
    return 1 if failures or image_failures else 0


def images(el):
    found = []
    for node in el.findall(f'{{{IT}}}image') + podnodes(el, 'image'):
        if node.get('href'):
            found.append((node.get('href'), ''))
    for node in el.findall(f'.//{{{MEDIA}}}thumbnail'):
        if node.get('url'):
            found.append((node.get('url'), ''))
    for node in el.findall('image'):
        url = field(node, 'url')
        if url:
            found.append((url, ''))
    return list(dict.fromkeys(found))


def assign_names(entries, sequential, bonus_pattern):
    def date_key(entry):
        try:
            value = email.utils.parsedate_to_datetime(field(entry['xml'], 'pubDate'))
            return value.replace(tzinfo=value.tzinfo or dt.timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            return float('inf')
    # Missing/tied dates retain feed order. Flag this instead of claiming chronology.
    entries.sort(key=date_key)
    groups = {'ep': [], 'bonus': [], 'trailer': []}
    for entry in entries:
        item = entry['xml']
        kind = field(item, f'{{{IT}}}episodeType').lower()
        if kind == 'bonus' or (bonus_pattern and bonus_pattern.search(field(item, 'title'))):
            prefix = 'bonus'
        else:
            prefix = 'trailer' if kind == 'trailer' else 'ep'
        entry['prefix'] = prefix
        if date_key(entry) == float('inf'):
            print('WARNING: Missing/invalid date: ' + field(item, 'title'), file=sys.stderr)
        groups[prefix].append(entry)
    for prefix, group in groups.items():
        reserved = set()
        for entry in group:
            raw = field(entry['xml'], f'{{{IT}}}episode')
            if not raw:
                raw = next((n.text.strip() for n in podnodes(entry['xml'], 'episode')
                            if n.text and n.text.strip()), '')
            number = int(raw) if raw.isdecimal() and int(raw) > 0 else None
            entry['feed_number'] = raw or None
            entry['number'] = number if prefix == 'ep' and not sequential else None
            if entry['number'] is not None:
                if number in reserved:
                    raise ValueError(f'Duplicate episode number {number}. Use --sequential '
                                     'for a global chronological sequence across seasons.')
                reserved.add(number)
        candidate = 1
        for entry in group:
            if entry['number'] is None:
                while candidate in reserved:
                    candidate += 1
                entry['number'] = candidate
                reserved.add(candidate)
                if prefix == 'ep' and not sequential:
                    print('WARNING: Assigned fallback number for ' + field(entry['xml'], 'title'),
                          file=sys.stderr)
            entry['name'] = f"{prefix}{entry['number']:03d}"


def run(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    # Every invocation is an independent snapshot, so old archives cannot be overwritten.
    dest = root / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    dest.mkdir()
    print(f'Archive: {dest.resolve()}', flush=True)
    manifest = {'feed_url': args.feed, 'captured_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                'feeds': [], 'episodes': [], 'assets': [], 'errors': []}

    def save():
        temp = dest / 'manifest.json.part'
        temp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
        temp.replace(dest / 'manifest.json')

    entries, seen_items, seen_pages = [], set(), set()
    url = args.feed
    try:
        for page in range(1, args.max_pages + 1):
            if url in seen_pages:
                raise ValueError('Feed pagination loop detected')
            seen_pages.add(url)
            path = dest / ('feed.xml' if page == 1 else f'feed-page{page:03d}.xml')
            meta = transfer(url, path, args.timeout, args.attempts)
            manifest['feeds'].append({'url': url, 'file': path.name, **meta})
            save()
            raw = path.read_bytes()
            # Reject DTDs/entities including UTF-16/32 encodings, without changing saved bytes.
            if re.search(br'<!\s*(DOCTYPE|ENTITY)\b', raw.replace(b'\x00', b''), re.I):
                raise ValueError('DTD/entity declarations are not supported')
            tree = ET.fromstring(raw)
            channel = tree.find('channel')
            if channel is None:
                raise ValueError('Expected an RSS feed containing a channel')
            fallback_images = images(channel)
            base = meta['resolved_url']
            for item in channel.findall('item'):
                enclosure = item.find('enclosure')
                identity = field(item, 'guid') or (enclosure.get('url') if enclosure is not None else '')
                identity = identity or hashlib.sha256(ET.tostring(item)).hexdigest()
                if identity in seen_items:
                    continue
                seen_items.add(identity)
                entries.append({'xml': item, 'base': base, 'id': identity,
                                'show_images': fallback_images})
            next_links = [n for n in channel.findall(f'{{{ATOM}}}link')
                          if n.get('rel') == 'next' and n.get('href')]
            if not next_links:
                break
            url = urllib.parse.urljoin(base, next_links[0].get('href'))
        else:
            raise ValueError('Pagination exceeds --max-pages; raise the limit')
        if not entries:
            raise ValueError('Feed contains no episodes')
        assign_names(entries, args.sequential,
                     re.compile(args.bonus_regex, re.I) if args.bonus_regex else None)
        used_paths = set()
        for entry in entries:
            item, base, name = entry['xml'], entry['base'], entry['name']
            record = {'name': name, 'title': field(item, 'title'), 'guid': entry['id'],
                      'feed_episode_number': entry['feed_number'],
                      'season': field(item, f'{{{IT}}}season'), 'type': entry['prefix'],
                      'published': field(item, 'pubDate'), 'assets': [], 'missing': []}
            manifest['episodes'].append(record)
            print(f"{name}: {record['title']}", flush=True)
            assets = []
            for node in item.findall('enclosure'):
                if node.get('url'):
                    assets.append(('audio', node.get('url'), node.get('type', '')))
            if not assets:
                manifest['errors'].append(f'{name}: no enclosure URL')
                record['missing'].append('audio')
            for kind, tag in [('transcripts', 'transcript'), ('chapters', 'chapters')]:
                nodes = [n for n in podnodes(item, tag) if n.get('url')]
                assets.extend((kind, n.get('url'), n.get('type', '')) for n in nodes)
                if not nodes:
                    record['missing'].append(kind)
            art = images(item) or entry['show_images']
            record['artwork_source'] = 'episode' if images(item) else 'show fallback'
            assets.extend(('images', u, m) for u, m in art)
            if not art:
                record['missing'].append('images')
            for kind, asset_url, mime in dict.fromkeys(assets):
                asset_url = urllib.parse.urljoin(base, asset_url)
                ext = '.json' if kind == 'chapters' else extension(asset_url, mime)
                relative = Path(kind) / f'{name}{ext}'
                serial = 2
                while str(relative) in used_paths:
                    relative = Path(kind) / f'{name}-{serial:02d}{ext}'
                    serial += 1
                used_paths.add(str(relative))
                asset = {'kind': kind, 'url': asset_url, 'file': str(relative)}
                record['assets'].append(asset)
                try:
                    downloader = download_chapters if kind == 'chapters' else transfer
                    asset.update(downloader(asset_url, dest / relative, args.timeout, args.attempts))
                    asset['status'] = 'downloaded'
                except Exception as exc:
                    asset.update(status='failed', error=str(exc))
                    manifest['errors'].append(f'{name}: {kind}: {exc}')
                    print(f'  FAILED: {kind}: {exc}', file=sys.stderr)
                save()
            # Prefer downloaded chapter JSON; use embedded Podlove chapters as fallback.
            downloaded_chapters = any(a['kind'] == 'chapters' and a['status'] == 'downloaded'
                                      for a in record['assets'])
            if not downloaded_chapters:
                for node in item.findall(f'{{{PSC}}}chapters')[:1]:
                    rel = Path('chapters') / f'{name}.json'
                    serial = 2
                    while str(rel) in used_paths:
                        rel = Path('chapters') / f'{name}-{serial:02d}.json'
                        serial += 1
                    used_paths.add(str(rel))
                    asset = {'kind': 'chapters', 'file': str(rel), 'source': 'embedded Podlove XML'}
                    record['assets'].append(asset)
                    try:
                        asset.update(save_chapters(ET.tostring(node, encoding='utf-8'), dest / rel))
                        asset['status'] = 'converted'
                        if 'chapters' in record['missing']:
                            record['missing'].remove('chapters')
                    except Exception as exc:
                        asset.update(status='failed', error=str(exc))
                        manifest['errors'].append(f'{name}: embedded chapters: {exc}')
                        print(f'  FAILED: embedded chapters: {exc}', file=sys.stderr)
            for chapter_asset in list(record['assets']):
                if chapter_asset['kind'] != 'chapters' or chapter_asset['status'] not in ('downloaded', 'converted'):
                    continue
                chapter_path = dest / chapter_asset['file']
                chapter_base = chapter_asset.get('source_download', {}).get('resolved_url', base)
                image_assets = download_chapter_images(chapter_path, dest / 'chapters' / 'images',
                                                       chapter_base, args.timeout, args.attempts)
                for image_asset in image_assets:
                    if 'file' in image_asset:
                        image_asset['file'] = str(Path(image_asset['file']).relative_to(dest))
                    if image_asset['status'] == 'failed':
                        manifest['errors'].append(f"{name}: chapter image: {image_asset['error']}")
                record['assets'].extend(image_assets)
            save()
    except Exception as exc:
        manifest['errors'].append(str(exc))
        save()
        raise
    save()
    print(f"Done: {len(entries)} episodes, {len(manifest['errors'])} errors. See manifest.json.")
    return 1 if manifest['errors'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('feed', nargs='?', help='RSS feed URL, quoted if it contains query parameters')
    parser.add_argument('--convert-chapters', metavar='DIRECTORY',
                        help='Convert existing chapters to JSON and download chapter images without downloading audio')
    parser.add_argument('-o', '--output', default='podcast-backup', help='Parent backup directory')
    parser.add_argument('--sequential', action='store_true',
                        help='Number regular episodes chronologically instead of using feed numbers')
    parser.add_argument('--bonus-regex', help='Also classify matching titles as bonus (case-insensitive)')
    parser.add_argument('--timeout', type=float, default=60, help='Network socket timeout in seconds')
    parser.add_argument('--attempts', type=int, default=3, help='Download attempts per file')
    parser.add_argument('--max-pages', type=int, default=100, help='RSS pagination safety limit')
    args = parser.parse_args()
    if args.convert_chapters and args.feed:
        parser.error('Use either a feed URL or --convert-chapters, not both')
    if not args.feed and not args.convert_chapters:
        parser.error('Provide a feed URL or --convert-chapters DIRECTORY')
    if args.attempts < 1 or args.timeout <= 0 or args.max_pages < 1:
        parser.error('attempts, timeout, and max-pages must be positive')
    try:
        if args.convert_chapters:
            return convert_existing_chapters(args.convert_chapters, args.timeout, args.attempts)
        return run(args)
    except KeyboardInterrupt:
        print('Interrupted. Completed files and feed snapshots remain saved.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
