"""JSON persistence: locked atomic files locally; private conditional Blob writes on Vercel.

Mutation callbacks only change their supplied dictionary. Keeping external side effects
outside callbacks makes retries safe when two hosted requests race to update the JSON.
"""

import copy
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from filelock import FileLock
from flask import current_app, g, has_request_context

ROOT = Path(__file__).resolve().parent.parent


class DatabaseError(Exception):
    """Storage is unavailable; never replace damaged data with an empty database."""


class DatabaseConflict(DatabaseError):
    """A concurrent hosted write won; the caller can retry from a fresh snapshot."""


def fresh_data():
    """Create the documented schema and sample catalog without invented customers."""
    products = json.loads((ROOT / "data" / "products.json").read_text(encoding="utf-8"))
    return {"schema_version": 1, "products": products, "users": [], "orders": [],
            "reviews": [], "tickets": [], "notifications": [], "auth_attempts": []}


def find(data, collection, identifier):
    """Find a record by ID in an already loaded snapshot."""
    return next((row for row in data[collection] if row["id"] == identifier), None)


def insert(data, collection, record):
    """Assign an integer ID inside the same atomic mutation as the new record."""
    row = {**record, "id": max((r["id"] for r in data[collection]), default=0) + 1}
    data[collection].append(row)
    return row


def _blob_config():
    """Derive only a private delivery hostname from the server-side store token."""
    token = current_app.config["BLOB_READ_WRITE_TOKEN"]
    match = re.match(r"^vercel_blob_rw_([A-Za-z0-9]+)_", token)
    if not match:
        raise DatabaseError("A valid private Blob store token is required.")
    path = current_app.config["JSON_BLOB_PATH"]
    return token, match.group(1).lower(), path


def _blob_read():
    """Fetch an uncached, authenticated JSON snapshot and its concurrency ETag."""
    token, store, path = _blob_config()
    # cache=0 is Vercel's private-blob origin bypass, not just a cache-busting label.
    url = f"https://{store}.private.blob.vercel-storage.com/{quote(path, safe='/')}?cache=0"
    headers = {"Authorization": f"Bearer {token}", "Cache-Control": "no-cache, no-store"}
    try:
        with urlopen(Request(url, headers=headers), timeout=12) as response:
            data, etag = json.load(response), response.headers.get("ETag")
            if not etag or data.get("schema_version") != 1:
                raise DatabaseError("Private JSON storage returned an invalid snapshot.")
            return data, etag
    except HTTPError as error:
        if error.code == 404:
            return fresh_data(), None
        raise DatabaseError("Private JSON storage is unavailable.") from None
    except (URLError, OSError, ValueError):
        raise DatabaseError("Private JSON storage could not be read.") from None


def _blob_write(data, etag):
    """Use Vercel's conditional write API to avoid silently overwriting other orders."""
    token, store, path = _blob_config()
    headers = {"Authorization": f"Bearer {token}", "x-api-version": "13",
        "x-vercel-blob-store-id": store, "x-vercel-blob-access": "private",
        "x-content-type": "application/json", "x-add-random-suffix": "0",
        "x-allow-overwrite": "1" if etag else "0", "x-cache-control-max-age": "60"}
    if etag:
        headers["x-if-match"] = etag
    url = "https://blob.vercel-storage.com/?" + urlencode({"pathname": path})
    try:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        with urlopen(Request(url, data=payload, headers=headers, method="PUT"), timeout=12):
            pass
    except HTTPError as error:
        if error.code in {409, 412}:
            raise DatabaseConflict("Another update is being saved. Please try again.") from None
        raise DatabaseError("Private JSON storage could not be saved.") from None
    except (URLError, OSError):
        raise DatabaseError("Private JSON storage could not be saved.") from None


def _local_path():
    """Resolve the private file independently of Flask's public asset folder."""
    return Path(current_app.config["JSON_FILE"])


def _local_read(path):
    """Read atomically replaced files; a missing file starts with the seed catalog."""
    if not path.exists():
        return fresh_data()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("Unsupported schema")
        return data
    except (OSError, ValueError):
        raise DatabaseError("The JSON database is unreadable. Restore a backup before continuing.") from None


def read():
    """Reuse one snapshot per request; mutations invalidate that request's cache."""
    if has_request_context() and hasattr(g, "json_data"):
        return copy.deepcopy(g.json_data)
    data = _blob_read()[0] if current_app.config["BLOB_READ_WRITE_TOKEN"] else _local_read(_local_path())
    if has_request_context():
        g.json_data = data
    return copy.deepcopy(data)


def mutate(callback):
    """Run a pure dictionary mutation under a file lock or optimistic hosted retry."""
    if current_app.config["BLOB_READ_WRITE_TOKEN"]:
        for attempt in range(3):
            data, etag = _blob_read()
            result = callback(data)
            try:
                _blob_write(data, etag)
                break
            except DatabaseConflict:
                if attempt == 2:
                    raise
    else:
        path = _local_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock", timeout=10):
            data = _local_read(path)
            result = callback(data)
            temporary = path.with_name(path.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
    if has_request_context():
        g.pop("json_data", None)
    return copy.deepcopy(result)
