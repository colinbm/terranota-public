"""Which maps' licences let Terra Nota use them.

A map's licence is in its IIIF manifest, at the holding library. Fetching every
manifest each day would take hours, so what a manifest says about rights is
kept in a cache file between builds and only fetched again now and then. The
cache holds the raw statements, not a verdict, so changing the rules below
takes effect on the next build without fetching anything.

The rules match `AllmapsLibrary.classify` in the app, which checks a map again
when it's added.
"""

import concurrent.futures
import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urlparse

# Internet Archive refuses agents with "(https://…" in them.
USER_AGENT = "TerraNota/1.0 (+https://terranota.app)"

# The app searches the National Library of Scotland itself, with licences.
EXCLUDED_HOSTS = {"map-view.nls.uk"}
# Libraries whose licences don't allow commercial use, accepted while Terra
# Nota is free (`AllmapsLibrary.noncommercialSources` in the app). A map from
# one is still left out if its manifest says something stricter.
NONCOMMERCIAL_HOSTS = {"www.davidrumsey.com", "gallica.bnf.fr", "openapi.bnf.fr"}

ALLOWED_URIS = [
    "creativecommons.org/publicdomain/mark",
    "creativecommons.org/publicdomain/zero",
    "creativecommons.org/licenses/by/",
    "rightsstatements.org/vocab/NoC-US",
    "rightsstatements.org/vocab/NKC",
]
RESTRICTED_TEXT = ["non-commercial", "noncommercial", "by-nc", "by-nd", "by-sa", "all rights reserved"]
OPEN_TEXT = ["public domain", "gemeinfrei", "no known copyright", "no known restrictions",
             "free of known restrictions", "out of copyright"]
# For a map from NONCOMMERCIAL_HOSTS: non-commercial and share-alike terms pass
# too, leaving only stricter ones out.
NONCOMMERCIAL_URIS = [
    "rightsstatements.org/vocab/NoC-NC",
    "creativecommons.org/licenses/by-nc/",
    "creativecommons.org/licenses/by-nc-sa/",
]
NONCOMMERCIAL_RESTRICTED_TEXT = ["by-nd", "nc-nd", "all rights reserved"]

# Days before a manifest is fetched again; each one waits between 1 and 2
# times this, so a day's worth of first fetches doesn't all come due at once.
RECHECK_DAYS = {True: 180, False: 30}
# Simultaneous requests to one library, and in all.
PER_HOST, WORKERS = 4, 48
# Longest run of a single text value kept in the cache.
MAX_TEXT = 1000

DIGITAL_COMMONWEALTH = re.compile(r"ark\.digitalcommonwealth\.org/ark:/50959/([^/]+)/manifest")


def is_allowed(rights, text, noncommercial=False):
    """Public domain, no known copyright, or attribution only. Anything
    non-commercial, no-derivatives or share-alike is out, and so is a map
    whose licence can't be found. `text` may be HTML: a licence is often only
    in a link. With `noncommercial` (a map from NONCOMMERCIAL_HOSTS),
    non-commercial and share-alike terms pass, and so does a manifest that
    doesn't say: those libraries' terms are known."""
    if any(pattern in uri for uri in rights
           for pattern in ALLOWED_URIS + (NONCOMMERCIAL_URIS if noncommercial else [])):
        return True
    if any("creativecommons.org" in uri or "rightsstatements.org" in uri for uri in rights):
        return False
    combined = " ".join(text).lower()
    if any(word in combined for word in (NONCOMMERCIAL_RESTRICTED_TEXT if noncommercial else RESTRICTED_TEXT)):
        return False
    return noncommercial or any(word in combined for word in OPEN_TEXT)


def strings(value):
    """Text out of a IIIF value: a string, a v3 language map
    (`{"en": ["…"]}`), a v2 `{"@value": "…"}`, or a list of these."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in strings(item)]
    if isinstance(value, dict):
        if "@value" in value:
            return strings(value["@value"])
        return [s for item in value.values() for s in strings(item)]
    return []


def manifest_statements(manifest):
    """(rights URIs, free-text statements) from a IIIF manifest."""
    # v3 `rights`, v2 `license`: a URI. v3 `requiredStatement`, v2
    # `attribution` and metadata rows: free text.
    rights = strings(manifest.get("rights")) + strings(manifest.get("license"))
    required = manifest.get("requiredStatement")
    text = strings(required.get("value") if isinstance(required, dict) else None)
    text += strings(manifest.get("attribution"))
    for row in manifest.get("metadata") or []:
        if not isinstance(row, dict):
            continue
        label = " ".join(strings(row.get("label"))).lower()
        if any(word in label for word in ("right", "licen", "copyright")):
            text += strings(row.get("value"))
    return rights, text


class Unavailable(Exception):
    """The library couldn't be reached; try again next build."""

    def __init__(self, status=None):
        super().__init__(status)
        self.status = status


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Missing or refused (the Library of Congress is behind a challenge
        # page): nothing to read, so the map counts as unlicensed until the
        # next check. Anything else may pass, so try again next build.
        if 400 <= error.code < 500 and error.code not in (408, 429):
            return None
        raise Unavailable(error.code) from error
    except ValueError:
        return None
    except Exception as error:
        raise Unavailable() from error


def statements(manifest_url):
    """(rights, text) for a manifest, fetched from its library."""
    # Digital Commonwealth's manifest links are stale, but its catalogue
    # records carry rights for each item.
    if match := DIGITAL_COMMONWEALTH.search(manifest_url):
        record = fetch_json(f"https://www.digitalcommonwealth.org/search/commonwealth:{match[1]}.json")
        attributes = ((record or {}).get("data") or {}).get("attributes") or {}
        # `license_ss` is what re-use is allowed; `rights_ss` is copyright status.
        return [], strings(attributes.get("license_ss")) + strings(attributes.get("rights_ss"))
    manifest = fetch_json(manifest_url)
    return manifest_statements(manifest) if isinstance(manifest, dict) else ([], [])


class Licences:
    """The cache of rights statements, by manifest URL."""

    def __init__(self, path):
        self.path = path
        self.entries = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.entries = json.load(f)
        self.today = datetime.date.today()

    def allowed(self, manifest_url, service):
        """True or False, or None if the manifest hasn't been read yet."""
        host = urlparse(service).hostname
        if host in EXCLUDED_HOSTS:
            return False
        noncommercial = host in NONCOMMERCIAL_HOSTS
        if not manifest_url:
            # Rumsey's maps don't link one (the app works it out), and its
            # manifests don't state a licence anyway.
            return noncommercial
        entry = self.entries.get(manifest_url)
        return is_allowed(entry["rights"], entry["text"], noncommercial) if entry else None

    def due(self, manifest_url):
        entry = self.entries.get(manifest_url)
        if not entry:
            return True
        days = RECHECK_DAYS[is_allowed(entry["rights"], entry["text"])]
        days += zlib.crc32(manifest_url.encode()) % days
        return (self.today - datetime.date.fromisoformat(entry["checked"])).days >= days

    def refresh(self, manifest_urls, minutes):
        """Fetches the manifests that are new or due, for up to `minutes`.
        Returns (fetched, failed, left over)."""
        wanted = sorted({url for url in manifest_urls if url and self.due(url)})
        by_host = {}
        for url in wanted:
            by_host.setdefault(urlparse(url).hostname, []).append(url)
        # Each library gets up to PER_HOST lanes, each fetching in turn;
        # the biggest libraries start first.
        lanes = [urls[i::PER_HOST] for urls in sorted(by_host.values(), key=len, reverse=True)
                 for i in range(min(PER_HOST, len(urls)))]
        deadline = time.monotonic() + minutes * 60
        lock = threading.Lock()
        counts = {"fetched": 0, "failed": 0}

        def run(lane):
            for url in lane:
                if time.monotonic() > deadline:
                    return
                try:
                    rights, text = statements(url)
                except Unavailable as error:
                    with lock:
                        counts["failed"] += 1
                    if error.status in (429, 503):  # Being asked to slow down.
                        time.sleep(30)
                    continue
                entry = {"rights": [s[:MAX_TEXT] for s in rights], "text": [s[:MAX_TEXT] for s in text],
                         "checked": self.today.isoformat()}
                with lock:
                    self.entries[url] = entry
                    counts["fetched"] += 1

        with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(run, lanes))
        left = sum(1 for url in wanted if self.due(url))
        return counts["fetched"], counts["failed"], left - counts["failed"]

    def save(self, keep):
        """Writes the cache, dropping manifests no longer in the dump."""
        if not self.path:
            return
        entries = {url: self.entries[url] for url in sorted(keep) if url in self.entries}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, separators=(",", ":"))
