import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import summarize_papers


class NormalizeSummaryTests(unittest.TestCase):
    @staticmethod
    def _valid_model_result():
        return {
            "title_zh": "衰老组织的长读长测序研究",
            "summary_zh": "研究使用长读长测序分析衰老组织，并报告相关分子变化。",
            "main_finding": "发现与衰老相关的转录本变化。",
            "innovation": "结合长读长数据分析转录本。",
            "limitation": "摘要未报告独立验证。",
            "study_object": "衰老组织",
            "study_design": "观察性研究",
            "disease": "",
            "sample_size": "",
            "sequencing_generation": "二代/短读长",
            "sequencing_assays": ["RNA测序"],
            "platforms": ["Illumina"],
            "aging_topics": ["生理性衰老"],
            "species": ["小鼠"],
            "tissues": ["脑组织"],
            "relevance_score": 90,
            "classification_evidence": ["aging"],
        }

    def test_missing_required_chinese_summary_raises(self):
        model_result = self._valid_model_result()
        model_result.pop("summary_zh")

        with self.assertRaisesRegex(ValueError, "summary_zh 为空"):
            summarize_papers._normalize_result(model_result, {})

    def test_deterministic_generation_cannot_be_overridden_by_model(self):
        record = {
            "sequencing_generation": "三代/长读长",
            "sequencing_assays": ["Iso-Seq"],
            "platforms": ["PacBio"],
            "aging_topics": ["生理性衰老"],
            "species": ["小鼠"],
            "tissues": [],
            "classification_evidence": ["PacBio"],
            "relevance_score": 100,
        }
        model_result = self._valid_model_result()
        model_result["sequencing_generation"] = "二代/短读长"

        normalized = summarize_papers._normalize_result(model_result, record)

        self.assertEqual(normalized["sequencing_generation"], "三代/长读长")
        self.assertIn("PacBio", normalized["platforms"])

    def test_total_retry_cap_marks_record_terminal_without_calling_llm(self):
        record = {
            "pmid": "123",
            "title": "Aging sequencing study",
            "abstract": "A usable abstract.",
            "ai_status": "error",
            "ai_attempts": 2,
            "ai_done": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "records.json"
            path.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
            with (
                mock.patch.object(summarize_papers, "LLM_MAX_TOTAL_ATTEMPTS", 2),
                mock.patch.object(summarize_papers, "call_llm") as call,
            ):
                stats = summarize_papers.process_file(path)
            updated = json.loads(path.read_text(encoding="utf-8"))[0]

        call.assert_not_called()
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(updated["ai_status"], "failed_terminal")
        self.assertFalse(updated["ai_done"])

    def test_deepseek_payload_disables_thinking_and_requires_json(self):
        record = {
            "title": "Aging sequencing study",
            "abstract": "A usable abstract.",
        }
        with (
            mock.patch.object(
                summarize_papers,
                "LLM_API_URL",
                "https://api.deepseek.com/chat/completions",
            ),
            mock.patch.object(summarize_papers, "LLM_MODEL", "deepseek-flash"),
        ):
            payload = summarize_papers._build_request_payload(record)

        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_deepseek_endpoint_matching_does_not_accept_lookalike_host(self):
        self.assertTrue(summarize_papers._is_deepseek_endpoint(
            "https://api.deepseek.com/v1/chat/completions"
        ))
        self.assertFalse(summarize_papers._is_deepseek_endpoint(
            "https://api.deepseek.com.evil.example/chat/completions"
        ))
        self.assertFalse(summarize_papers._is_deepseek_endpoint(
            "https://gateway.deepseek.com/chat/completions"
        ))

    def test_other_provider_payload_omits_deepseek_extensions(self):
        with mock.patch.object(
            summarize_papers,
            "LLM_API_URL",
            "https://api.example.com/v1/chat/completions",
        ):
            payload = summarize_papers._build_request_payload({})

        self.assertNotIn("thinking", payload)
        self.assertNotIn("response_format", payload)

    def test_call_llm_sends_deepseek_extensions_at_request_root(self):
        model_result = self._valid_model_result()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "choices": [{
                "message": {"content": json.dumps(model_result, ensure_ascii=False)}
            }]
        }).encode("utf-8")
        captured = {}

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return response

        record = {
            "title": "Aging sequencing study",
            "abstract": "A usable abstract.",
        }
        with (
            mock.patch.object(
                summarize_papers,
                "LLM_API_URL",
                "https://api.deepseek.com/chat/completions",
            ),
            mock.patch.object(summarize_papers, "LLM_API_KEY", "test-key"),
            mock.patch.object(summarize_papers.urllib.request, "urlopen", fake_urlopen),
        ):
            result, error = summarize_papers.call_llm(record)

        self.assertEqual(error, "")
        self.assertEqual(result["summary_zh"], model_result["summary_zh"])
        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(
            captured["payload"]["response_format"],
            {"type": "json_object"},
        )
        self.assertTrue(all(
            "thinking" not in message
            for message in captured["payload"]["messages"]
        ))


if __name__ == "__main__":
    unittest.main()
