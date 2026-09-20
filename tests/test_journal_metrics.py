import contextlib
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import journal_metrics


FIXED_TIME = "2026-09-20T01:02:03Z"
FIXED_DATETIME = datetime.datetime(2026, 9, 20, 1, 2, 3, tzinfo=datetime.timezone.utc)


def api_payload(**overrides):
    ranks = {
        "sciif": "12.34",
        "sci": "Q1",
        "sciUp": "生物学1区",
    }
    ranks.update(overrides)
    return {
        "code": 200,
        "msg": "SUCCESS",
        "data": {"officialRank": {"all": ranks}},
    }


class FakeResponse:
    def __init__(self, payload=None, *, status=200, raw=None):
        self.status = status
        self._body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def close(self):
        self.closed = True


def metric_entry(impact_factor=None, **overrides):
    entry = {
        "if": "3.2",
        "jcr": "Q2",
        "cas": "医学2区",
        "source": "EasyScholar",
        "retrieved_at": FIXED_TIME,
        "query_name": "Example Journal",
        "status": "matched",
    }
    if impact_factor is not None:
        entry["if"] = impact_factor
    entry.update(overrides)
    return entry


class StubClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def lookup(self, name):
        self.calls.append(name)
        return dict(self.result)


class JournalNameTests(unittest.TestCase):
    def test_sanitize_collapses_whitespace_and_normalizes_unicode(self):
        self.assertEqual(
            journal_metrics.sanitize_publication_name("  Nature\u3000Aging  "),
            "Nature Aging",
        )

    def test_sanitize_rejects_empty_control_and_overlong_names(self):
        invalid = ("", "Journal\nInjected", "A\u200bB", "x" * 201)
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(journal_metrics.InvalidPublicationName):
                    journal_metrics.sanitize_publication_name(value)

    def test_normalizers_make_stable_name_and_issn_keys(self):
        self.assertEqual(
            journal_metrics.normalize_journal_name("The Journal of Aging & Health"),
            "journal of aging & health",
        )
        self.assertEqual(journal_metrics.normalize_issn("1234-567X"), "1234567X")
        self.assertEqual(journal_metrics.normalize_issn("not-an-issn"), "")


class ResponseParsingTests(unittest.TestCase):
    def test_parses_only_required_official_rank_fields(self):
        result = journal_metrics.parse_api_response(
            api_payload(sciif="7.89", sci="Q2", sciUp="医学2区", unrelated="ignore"),
            "Nature Aging",
            FIXED_TIME,
        )
        self.assertEqual(result, {
            "if": "7.89",
            "jcr": "Q2",
            "cas": "医学2区",
            "source": "EasyScholar",
            "retrieved_at": FIXED_TIME,
            "query_name": "Nature Aging",
            "status": "matched",
        })
        self.assertNotIn("year", result)

    def test_uses_ssci_when_sci_is_absent_and_preserves_a_disagreement(self):
        ssci_only = journal_metrics.parse_api_response(
            api_payload(sci="", ssci="Q3", sciUp=""),
            "Applied Economics",
            FIXED_TIME,
        )
        self.assertEqual(ssci_only["jcr"], "Q3")

        both = journal_metrics.parse_api_response(
            api_payload(sci="Q1", ssci="Q2"),
            "Mixed Journal",
            FIXED_TIME,
        )
        self.assertEqual(both["jcr"], "SCI Q1 / SSCI Q2")

    def test_empty_rank_dictionary_is_a_cacheable_not_found(self):
        result = journal_metrics.parse_api_response(
            api_payload(sciif="", sci="", sciUp=""),
            "Unknown Journal",
            FIXED_TIME,
        )
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["if"], "")

    def test_rejects_unexpected_or_malicious_response_shapes(self):
        payloads = [
            [],
            {"code": 40002, "msg": "bad key"},
            {"code": 200, "msg": "SUCCESS", "data": []},
            {"code": 200, "msg": "SUCCESS", "data": {"officialRank": {"all": []}}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                result = journal_metrics.parse_api_response(payload, "Journal", FIXED_TIME)
                self.assertEqual(result["status"], "error")

        malicious = journal_metrics.parse_api_response(
            api_payload(
                sciif="999999999999999999999",
                sci="<script>Q1</script>",
                sciUp="<img src=x onerror=alert(1)>",
            ),
            "Journal",
            FIXED_TIME,
        )
        self.assertEqual(malicious["status"], "not_found")


class EasyScholarClientTests(unittest.TestCase):
    def _client(self, opener, **overrides):
        options = {
            "secret_key": "test-secret",
            "opener": opener,
            "now": lambda: FIXED_DATETIME,
            "sleeper": lambda _seconds: None,
            "timeout": 10.0,
            "max_retries": 0,
        }
        options.update(overrides)
        return journal_metrics.EasyScholarClient(**options)

    def test_missing_secret_never_opens_a_network_connection(self):
        def forbidden_opener(*_args, **_kwargs):
            raise AssertionError("network must not be called")

        with mock.patch.dict(os.environ, {}, clear=True):
            client = journal_metrics.EasyScholarClient(
                opener=forbidden_opener,
                now=lambda: FIXED_DATETIME,
            )
            result = client.lookup("Nature Aging")
        self.assertEqual(result["status"], "unavailable_no_secret")

    def test_request_is_encoded_but_never_printed(self):
        captured = {}

        def opener(request, timeout):
            captured["query"] = urllib.parse.parse_qs(
                urllib.parse.urlsplit(request.full_url).query
            )
            captured["timeout"] = timeout
            return FakeResponse(api_payload())

        client = self._client(opener, secret_key="secret&with=symbols")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = client.lookup("Nature & Aging")

        self.assertEqual(result["status"], "matched")
        self.assertEqual(captured["query"]["secretKey"], ["secret&with=symbols"])
        self.assertEqual(captured["query"]["publicationName"], ["Nature & Aging"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("secret&with=symbols", repr(client))

    def test_rate_limit_keeps_request_starts_half_a_second_apart(self):
        clock = {"now": 0.0}
        starts = []
        sleeps = []

        def monotonic():
            return clock["now"]

        def sleeper(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds

        def opener(_request, timeout):
            self.assertEqual(timeout, 10.0)
            starts.append(clock["now"])
            return FakeResponse(api_payload())

        client = self._client(
            opener,
            sleeper=sleeper,
            monotonic=monotonic,
            request_interval=0.01,  # Must still be clamped to the 2 req/s limit.
        )
        client.lookup("Journal One")
        client.lookup("Journal Two")

        self.assertEqual(starts, [0.0, 0.5])
        self.assertIn(0.5, sleeps)

    def test_429_and_server_errors_retry_only_up_to_the_bound(self):
        statuses = [429, 503, 200]
        calls = []
        clock = {"now": 0.0}

        def sleeper(seconds):
            clock["now"] += seconds

        def opener(_request, timeout):
            calls.append(timeout)
            return FakeResponse(api_payload(), status=statuses.pop(0))

        client = self._client(
            opener,
            max_retries=2,
            sleeper=sleeper,
            monotonic=lambda: clock["now"],
        )
        result = client.lookup("Retry Journal")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(len(calls), 3)

        failed_calls = []

        def always_fails(_request, timeout):
            failed_calls.append(timeout)
            return FakeResponse(api_payload(), status=500)

        failed_client = self._client(
            always_fails,
            max_retries=2,
            sleeper=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
        failed = failed_client.lookup("Failed Journal")
        self.assertEqual(failed["status"], "error")
        self.assertEqual(len(failed_calls), 3)

    def test_timeout_retries_and_non_retryable_client_error_does_not(self):
        calls = []

        def timeout_then_success(_request, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise TimeoutError()
            return FakeResponse(api_payload())

        client = self._client(
            timeout_then_success,
            max_retries=1,
            sleeper=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
        self.assertEqual(client.lookup("Timeout Journal")["status"], "matched")
        self.assertEqual(len(calls), 2)

        body_calls = []

        class BodyTimeoutResponse(FakeResponse):
            def read(self, size=-1):
                raise TimeoutError()

        def body_timeout_then_success(_request, timeout):
            body_calls.append(timeout)
            if len(body_calls) == 1:
                return BodyTimeoutResponse(api_payload())
            return FakeResponse(api_payload())

        body_client = self._client(
            body_timeout_then_success,
            max_retries=1,
            sleeper=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
        self.assertEqual(body_client.lookup("Body Timeout Journal")["status"], "matched")
        self.assertEqual(len(body_calls), 2)

        bad_calls = []

        def bad_request(_request, timeout):
            bad_calls.append(timeout)
            return FakeResponse(api_payload(), status=404)

        bad_client = self._client(bad_request, max_retries=4)
        self.assertEqual(bad_client.lookup("Bad Journal")["status"], "error")
        self.assertEqual(len(bad_calls), 1)

    def test_oversized_and_non_json_responses_fail_closed(self):
        oversized = self._client(
            lambda _request, timeout: FakeResponse(raw=b"x" * (1024 * 1024 + 1))
        )
        self.assertEqual(oversized.lookup("Journal")["status"], "error")

        malformed = self._client(
            lambda _request, timeout: FakeResponse(raw=b"{not-json")
        )
        self.assertEqual(malformed.lookup("Journal")["status"], "error")


class CacheAndEnrichmentTests(unittest.TestCase):
    def test_issn_cache_entry_takes_priority_over_name_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "journal_metrics_cache.json"
            entries = {
                "issn:12345678": metric_entry(impact_factor="9.9", query_name="ISSN Match"),
                "name:example journal": metric_entry(impact_factor="1.1", query_name="Name Match"),
            }
            journal_metrics.save_cache(cache_path, entries)
            client = StubClient(metric_entry())
            records = journal_metrics.enrich_records(
                [{"journal": "Example Journal", "issn": "1234-5678"}],
                cache_path,
                client=client,
            )

        self.assertEqual(records[0]["journal_if"], "9.9")
        self.assertEqual(records[0]["journal_metrics_query_name"], "ISSN Match")
        self.assertEqual(client.calls, [])

    def test_name_cache_fallback_does_not_call_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "cache.json"
            journal_metrics.save_cache(
                cache_path,
                {"name:nature aging": metric_entry(query_name="Nature Aging")},
            )
            client = StubClient(metric_entry(impact_factor="99"))
            records = journal_metrics.enrich_records(
                [{"journal": "The Nature Aging"}],
                cache_path,
                client=client,
            )

        self.assertEqual(records[0]["journal_if"], "3.2")
        self.assertEqual(client.calls, [])

    def test_stale_matched_cache_is_refreshed(self):
        stale = metric_entry(
            impact_factor="1.0",
            retrieved_at="2025-01-01T00:00:00Z",
        )
        client = StubClient(metric_entry(impact_factor="8.8"))
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"EASYSCHOLAR_CACHE_DAYS": "180"}, clear=False,
        ):
            cache_path = Path(temporary) / "cache.json"
            journal_metrics.save_cache(
                cache_path, {"name:example journal": stale},
            )
            records = journal_metrics.enrich_records(
                [{
                    "journal": "Example Journal",
                    "journal_if": "1.0",
                    "journal_jcr": "Q2",
                    "journal_metrics_status": "matched",
                    "journal_metrics_source": "EasyScholar",
                }],
                cache_path,
                client=client,
                now=FIXED_DATETIME,
            )

        self.assertEqual(client.calls, ["Example Journal"])
        self.assertEqual(records[0]["journal_if"], "8.8")

    def test_stale_not_found_cache_is_retried_sooner(self):
        stale_miss = metric_entry(
            impact_factor="",
            jcr="",
            cas="",
            retrieved_at="2026-08-01T00:00:00Z",
            status="not_found",
        )
        client = StubClient(metric_entry(impact_factor="6.6"))
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"EASYSCHOLAR_NOT_FOUND_CACHE_DAYS": "30"}, clear=False,
        ):
            cache_path = Path(temporary) / "cache.json"
            journal_metrics.save_cache(
                cache_path, {"name:example journal": stale_miss},
            )
            records = journal_metrics.enrich_records(
                [{"journal": "Example Journal"}],
                cache_path,
                client=client,
                now=FIXED_DATETIME,
            )

        self.assertEqual(client.calls, ["Example Journal"])
        self.assertEqual(records[0]["journal_if"], "6.6")

    def test_complete_local_metrics_do_not_spend_api_quota(self):
        client = StubClient(metric_entry(impact_factor="99"))
        original = {
            "journal": "Locally Annotated Journal",
            "journal_if": "5.1",
            "journal_jcr": "Q1",
            "journal_metrics_status": "matched",
            "journal_metrics_source": "local_tsv",
        }
        with tempfile.TemporaryDirectory() as temporary:
            records = journal_metrics.enrich_records(
                [original], Path(temporary) / "cache.json", client=client,
            )

        self.assertEqual(records[0]["journal_if"], "5.1")
        self.assertEqual(records[0]["journal_metrics_source"], "local_tsv")
        self.assertEqual(client.calls, [])

    def test_one_api_result_is_cached_by_issn_and_name_without_secret(self):
        result = metric_entry(
            impact_factor="8.8",
            jcr="Q1",
            cas="生物学1区",
            query_name="Aging Cell",
        )
        client = StubClient(result)
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "data" / "journal_metrics_cache.json"
            records = journal_metrics.enrich_records(
                [
                    {"journal": "Aging Cell", "issn": "1474-9718"},
                    {"journal": "Aging Cell"},
                ],
                cache_path,
                client=client,
            )
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            serialized = cache_path.read_text(encoding="utf-8")

        self.assertEqual(client.calls, ["Aging Cell"])
        self.assertEqual([item["journal_if"] for item in records], ["8.8", "8.8"])
        self.assertIn("issn:14749718", payload["journals"])
        self.assertIn("name:aging cell", payload["journals"])
        for entry in payload["journals"].values():
            self.assertEqual(set(entry), set(journal_metrics.CACHE_FIELDS))
            self.assertNotIn("year", entry)
        self.assertNotIn("secret", serialized.casefold())

    def test_not_found_is_cached_and_does_not_erase_existing_metrics(self):
        client = StubClient(metric_entry(
            impact_factor="",
            jcr="",
            cas="",
            query_name="No Rank Journal",
            status="not_found",
        ))
        original = {
            "journal": "No Rank Journal",
            "journal_if": "2.1",
            "journal_jcr": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "cache.json"
            records = journal_metrics.enrich_records(
                [original, dict(original)],
                cache_path,
                client=client,
            )
            second_client = StubClient(metric_entry(impact_factor="100"))
            again = journal_metrics.enrich_records(
                [original], cache_path, client=second_client
            )

        self.assertEqual(client.calls, ["No Rank Journal"])
        self.assertEqual(records[0]["journal_if"], "2.1")
        self.assertEqual(records[0]["journal_jcr"], "")
        self.assertEqual(again[0]["journal_metrics_status"], "not_found")
        self.assertEqual(second_client.calls, [])

    def test_failed_lookup_does_not_replace_local_metric_provenance(self):
        client = StubClient(metric_entry(
            impact_factor="",
            jcr="",
            cas="",
            query_name="Local Journal",
            status="not_found",
        ))
        original = {
            "journal": "Local Journal",
            "journal_if": "4.2",
            "journal_jcr": "Q2",
            "journal_metrics_status": "matched",
            "journal_metrics_source": "local_tsv",
            "journal_metrics_retrieved_at": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            records = journal_metrics.enrich_records(
                [original], Path(temporary) / "cache.json", client=client,
            )

        self.assertEqual(records[0]["journal_if"], "4.2")
        self.assertEqual(records[0]["journal_metrics_status"], "matched")
        self.assertEqual(records[0]["journal_metrics_source"], "local_tsv")

    def test_partial_api_match_reports_mixed_provenance(self):
        client = StubClient(metric_entry(
            impact_factor="",
            jcr="Q1",
            cas="",
            query_name="Mixed Source Journal",
        ))
        original = {
            "journal": "Mixed Source Journal",
            "journal_if": "4.2",
            "journal_jcr": "",
            "journal_cas": "生物学2区",
            "journal_metrics_status": "matched",
            "journal_metrics_source": "local_tsv",
        }
        with tempfile.TemporaryDirectory() as temporary:
            records = journal_metrics.enrich_records(
                [original], Path(temporary) / "cache.json", client=client,
                now=FIXED_DATETIME,
            )

        self.assertEqual(records[0]["journal_if"], "4.2")
        self.assertEqual(records[0]["journal_jcr"], "Q1")
        self.assertEqual(records[0]["journal_cas"], "生物学2区")
        self.assertEqual(
            records[0]["journal_metrics_source"], "local_tsv+EasyScholar",
        )

    def test_api_failure_never_overwrites_values_or_cache(self):
        client = StubClient(metric_entry(
            impact_factor="",
            jcr="",
            cas="",
            query_name="Failure Journal",
            status="error",
        ))
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "cache.json"
            records = journal_metrics.enrich_records(
                [{
                    "journal": "Failure Journal",
                    "journal_if": "4.2",
                    "journal_jcr": "",
                    "journal_cas": "",
                }],
                cache_path,
                client=client,
            )
            exists = cache_path.exists()

        self.assertFalse(exists)
        self.assertEqual(records[0]["journal_if"], "4.2")
        self.assertEqual(records[0]["journal_jcr"], "")
        self.assertEqual(records[0]["journal_metrics_status"], "error")

    def test_missing_secret_preserves_values_without_network_or_cache_write(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("network must not be called")

        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {}, clear=True
        ):
            cache_path = Path(temporary) / "cache.json"
            client = journal_metrics.EasyScholarClient(
                opener=forbidden,
                now=lambda: FIXED_DATETIME,
            )
            records = journal_metrics.enrich_records(
                [{"journal": "Nature Aging", "journal_if": "10.1"}],
                cache_path,
                client=client,
            )
            exists = cache_path.exists()

        self.assertFalse(exists)
        self.assertEqual(records[0]["journal_if"], "10.1")
        self.assertEqual(records[0]["journal_metrics_status"], "unavailable_no_secret")

    def test_biorxiv_is_not_applicable_and_never_queried(self):
        client = StubClient(metric_entry(impact_factor="99"))
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "cache.json"
            records = journal_metrics.enrich_records(
                [{
                    "journal": "bioRxiv",
                    "doi": "10.1101/2026.01.01.123456",
                    "journal_if": "wrong old value",
                    "journal_jcr": "Q1",
                }],
                cache_path,
                client=client,
            )
            cache = journal_metrics.load_cache(cache_path)

        self.assertEqual(client.calls, [])
        self.assertEqual(records[0]["journal_metrics_status"], "not_applicable")
        self.assertEqual(records[0]["journal_if"], "")
        self.assertEqual(records[0]["journal_jcr"], "")
        self.assertEqual(cache["name:biorxiv"]["source"], "preprint")

    def test_pubmed_biorxiv_long_title_is_not_applicable(self):
        client = StubClient(metric_entry(impact_factor="99"))
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "cache.json"
            records = journal_metrics.enrich_records(
                [{
                    "journal": "bioRxiv : the preprint server for biology",
                    "doi": "10.64898/2026.08.05.743027",
                }],
                cache_path,
                client=client,
            )
            cache = journal_metrics.load_cache(cache_path)

        self.assertEqual(client.calls, [])
        self.assertEqual(records[0]["journal_metrics_status"], "not_applicable")
        self.assertIn("name:biorxiv the preprint server for biology", cache)

    def test_cached_preprint_annotation_is_reused_without_timestamp_churn(self):
        cached = metric_entry(
            impact_factor="",
            jcr="",
            cas="",
            source="preprint",
            query_name="bioRxiv",
            status="not_applicable",
        )
        client = StubClient(metric_entry(impact_factor="99"))
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "cache.json"
            journal_metrics.save_cache(cache_path, {"name:biorxiv": cached})
            before = cache_path.read_text(encoding="utf-8")
            records = journal_metrics.enrich_records(
                [{"journal": "bioRxiv"}], cache_path, client=client
            )
            after = cache_path.read_text(encoding="utf-8")

        self.assertEqual(client.calls, [])
        self.assertEqual(records[0]["journal_metrics_retrieved_at"], FIXED_TIME)
        self.assertEqual(before, after)

    def test_fetch_missing_false_is_cache_only(self):
        client = StubClient(metric_entry())
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "cache.json"
            records = journal_metrics.enrich_records(
                [{"journal": "Uncached Journal"}],
                cache_path,
                fetch_missing=False,
                client=client,
            )

        self.assertEqual(client.calls, [])
        self.assertEqual(records[0]["journal_metrics_status"], "cache_miss")

    def test_corrupt_cache_is_ignored_and_unexpected_fields_are_not_resaved(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "cache.json"
            cache_path.write_text("{not-json", encoding="utf-8")
            self.assertEqual(journal_metrics.load_cache(cache_path), {})

            entry = metric_entry()
            entry["secretKey"] = "must-not-persist"
            journal_metrics.save_cache(cache_path, {"name:example journal": entry})
            payload = json.loads(cache_path.read_text(encoding="utf-8"))

        saved = payload["journals"]["name:example journal"]
        self.assertEqual(set(saved), set(journal_metrics.CACHE_FIELDS))
        self.assertNotIn("secretKey", saved)


if __name__ == "__main__":
    unittest.main()
