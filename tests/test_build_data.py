import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_data


class BuildDataTests(unittest.TestCase):
    def _record(self, **overrides):
        record = {
            "schema_version": 2,
            "tracking_domain": build_data.DATASET_ID,
            "source": "PubMed",
            "pmid": "123",
            "title": "An aging sequencing study",
            "abstract": "Raw abstract must stay private to the build bundle.",
            "pub_date": "2026-09-01",
            "journal": "Example Journal",
            "sequencing_generation": "二代/短读长",
            "sequencing_assays": ["RNA测序"],
            "platforms": ["Illumina"],
            "aging_topics": ["细胞衰老"],
            "species": ["人"],
            "tissues": [],
            "classification_evidence": ["Illumina"],
            "ai_status": "pending",
            "ai_done": False,
        }
        record.update(overrides)
        return record

    def test_public_record_does_not_publish_pubmed_abstract(self):
        public = build_data._frontend_record(self._record())
        self.assertNotIn("abstract", public)
        self.assertEqual(public["pmid"], "123")

    def test_stats_cover_generation_assay_topic_species_and_status(self):
        stats = build_data.build_stats([self._record()])
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_generation"]["二代/短读长"], 1)
        self.assertEqual(stats["by_assay"]["RNA测序"], 1)
        self.assertEqual(stats["by_aging_topic"]["细胞衰老"], 1)
        self.assertEqual(stats["by_species"]["人"], 1)
        self.assertEqual(stats["by_ai_status"]["pending"], 1)

    def test_successful_ai_result_wins_when_duplicate_pmids_are_merged(self):
        successful = self._record(
            fetch_date="2026-09-01",
            title_zh="衰老测序研究",
            summary_zh="已验证的中文摘要。",
            ai_status="success",
            ai_done=True,
        )
        refreshed = self._record(
            fetch_date="2026-09-02",
            title="Updated bibliographic title",
            title_zh="",
            summary_zh="",
            ai_status="pending",
            ai_done=False,
        )
        merged = build_data._merge_group([successful, refreshed])
        self.assertEqual(merged["title"], "Updated bibliographic title")
        self.assertEqual(merged["summary_zh"], "已验证的中文摘要。")
        self.assertEqual(merged["ai_status"], "success")
        self.assertTrue(merged["ai_done"])

    def test_existing_sanitized_web_data_is_the_cumulative_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public_file = root / "data.json"
            daily_dir = root / "daily"
            daily_dir.mkdir()
            valid = build_data._frontend_record(self._record(
                pmid="900",
                summary_zh="可持续保留的中文摘要。",
                ai_status="success",
                ai_done=True,
            ))
            legacy = {"pmid": "old", "title": "Unrelated upstream record"}
            public_file.write_text(
                json.dumps([valid, legacy], ensure_ascii=False),
                encoding="utf-8",
            )
            original_out = build_data.OUT_FILE
            original_daily = build_data.DAILY_DIR
            try:
                build_data.OUT_FILE = public_file
                build_data.DAILY_DIR = daily_dir
                records = build_data.merge_all()
            finally:
                build_data.OUT_FILE = original_out
                build_data.DAILY_DIR = original_daily

        self.assertEqual([record["pmid"] for record in records], ["900"])
        self.assertNotIn("abstract", records[0])

    def test_corrupt_cumulative_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public_file = root / "data.json"
            stats_file = root / "stats.json"
            public_file.write_text("{not-json", encoding="utf-8")
            stats_file.write_text(json.dumps({
                "tracking_domain": build_data.DATASET_ID,
                "total": 12,
            }), encoding="utf-8")
            original_out = build_data.OUT_FILE
            original_stats = build_data.STATS_FILE
            try:
                build_data.OUT_FILE = public_file
                build_data.STATS_FILE = stats_file
                with self.assertRaisesRegex(RuntimeError, "停止构建"):
                    build_data._load_existing_public()
            finally:
                build_data.OUT_FILE = original_out
                build_data.STATS_FILE = original_stats


if __name__ == "__main__":
    unittest.main()
