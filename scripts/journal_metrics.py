#!/usr/bin/env python3
"""Safe, cached EasyScholar journal-metric enrichment.

The public EasyScholar endpoint accepts a secret in its query string.  This
module deliberately keeps request construction inside the client, never logs
the URL, and persists only a small, non-sensitive projection of the response.
It uses only the Python standard library so it can run in GitHub Actions with
the rest of this project.

The cache contains no claimed metric year: the API response does not provide
one.  Callers should not infer a year from the retrieval timestamp.
"""

import datetime as _datetime
import http.client
import json
import math
import os
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional


API_URL = "https://www.easyscholar.cc/open/getPublicationRank"
SECRET_ENV = "EASYSCHOLAR_SECRET_KEY"
BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_SCHEMA_VERSION = 1
MIN_REQUEST_INTERVAL_SECONDS = 0.5  # EasyScholar documents a 2 req/s limit.
MAX_PUBLICATION_NAME_LENGTH = 200
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RETRY_DELAY_SECONDS = 5.0
DEFAULT_CACHE_TTL_DAYS = 180
DEFAULT_NOT_FOUND_CACHE_TTL_DAYS = 30
MAX_CACHE_TTL_DAYS = 730

CACHE_FIELDS = (
    "if",
    "jcr",
    "cas",
    "source",
    "retrieved_at",
    "query_name",
    "status",
)
_CACHEABLE_STATUSES = {"matched", "not_found", "not_applicable"}
_PREPRINT_NAMES = {
    "arxiv",
    "biorxiv",
    "medrxiv",
    "research square",
    "ssrn",
}
_ISSN_RE = re.compile(r"(?<![0-9])([0-9]{4})-?([0-9]{3}[0-9Xx])(?![0-9])")
_QUARTILE_RE = re.compile(r"(?<![A-Za-z0-9])Q\s*([1-4])(?![0-9])", re.IGNORECASE)
_IF_RE = re.compile(r"^(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,6})?$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class InvalidPublicationName(ValueError):
    """Raised when a journal name is unsafe or unsuitable for an API query."""


class _RetryableRequestError(Exception):
    pass


class _NonRetryableRequestError(Exception):
    pass


def _load_env_file(path: Path) -> None:
    """Load local development settings without replacing real environment."""
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        return


_load_env_file(BASE_DIR / "config.env")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _contains_control_characters(value: str) -> bool:
    # Reject all Unicode control, surrogate, private-use, unassigned and format
    # characters.  None is useful in a journal name and several are invisible.
    return any(unicodedata.category(character).startswith("C") for character in value)


def sanitize_publication_name(value: str) -> str:
    """Return a bounded journal name suitable for an exact API lookup.

    The API itself performs name matching, so punctuation and letter case are
    retained.  Invisible/control characters are rejected rather than silently
    changed, while ordinary whitespace is collapsed.
    """
    if not isinstance(value, str):
        raise InvalidPublicationName("publication name must be text")
    if _contains_control_characters(value):
        raise InvalidPublicationName("publication name contains control characters")
    cleaned = unicodedata.normalize("NFKC", value)
    if _contains_control_characters(cleaned):
        raise InvalidPublicationName("publication name contains control characters")
    cleaned = " ".join(cleaned.split()).strip()
    if not cleaned:
        raise InvalidPublicationName("publication name is empty")
    if len(cleaned) > MAX_PUBLICATION_NAME_LENGTH:
        raise InvalidPublicationName("publication name is too long")
    return cleaned


def normalize_journal_name(value: str) -> str:
    """Return a conservative cache key for a journal name."""
    try:
        text = sanitize_publication_name(value).casefold()
    except InvalidPublicationName:
        return ""
    if text.startswith("the "):
        text = text[4:]
    text = re.sub(r"[^\w&]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def normalize_issn(value: object) -> str:
    """Return an eight-character ISSN cache key or an empty string."""
    if not isinstance(value, str):
        return ""
    match = _ISSN_RE.fullmatch(value.strip())
    if not match:
        return ""
    return (match.group(1) + match.group(2)).upper()


def _extract_issns(record: Mapping[str, object]) -> List[str]:
    found: List[str] = []
    for field in (
        "issn", "issn_linking", "eissn", "journal_issn", "journal_eissn",
    ):
        raw = record.get(field)
        values: Iterable[object] = raw if isinstance(raw, (list, tuple)) else (raw,)
        for value in values:
            if not isinstance(value, str):
                continue
            for match in _ISSN_RE.finditer(value):
                issn = (match.group(1) + match.group(2)).upper()
                if issn not in found:
                    found.append(issn)
    return found


def _record_cache_keys(record: Mapping[str, object]) -> List[str]:
    keys = ["issn:" + issn for issn in _extract_issns(record)]
    normalized_name = normalize_journal_name(str(record.get("journal") or ""))
    if normalized_name:
        keys.append("name:" + normalized_name)
    return keys


def _safe_text(value: object, maximum_length: int = 128) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text or len(text) > maximum_length or _contains_control_characters(text):
        return ""
    # These characters are unnecessary in the expected metric values and can
    # be hazardous if a future frontend accidentally interpolates them as HTML.
    if any(character in text for character in "<>&"):
        return ""
    return text


def _safe_impact_factor(value: object) -> str:
    text = _safe_text(value, maximum_length=32)
    if not text or not _IF_RE.fullmatch(text):
        return ""
    try:
        number = float(text)
    except ValueError:
        return ""
    if not math.isfinite(number) or number < 0 or number > 1000:
        return ""
    return text


def _safe_quartile(value: object) -> str:
    text = _safe_text(value, maximum_length=64)
    if not text:
        return ""
    match = _QUARTILE_RE.search(text)
    return "Q" + match.group(1) if match else ""


def _combined_jcr(sci: object, ssci: object) -> str:
    sci_rank = _safe_quartile(sci)
    ssci_rank = _safe_quartile(ssci)
    if sci_rank and ssci_rank and sci_rank != ssci_rank:
        return "SCI {0} / SSCI {1}".format(sci_rank, ssci_rank)
    return sci_rank or ssci_rank


def _timestamp(now: Optional[_datetime.datetime] = None) -> str:
    current = now or _datetime.datetime.now(_datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_datetime.timezone.utc)
    current = current.astimezone(_datetime.timezone.utc).replace(microsecond=0)
    return current.isoformat().replace("+00:00", "Z")


def _utc_now(now: Optional[_datetime.datetime] = None) -> _datetime.datetime:
    current = now or _datetime.datetime.now(_datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_datetime.timezone.utc)
    return current.astimezone(_datetime.timezone.utc)


def _cache_entry_is_fresh(
    entry: Mapping[str, str],
    now: _datetime.datetime,
) -> bool:
    """Return whether a cached result is still suitable for reuse."""
    status = entry.get("status")
    if status == "not_applicable":
        return True
    try:
        retrieved_at = _datetime.datetime.strptime(
            entry.get("retrieved_at", ""), "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=_datetime.timezone.utc)
    except (TypeError, ValueError):
        return False

    default_days = (
        DEFAULT_NOT_FOUND_CACHE_TTL_DAYS
        if status == "not_found" else DEFAULT_CACHE_TTL_DAYS
    )
    env_name = (
        "EASYSCHOLAR_NOT_FOUND_CACHE_DAYS"
        if status == "not_found" else "EASYSCHOLAR_CACHE_DAYS"
    )
    ttl_days = min(MAX_CACHE_TTL_DAYS, max(1, _env_int(env_name, default_days)))
    age = _utc_now(now) - retrieved_at
    # A timestamp far in the future is malformed rather than perpetually fresh.
    if age < _datetime.timedelta(days=-1):
        return False
    return age <= _datetime.timedelta(days=ttl_days)


def _empty_result(status: str, query_name: str, retrieved_at: str) -> Dict[str, str]:
    return {
        "if": "",
        "jcr": "",
        "cas": "",
        "source": "EasyScholar",
        "retrieved_at": retrieved_at,
        "query_name": query_name,
        "status": status,
    }


def parse_api_response(
    payload: object,
    query_name: str,
    retrieved_at: Optional[str] = None,
) -> Dict[str, str]:
    """Parse the untrusted EasyScholar response into the cache projection."""
    try:
        clean_name = sanitize_publication_name(query_name)
    except InvalidPublicationName:
        clean_name = ""
    stamp = retrieved_at if retrieved_at and _TIMESTAMP_RE.fullmatch(retrieved_at) else _timestamp()
    invalid = _empty_result("error", clean_name, stamp)
    if not isinstance(payload, dict):
        return invalid

    code = payload.get("code")
    if str(code) != "200":
        return invalid
    message = payload.get("msg")
    if message is not None and str(message).upper() != "SUCCESS":
        return invalid

    data = payload.get("data")
    if not isinstance(data, dict):
        return invalid
    official = data.get("officialRank")
    if not isinstance(official, dict) or "all" not in official:
        return invalid
    ranks = official.get("all")
    if not isinstance(ranks, dict):
        return invalid

    result = {
        "if": _safe_impact_factor(ranks.get("sciif")),
        "jcr": _combined_jcr(ranks.get("sci"), ranks.get("ssci")),
        "cas": (
            _safe_text(ranks.get("sciUp"))
            or _safe_text(ranks.get("sciBase"))
            or _safe_text(ranks.get("sciUpSmall"))
        ),
        "source": "EasyScholar",
        "retrieved_at": stamp,
        "query_name": clean_name,
        "status": "matched",
    }
    if not any(result[field] for field in ("if", "jcr", "cas")):
        result["status"] = "not_found"
    return result


class EasyScholarClient:
    """Small EasyScholar HTTP client with rate limiting and bounded retries."""

    def __init__(
        self,
        secret_key: Optional[str] = None,
        *,
        opener: Optional[Callable] = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Optional[Callable[[], _datetime.datetime]] = None,
        request_interval: Optional[float] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        raw_secret = os.environ.get(SECRET_ENV, "") if secret_key is None else secret_key
        self._secret_key = raw_secret.strip() if isinstance(raw_secret, str) else ""
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleeper
        self._monotonic = monotonic
        self._now = now or (lambda: _datetime.datetime.now(_datetime.timezone.utc))
        configured_interval = (
            _env_float("EASYSCHOLAR_REQUEST_DELAY", 0.6)
            if request_interval is None else float(request_interval)
        )
        configured_timeout = (
            _env_float("EASYSCHOLAR_TIMEOUT", 20.0)
            if timeout is None else float(timeout)
        )
        configured_retries = (
            _env_int("EASYSCHOLAR_RETRIES", 3)
            if max_retries is None else int(max_retries)
        )
        self._request_interval = max(MIN_REQUEST_INTERVAL_SECONDS, configured_interval)
        self._timeout = min(60.0, max(1.0, configured_timeout))
        self._max_retries = min(4, max(0, configured_retries))
        self._last_request_started_at: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return bool(self._secret_key)

    def _throttle(self) -> None:
        now = self._monotonic()
        if self._last_request_started_at is not None:
            remaining = self._request_interval - (now - self._last_request_started_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request_started_at = now

    def _request_payload(self, publication_name: str) -> object:
        # Do not log or expose this URL: the required secret is in its query.
        query = urllib.parse.urlencode({
            "secretKey": self._secret_key,
            "publicationName": publication_name,
        })
        request = urllib.request.Request(
            API_URL + "?" + query,
            headers={
                "Accept": "application/json",
                "User-Agent": "aging-sequencing-daily/1.0",
            },
            method="GET",
        )
        try:
            response = self._opener(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code <= 599:
                raise _RetryableRequestError() from None
            raise _NonRetryableRequestError() from None
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise _RetryableRequestError() from None

        try:
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
                raise _RetryableRequestError()
            if isinstance(status, int) and not 200 <= status <= 299:
                raise _NonRetryableRequestError()
            try:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                ConnectionError,
                http.client.IncompleteRead,
                OSError,
            ):
                raise _RetryableRequestError() from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except (OSError, TimeoutError, socket.timeout):
                    pass

        if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_RESPONSE_BYTES:
            raise _NonRetryableRequestError()
        try:
            return json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _NonRetryableRequestError() from None

    def lookup(self, publication_name: str) -> Dict[str, str]:
        stamp = _timestamp(self._now())
        try:
            clean_name = sanitize_publication_name(publication_name)
        except InvalidPublicationName:
            return _empty_result("invalid_name", "", stamp)
        if not self.enabled:
            return _empty_result("unavailable_no_secret", clean_name, stamp)

        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                payload = self._request_payload(clean_name)
                return parse_api_response(payload, clean_name, stamp)
            except _RetryableRequestError:
                if attempt >= self._max_retries:
                    break
                delay = min(0.5 * (2 ** attempt), MAX_RETRY_DELAY_SECONDS)
                self._sleep(delay)
            except _NonRetryableRequestError:
                break
        return _empty_result("error", clean_name, stamp)


def _safe_cache_key(value: object) -> str:
    if not isinstance(value, str) or len(value) > 300 or _contains_control_characters(value):
        return ""
    if value.startswith("issn:"):
        return value if normalize_issn(value[5:]) else ""
    if value.startswith("name:") and normalize_journal_name(value[5:]) == value[5:]:
        return value
    return ""


def _clean_cached_entry(value: object) -> Optional[Dict[str, str]]:
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    if status not in _CACHEABLE_STATUSES:
        return None
    try:
        query_name = sanitize_publication_name(str(value.get("query_name") or ""))
    except InvalidPublicationName:
        return None
    retrieved_at = _safe_text(value.get("retrieved_at"), maximum_length=32)
    if not _TIMESTAMP_RE.fullmatch(retrieved_at):
        return None
    source = value.get("source")
    if source not in {"EasyScholar", "preprint"}:
        return None

    cleaned = {
        "if": _safe_impact_factor(value.get("if")),
        "jcr": _safe_text(value.get("jcr"), maximum_length=64),
        "cas": _safe_text(value.get("cas"), maximum_length=128),
        "source": source,
        "retrieved_at": retrieved_at,
        "query_name": query_name,
        "status": status,
    }
    if status == "matched" and not any(cleaned[field] for field in ("if", "jcr", "cas")):
        return None
    if status in {"not_found", "not_applicable"}:
        cleaned["if"] = ""
        cleaned["jcr"] = ""
        cleaned["cas"] = ""
    return cleaned


def load_cache(cache_path: Path) -> Dict[str, Dict[str, str]]:
    """Load and validate cache entries; malformed content is ignored."""
    path = Path(cache_path)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        return {}
    raw_entries = payload.get("journals")
    if not isinstance(raw_entries, dict):
        return {}

    entries: Dict[str, Dict[str, str]] = {}
    for raw_key, raw_value in raw_entries.items():
        key = _safe_cache_key(raw_key)
        entry = _clean_cached_entry(raw_value)
        if key and entry:
            entries[key] = entry
    return entries


def save_cache(cache_path: Path, entries: Mapping[str, Mapping[str, str]]) -> None:
    """Atomically persist only the validated, non-sensitive cache projection."""
    cleaned: Dict[str, Dict[str, str]] = {}
    for raw_key, raw_value in entries.items():
        key = _safe_cache_key(raw_key)
        entry = _clean_cached_entry(raw_value)
        if key and entry:
            cleaned[key] = {field: entry[field] for field in CACHE_FIELDS}

    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "journals": dict(sorted(cleaned.items())),
    }
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(str(temporary), str(path))


def _is_preprint(record: Mapping[str, object]) -> bool:
    for field in ("journal", "source"):
        name = normalize_journal_name(str(record.get(field) or ""))
        if any(name == prefix or name.startswith(prefix + " ") for prefix in _PREPRINT_NAMES):
            return True
    doi = str(record.get("doi") or "").strip().lower()
    if doi.startswith("10.1101/"):
        return True
    article_type = record.get("article_type")
    values = article_type if isinstance(article_type, (list, tuple)) else (article_type,)
    return any("preprint" in str(value or "").casefold() for value in values)


def _preprint_result(record: Mapping[str, object], stamp: str) -> Dict[str, str]:
    raw_name = str(record.get("journal") or record.get("source") or "preprint")
    try:
        query_name = sanitize_publication_name(raw_name)
    except InvalidPublicationName:
        query_name = "preprint"
    return {
        "if": "",
        "jcr": "",
        "cas": "",
        "source": "preprint",
        "retrieved_at": stamp,
        "query_name": query_name,
        "status": "not_applicable",
    }


def _lookup_cached(
    record: Mapping[str, object],
    entries: Mapping[str, Mapping[str, str]],
    now: _datetime.datetime,
) -> Optional[Dict[str, str]]:
    # ISSN keys are deliberately ordered before the normalized-name fallback.
    for key in _record_cache_keys(record):
        if key in entries and _cache_entry_is_fresh(entries[key], now):
            return dict(entries[key])
    return None


def _combined_source(existing: object, incoming: object) -> str:
    parts: List[str] = []
    for raw_source in (existing, incoming):
        for source in str(raw_source or "").split("+"):
            source = source.strip()
            if source and source not in parts:
                parts.append(source)
    return "+".join(parts)


def _apply_result(record: Dict[str, object], result: Mapping[str, str]) -> None:
    status = result.get("status", "error")
    existing_values = {
        field: str(record.get(field) or "").strip()
        for field in ("journal_if", "journal_jcr", "journal_cas")
    }
    previous_source = record.get("journal_metrics_source", "")
    already_annotated = any(
        existing_values.values()
    )
    retained_existing_value = False
    if status == "matched":
        for source_field, record_field in (
            ("if", "journal_if"),
            ("jcr", "journal_jcr"),
            ("cas", "journal_cas"),
        ):
            value = result.get(source_field, "")
            if value:
                record[record_field] = value
            elif existing_values[record_field]:
                retained_existing_value = True
    elif status == "not_applicable":
        record["journal_if"] = ""
        record["journal_jcr"] = ""
        record["journal_cas"] = ""

    # A disabled/failed/no-result API lookup must not replace the provenance
    # of a usable local annotation. It may only add metadata when no fallback
    # value exists, or when the API actually matched / is not applicable.
    if (
        already_annotated
        and record.get("journal_metrics_source")
        and status not in {"matched", "not_applicable"}
    ):
        return

    result_source = result.get("source", "")
    if status == "matched" and retained_existing_value and previous_source:
        result_source = _combined_source(previous_source, result_source)

    record["journal_metrics_status"] = status
    record["journal_metrics_source"] = result_source
    record["journal_metrics_retrieved_at"] = result.get("retrieved_at", "")
    record["journal_metrics_query_name"] = result.get("query_name", "")


def enrich_records(
    records: Iterable[Mapping[str, object]],
    cache_path: Path,
    *,
    fetch_missing: bool = True,
    client: Optional[EasyScholarClient] = None,
    now: Optional[_datetime.datetime] = None,
) -> List[Dict[str, object]]:
    """Return journal-enriched record copies and update the cache if needed.

    Existing annotations remain untouched when the API is unavailable, returns
    no match, or fails.  A successful API response fills/updates only metrics it
    actually supplied.  Preprints are explicitly marked not applicable.
    """
    entries = load_cache(cache_path)
    changed = False
    active_client = client
    session_results: Dict[str, Dict[str, str]] = {}
    enriched: List[Dict[str, object]] = []
    current_time = _utc_now(now)
    stamp = _timestamp(current_time)

    for raw_record in records:
        if not isinstance(raw_record, Mapping):
            raise TypeError("each record must be a mapping")
        record: Dict[str, object] = dict(raw_record)
        keys = _record_cache_keys(record)

        if _is_preprint(record):
            cached_preprint = _lookup_cached(record, entries, current_time)
            if cached_preprint and cached_preprint.get("status") == "not_applicable":
                result = cached_preprint
            else:
                result = _preprint_result(record, stamp)
            # Cache only recognized preprint journal names, not a possibly
            # incorrect generic article-type classification.
            journal_name = normalize_journal_name(str(record.get("journal") or ""))
            if any(
                journal_name == prefix or journal_name.startswith(prefix + " ")
                for prefix in _PREPRINT_NAMES
            ):
                key = "name:" + journal_name
                if entries.get(key) != result:
                    entries[key] = dict(result)
                    changed = True
            _apply_result(record, result)
            enriched.append(record)
            continue

        result = _lookup_cached(record, entries, current_time)
        if result is None:
            # A complete local annotation is already useful and avoids spending
            # API quota. EasyScholar remains the cache-backed fallback for
            # genuinely missing IF/JCR data.
            metric_sources = {
                source.strip()
                for source in str(record.get("journal_metrics_source") or "").split("+")
                if source.strip()
            }
            has_local_core_metrics = bool(
                str(record.get("journal_if") or "").strip()
                and str(record.get("journal_jcr") or "").strip()
                and "EasyScholar" not in metric_sources
            )
            if has_local_core_metrics:
                enriched.append(record)
                continue
            raw_name = str(record.get("journal") or "")
            normalized_name = normalize_journal_name(raw_name)
            if not normalized_name:
                result = _empty_result("missing_journal", "", stamp)
            elif not fetch_missing:
                try:
                    clean_name = sanitize_publication_name(raw_name)
                except InvalidPublicationName:
                    clean_name = ""
                result = _empty_result("cache_miss", clean_name, stamp)
            elif normalized_name in session_results:
                result = dict(session_results[normalized_name])
            else:
                if active_client is None:
                    active_client = EasyScholarClient()
                result = active_client.lookup(raw_name)
                session_results[normalized_name] = dict(result)

            if result.get("status") in {"matched", "not_found"}:
                for key in keys:
                    if entries.get(key) != result:
                        entries[key] = {field: str(result.get(field, "")) for field in CACHE_FIELDS}
                        changed = True

        _apply_result(record, result)
        enriched.append(record)

    if changed:
        save_cache(cache_path, entries)
    return enriched


__all__ = [
    "API_URL",
    "EasyScholarClient",
    "InvalidPublicationName",
    "enrich_records",
    "load_cache",
    "normalize_issn",
    "normalize_journal_name",
    "parse_api_response",
    "sanitize_publication_name",
    "save_cache",
]
