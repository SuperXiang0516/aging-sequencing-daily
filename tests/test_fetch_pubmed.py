import json
import unittest
from unittest import mock

from scripts import fetch_pubmed


class PubMedQueryTests(unittest.TestCase):
    def test_query_requires_aging_and_sequencing_and_uses_creation_date(self):
        with (
            mock.patch.object(fetch_pubmed, "CUSTOM_BASE_QUERY", ""),
            mock.patch.object(fetch_pubmed, "DATE_FIELD", "crdt"),
        ):
            topic_query = fetch_pubmed.build_topic_query()
            query = fetch_pubmed.build_query("2026/09/18", "2026/09/20")

        expected_topic_query = "(({0}) AND ({1}))".format(
            " OR ".join(fetch_pubmed.AGING_TERMS),
            " OR ".join(fetch_pubmed.SEQUENCING_TERMS),
        )
        self.assertEqual(topic_query, expected_topic_query)
        self.assertEqual(
            query,
            expected_topic_query + " AND 2026/09/18:2026/09/20[crdt]",
        )
        self.assertIn('"aging"[Title/Abstract]', query)
        self.assertIn('"RNA-seq"[Title/Abstract]', query)


class SequencingClassificationTests(unittest.TestCase):
    @staticmethod
    def _record(text):
        return {
            "title": "Sequencing in aging",
            "abstract": text,
            "mesh_terms": [],
            "keywords": [],
        }

    def test_illumina_is_second_generation(self):
        result = fetch_pubmed.classify_record(
            self._record(
                "We profiled cellular aging by scRNA-seq on an Illumina NovaSeq instrument."
            )
        )

        self.assertEqual(result["sequencing_generation"], "二代/短读长")
        self.assertIn("Illumina", result["platforms"])
        self.assertIn("单细胞RNA测序", result["sequencing_assays"])

    def test_pacbio_is_third_generation(self):
        result = fetch_pubmed.classify_record(
            self._record(
                "PacBio HiFi long-read sequencing was used to study aging mouse tissues."
            )
        )

        self.assertEqual(result["sequencing_generation"], "三代/长读长")
        self.assertIn("PacBio", result["platforms"])

    def test_second_and_third_generation_evidence_is_hybrid(self):
        result = fetch_pubmed.classify_record(
            self._record(
                "Aging samples were sequenced with Illumina NovaSeq and Oxford Nanopore."
            )
        )

        self.assertEqual(result["sequencing_generation"], "二代+三代")
        self.assertIn("Illumina", result["platforms"])
        self.assertIn("Oxford Nanopore", result["platforms"])

    def test_scrna_seq_alone_does_not_imply_a_platform_generation(self):
        result = fetch_pubmed.classify_record(
            self._record(
                "Single-cell RNA sequencing (scRNA-seq) characterized cellular senescence."
            )
        )

        self.assertEqual(result["sequencing_generation"], "平台未报告")
        self.assertEqual(result["platforms"], [])
        self.assertIn("单细胞RNA测序", result["sequencing_assays"])


class PubMedPaginationTests(unittest.TestCase):
    def test_search_pmids_reads_all_pages_with_mocked_transport(self):
        pages = {
            0: ["101", "102"],
            2: ["103", "104"],
            4: ["105"],
        }
        calls = []

        def fake_get(url, params):
            calls.append((url, dict(params)))
            payload = {
                "esearchresult": {
                    "count": "5",
                    "idlist": pages[params["retstart"]],
                }
            }
            return json.dumps(payload).encode("utf-8")

        with (
            mock.patch.object(fetch_pubmed, "MAX_RESULTS", 5),
            mock.patch.object(fetch_pubmed, "SEARCH_PAGE_SIZE", 2),
            mock.patch.object(fetch_pubmed, "_get", side_effect=fake_get),
        ):
            result = fetch_pubmed.search_pmids("offline test query")

        self.assertEqual(result, ["101", "102", "103", "104", "105"])
        self.assertEqual([params["retstart"] for _, params in calls], [0, 2, 4])
        self.assertEqual([params["retmax"] for _, params in calls], [2, 2, 1])
        self.assertTrue(all(url == fetch_pubmed.ESEARCH_URL for url, _ in calls))
        self.assertTrue(all(params["term"] == "offline test query" for _, params in calls))

    def test_search_fails_instead_of_silently_truncating(self):
        payload = {"esearchresult": {"count": "6", "idlist": ["101", "102"]}}
        with (
            mock.patch.object(fetch_pubmed, "MAX_RESULTS", 5),
            mock.patch.object(fetch_pubmed, "SEARCH_PAGE_SIZE", 2),
            mock.patch.object(fetch_pubmed, "_get", return_value=json.dumps(payload).encode()),
        ):
            with self.assertRaisesRegex(RuntimeError, "PUBMED_MAX_RESULTS"):
                fetch_pubmed.search_pmids("too broad")


class PublicRetryTests(unittest.TestCase):
    def test_only_pending_and_error_public_records_are_refetched(self):
        records = [
            {"pmid": "1", "ai_status": "success"},
            {"pmid": "2", "ai_status": "pending"},
            {"pmid": "3", "ai_status": "error"},
            {
                "pmid": "4",
                "ai_status": "skipped_no_abstract",
                "fetch_date": fetch_pubmed.datetime.date.today().isoformat(),
            },
            {"pmid": "2", "ai_status": "pending"},
        ]
        with mock.patch.object(fetch_pubmed, "_load_public_records", return_value=records):
            self.assertEqual(fetch_pubmed.load_retry_pmids(), ["2", "3"])

    def test_no_abstract_record_is_rechecked_after_configured_interval(self):
        today = fetch_pubmed.datetime.date.today().isoformat()
        with mock.patch.object(fetch_pubmed, "NO_ABSTRACT_RETRY_DAYS", 30):
            self.assertFalse(fetch_pubmed._no_abstract_retry_due({
                "ai_status": "skipped_no_abstract", "fetch_date": today,
            }))
            self.assertTrue(fetch_pubmed._no_abstract_retry_due({
                "ai_status": "skipped_no_abstract", "fetch_date": "2000-01-01",
            }))


if __name__ == "__main__":
    unittest.main()
