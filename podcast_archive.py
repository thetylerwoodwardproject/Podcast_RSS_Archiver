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
                ext = extension(asset_url, mime)
                relative = Path(kind) / f'{name}{ext}'
                serial = 2
                while str(relative) in used_paths:
                    relative = Path(kind) / f'{name}-{serial:02d}{ext}'
                    serial += 1
                used_paths.add(str(relative))
                asset = {'kind': kind, 'url': asset_url, 'file': str(relative)}
                record['assets'].append(asset)
                try:
                    asset.update(transfer(asset_url, dest / relative, args.timeout, args.attempts))
                    asset['status'] = 'downloaded'
                except Exception as exc:
                    asset.update(status='failed', error=str(exc))
                    manifest['errors'].append(f'{name}: {kind}: {exc}')
                    print(f'  FAILED: {kind}: {exc}', file=sys.stderr)
                save()
            # Preserve embedded Podlove chapters as XML, not invented JSON.
            for index, node in enumerate(item.findall(f'{{{PSC}}}chapters'), 1):
                rel = Path('chapters') / f'{name}.embedded{index:02d}.xml'
                (dest / rel).parent.mkdir(exist_ok=True)
                (dest / rel).write_bytes(ET.tostring(node, encoding='utf-8', xml_declaration=True))
                record['assets'].append({'kind': 'chapters', 'file': str(rel), 'status': 'extracted'})
                if 'chapters' in record['missing']:
                    record['missing'].remove('chapters')
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
    parser.add_argument('feed', help='RSS feed URL, quoted if it contains query parameters')
    parser.add_argument('-o', '--output', default='podcast-backup', help='Parent backup directory')
    parser.add_argument('--sequential', action='store_true',
                        help='Number regular episodes chronologically instead of using feed numbers')
    parser.add_argument('--bonus-regex', help='Also classify matching titles as bonus (case-insensitive)')
    parser.add_argument('--timeout', type=float, default=60, help='Network socket timeout in seconds')
    parser.add_argument('--attempts', type=int, default=3, help='Download attempts per file')
    parser.add_argument('--max-pages', type=int, default=100, help='RSS pagination safety limit')
    args = parser.parse_args()
    if args.attempts < 1 or args.timeout <= 0 or args.max_pages < 1:
        parser.error('attempts, timeout, and max-pages must be positive')
    try:
        return run(args)
    except KeyboardInterrupt:
        print('Interrupted. Completed files and feed snapshots remain saved.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
