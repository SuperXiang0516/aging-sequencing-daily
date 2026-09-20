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
        self.assertIn('"Illumina sequencing"[Title/Abstract]', query)
        self.assertNotIn('"Illumina"[Title/Abstract]', query)


class SequencingClassificationTests(unittest.TestCase):
    @staticmethod
    def _record(text, *, title="Sequencing in aging", keywords=None, mesh_terms=None):
        return {
            "title": title,
            "abstract": text,
            "mesh_terms": mesh_terms or [],
            "keywords": keywords or [],
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
        self.assertEqual(result["evidence_scope"], "methods")
        self.assertTrue(any(
            item["label"] == "Illumina"
            and item["source"] == "abstract"
            and item["strength"] == "strong"
            for item in result["generation_evidence"]
        ))

    def test_pacbio_is_third_generation(self):
        result = fetch_pubmed.classify_record(
            self._record(
                "PacBio HiFi long-read sequencing was used to study aging mouse tissues."
            )
        )

        self.assertEqual(result["sequencing_generation"], "三代/长读长")
        self.assertIn("PacBio", result["platforms"])
        self.assertEqual(result["evidence_scope"], "methods")

    def test_second_and_third_generation_evidence_is_hybrid(self):
        result = fetch_pubmed.classify_record(
            self._record(
                "Aging samples were sequenced with Illumina NovaSeq and Oxford Nanopore."
            )
        )

        self.assertEqual(result["sequencing_generation"], "二代+三代")
        self.assertIn("Illumina", result["platforms"])
        self.assertIn("Oxford Nanopore", result["platforms"])
        self.assertEqual(result["evidence_scope"], "methods")
        self.assertEqual(
            {item["label"] for item in result["generation_evidence"]},
            {"Illumina", "Oxford Nanopore"},
        )

    def test_scrna_seq_alone_does_not_imply_a_platform_generation(self):
        result = fetch_pubmed.classify_record(
            self._record(
                "Single-cell RNA sequencing (scRNA-seq) characterized cellular senescence."
            )
        )

        self.assertEqual(result["sequencing_generation"], "平台未报告")
        self.assertEqual(result["platforms"], [])
        self.assertIn("单细胞RNA测序", result["sequencing_assays"])
        self.assertEqual(result["evidence_scope"], "none")
        self.assertEqual(result["generation_evidence"], [])

    def test_illumina_epic_array_is_not_second_generation(self):
        result = fetch_pubmed.classify_record({
            "title": "Epigenetic age measured with methylation arrays",
            "abstract": (
                "Genome-wide DNA methylation was measured using Illumina "
                "Infinium MethylationEPIC BeadChip arrays."
            ),
            "mesh_terms": [],
            "keywords": [],
        })

        self.assertEqual(result["sequencing_generation"], "非测序/芯片")
        self.assertEqual(result["platforms"], [])
        self.assertEqual(result["evidence_scope"], "non_sequencing")
        self.assertTrue(any(
            item["source"] == "abstract" and item["strength"] == "strong"
            for item in result["generation_evidence"]
        ))
        self.assertNotIn("Illumina", result["classification_evidence"])

    def test_keyword_long_read_is_weak_topic_evidence_only(self):
        result = fetch_pubmed.classify_record(
            self._record(
                "This review discusses genomic approaches to biological aging.",
                keywords=["long-read sequencing"],
            )
        )

        self.assertEqual(result["sequencing_generation"], "平台未报告")
        self.assertEqual(result["platforms"], [])
        self.assertEqual(result["evidence_scope"], "topic")
        self.assertEqual(result["generation_evidence"], [{
            "label": "长读长平台未注明",
            "source": "keywords",
            "phrase": "long-read sequencing",
            "strength": "weak",
        }])
        self.assertIn("长读长平台未注明（主题证据）", result["classification_evidence"])

    def test_bare_illumina_name_is_not_platform_evidence(self):
        result = fetch_pubmed.classify_record(
            self._record("Samples were assessed using an Illumina assay.")
        )

        self.assertEqual(result["sequencing_generation"], "平台未报告")
        self.assertEqual(result["platforms"], [])
        self.assertEqual(result["evidence_scope"], "none")

    def test_platform_model_synonyms_are_recognized(self):
        second = fetch_pubmed.classify_record(
            self._record("Libraries were processed on an MGI DNBSEQ-T7 platform.")
        )
        third = fetch_pubmed.classify_record(
            self._record("Long molecules were analyzed on a PacBio Revio system.")
        )

        self.assertEqual(second["sequencing_generation"], "二代/短读长")
        self.assertIn("MGI/DNBSEQ", second["platforms"])
        self.assertEqual(third["sequencing_generation"], "三代/长读长")
        self.assertIn("PacBio", third["platforms"])

    def test_array_and_assay_without_sequencer_remains_unreported(self):
        result = fetch_pubmed.classify_record({
            "title": "Integrated methylation and transcriptome analysis in aging",
            "abstract": "We combined EPIC arrays with RNA-seq in aging samples.",
            "mesh_terms": [],
            "keywords": [],
        })

        self.assertEqual(result["sequencing_generation"], "平台未报告")
        self.assertEqual(result["evidence_scope"], "none")
        self.assertIn("RNA测序", result["sequencing_assays"])


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

    def test_only_records_from_an_old_classifier_are_queued_for_refresh(self):
        records = [
            {"pmid": "1", "classification_version": "sequencing-evidence-v1"},
            {
                "pmid": "2",
                "classification_version": fetch_pubmed.CLASSIFICATION_VERSION,
            },
            {"pmid": "3"},
            {"pmid": "1", "classification_version": "sequencing-evidence-v1"},
        ]
        with mock.patch.object(fetch_pubmed, "_load_public_records", return_value=records):
            self.assertEqual(fetch_pubmed.load_reclassification_pmids(), ["1", "3"])

    def test_classifier_refresh_bypasses_seen_set_and_reuses_ai_summary(self):
        previous = {
            "pmid": "1",
            "ai_status": "success",
            "ai_done": True,
            "summary_zh": "已生成的中文摘要。",
            "sequencing_generation": "二代/短读长",
            "platforms": ["Illumina"],
        }
        fresh = {
            "pmid": "1",
            "title": "EPIC methylation study",
            "abstract": "Illumina MethylationEPIC BeadChip arrays were used.",
            "classification_version": fetch_pubmed.CLASSIFICATION_VERSION,
            "sequencing_generation": "非测序/芯片",
            "platforms": [],
            "ai_status": "pending",
            "ai_done": False,
        }
        with (
            mock.patch.object(fetch_pubmed, "fetch_details", return_value=[fresh]),
            mock.patch.object(fetch_pubmed, "should_exclude_article", return_value=(False, "")),
            mock.patch.object(fetch_pubmed, "_load_public_records", return_value=[previous]),
            mock.patch.object(fetch_pubmed, "_save_daily") as save,
        ):
            accepted = fetch_pubmed._process_pmids(
                ["1"],
                "2026-09-20",
                {"1"},
                retry=True,
                refresh_classification=True,
            )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["sequencing_generation"], "非测序/芯片")
        self.assertEqual(accepted[0]["platforms"], [])
        self.assertEqual(accepted[0]["summary_zh"], "已生成的中文摘要。")
        self.assertEqual(accepted[0]["ai_status"], "success")
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
