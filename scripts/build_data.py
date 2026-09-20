#!/usr/bin/env python3
"""Build the public, copyright-conscious dataset for the static website.

Only records produced by the aging-sequencing pipeline are accepted. Raw
PubMed abstracts remain in the ignored ``data/aging_daily`` working cache and
are never copied into the public web bundle. The sanitized web dataset is also
used as the cumulative index, so GitHub Actions need not commit raw abstracts.
"""

import csv
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DAILY_DIR = BASE_DIR / "data" / "aging_daily"
WEB_DIR = BASE_DIR / "web"
DATA_DIR = WEB_DIR / "data"
OUT_FILE = WEB_DIR / "data.json"
STATS_FILE = WEB_DIR / "stats.json"
TSV_PATH = BASE_DIR / "journal_info.tsv"

DATASET_ID = "aging-sequencing-v1"
SCHEMA_VERSION = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


FRONTEND_FIELDS = (
    "schema_version", "tracking_domain", "source", "pmid", "doi", "title",
    "title_zh", "journal", "journal_if", "journal_jcr", "journal_cas",
    "pub_date", "created_date", "authors", "article_type", "summary_zh",
    "main_finding", "innovation", "limitation", "study_object",
    "study_design", "disease", "sample_size", "sequencing_generation",
    "sequencing_assays", "platforms", "aging_topics", "species", "tissues",
    "relevance_score", "classification_evidence", "ai_status", "ai_done",
    "ai_attempts", "ai_prompt_version", "fetch_date",
)

LIST_FIELDS = {
    "sequencing_assays", "platforms", "aging_topics", "species", "tissues",
    "classification_evidence",
}

AI_FIELDS = {
    "title_zh", "summary_zh", "main_finding", "innovation", "limitation",
    "study_object", "study_design", "disease", "sample_size",
    "sequencing_generation", "sequencing_assays", "platforms",
    "aging_topics", "species", "tissues", "relevance_score",
    "classification_evidence", "ai_status", "ai_error", "ai_attempts",
    "ai_done", "ai_model", "ai_prompt_version", "ai_completed_at",
}


def _is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _unique_strings(value) -> list:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    ))


def _atomic_write(path: Path, value, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        if pretty:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        else:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(str(temporary), str(path))


def normalize_journal_name(name: str) -> str:
    """Normalize a PubMed journal title for best-effort TSV matching."""
    text = (name or "").strip().lower()
    if text.startswith("the "):
        text = text[4:]
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s*[:=]\s*.*$", "", text)
    text = re.sub(r"[^a-z0-9&/]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_journal_lookup() -> tuple:
    """Load optional IF/JCR/CAS annotations; absence never excludes a paper."""
    by_name = {}
    by_issn = {}
    if not TSV_PATH.exists():
        log.warning("未找到 %s；继续构建，不添加期刊分区", TSV_PATH)
        return by_name, by_issn

    with open(TSV_PATH, encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter="\t")
        next(reader, None)
        for row in reader:
            if len(row) < 7:
                continue
            info = {
                "if": row[1].strip(),
                "jcr": row[2].strip(),
                "cas": row[6].strip(),
            }
            raw_name = row[0].strip()
            normalized = normalize_journal_name(raw_name)
            if raw_name:
                by_name[raw_name.lower()] = info
            if normalized:
                by_name[normalized] = info
            for raw_issn in (row[4].strip(), row[5].strip()):
                if raw_issn and raw_issn.upper() != "N/A":
                    by_issn[raw_issn.replace("-", "").lower()] = info
    log.info("期刊注释表：%s 个名称，%s 个 ISSN", len(by_name), len(by_issn))
    return by_name, by_issn


def lookup_journal(record: dict, by_name: dict, by_issn: dict) -> dict:
    empty = {"if": "", "jcr": "", "cas": ""}
    issn = str(record.get("issn") or "").replace("-", "").lower()
    if issn and issn in by_issn:
        return by_issn[issn]
    name = str(record.get("journal") or "").strip()
    if not name:
        return empty
    return by_name.get(name.lower(), by_name.get(normalize_journal_name(name), empty))


def _merge_group(records: list) -> dict:
    """Merge duplicate PMIDs while keeping the best successful AI result."""
    ordered = sorted(
        records,
        key=lambda item: (
            str(item.get("fetch_date") or ""),
            str(item.get("created_date") or ""),
            str(item.get("pub_date") or ""),
        ),
    )
    merged = dict(ordered[-1])

    for record in ordered:
        for key, value in record.items():
            if key in LIST_FIELDS:
                merged[key] = list(dict.fromkeys(
                    _unique_strings(merged.get(key)) + _unique_strings(value)
                ))
            elif _is_empty(merged.get(key)) and not _is_empty(value):
                merged[key] = value

    successes = [
        record for record in ordered
        if record.get("ai_status") == "success" or record.get("ai_done") is True
    ]
    if successes:
        successful = successes[-1]
        for key in AI_FIELDS:
            if key in successful:
                merged[key] = successful[key]
        merged["ai_status"] = "success"
        merged["ai_done"] = True

    merged["schema_version"] = SCHEMA_VERSION
    merged["tracking_domain"] = DATASET_ID
    merged["source"] = merged.get("source") or "PubMed"
    return merged


def _expected_previous_total():
    """Return the last confirmed total for this dataset, if available."""
    if not STATS_FILE.exists():
        return None
    try:
        with open(STATS_FILE, encoding="utf-8") as handle:
            stats = json.load(handle)
        if not isinstance(stats, dict) or stats.get("tracking_domain") != DATASET_ID:
            return None
        return max(0, int(stats.get("total", 0)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _load_existing_public() -> list:
    """Load only this project's sanitized records from the prior web build."""
    expected_total = _expected_previous_total()
    if not OUT_FILE.exists():
        if expected_total:
            raise RuntimeError("累计网页数据缺失；为防止清空历史记录，已停止构建")
        return []
    try:
        with open(OUT_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("累计网页数据无法解析；为防止清空历史记录，已停止构建") from exc
    if not isinstance(payload, list):
        raise RuntimeError("累计网页数据顶层必须是数组；已停止构建")
    records = [
        record for record in payload
        if isinstance(record, dict)
        and record.get("tracking_domain") == DATASET_ID
        and str(record.get("pmid") or "").strip()
    ]
    if expected_total is not None and len(records) < expected_total:
        raise RuntimeError(
            "累计网页数据从 {0} 篇异常降至 {1} 篇；已停止构建".format(
                expected_total, len(records)
            )
        )
    return records


def merge_all() -> list:
    """Merge the sanitized cumulative index with the local raw working cache."""
    grouped = defaultdict(list)
    invalid_count = 0
    existing_public = _load_existing_public()
    for record in existing_public:
        grouped[str(record["pmid"])].append(record)

    files = sorted(DAILY_DIR.glob("*.json"))
    for path in files:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, list):
                raise ValueError("顶层不是数组")
            for record in payload:
                if not isinstance(record, dict):
                    invalid_count += 1
                    continue
                if record.get("tracking_domain") != DATASET_ID:
                    invalid_count += 1
                    continue
                pmid = str(record.get("pmid") or "").strip()
                if not pmid:
                    invalid_count += 1
                    continue
                grouped[pmid].append(record)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.warning("跳过无法读取的 %s：%s", path, exc)

    records = [_merge_group(group) for group in grouped.values()]
    records.sort(
        key=lambda record: (
            str(record.get("pub_date") or ""),
            str(record.get("created_date") or ""),
            str(record.get("pmid") or ""),
        ),
        reverse=True,
    )
    if invalid_count:
        log.warning("忽略 %s 条非本项目或无效记录", invalid_count)
    log.info(
        "读取已有网页 %s 篇和 %s 个本地缓存文件，合并为 %s 篇论文",
        len(existing_public), len(files), len(records),
    )
    return records


def _count_tags(records: list, field: str, fallback: str = "未标注") -> dict:
    counts = defaultdict(int)
    for record in records:
        values = record.get(field)
        if not isinstance(values, list):
            values = [values] if values else []
        clean = list(dict.fromkeys(
            str(value).strip() for value in values if str(value).strip()
        ))
        if not clean:
            clean = [fallback]
        for value in clean:
            counts[value] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _count_values(records: list, field: str, fallback: str = "未标注") -> dict:
    counts = defaultdict(int)
    for record in records:
        value = str(record.get(field) or fallback).strip() or fallback
        counts[value] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def build_stats(records: list) -> dict:
    years = defaultdict(int)
    journals = defaultdict(int)
    for record in records:
        pub_date = str(record.get("pub_date") or "")
        year_match = re.match(r"^(19|20)\d{2}", pub_date)
        years[year_match.group(0) if year_match else "未知"] += 1
        journal = str(record.get("journal") or "未知期刊").strip() or "未知期刊"
        journals[journal] += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "tracking_domain": DATASET_ID,
        "total": len(records),
        "by_generation": _count_values(records, "sequencing_generation", "平台未报告"),
        "by_assay": _count_tags(records, "sequencing_assays"),
        "by_aging_topic": _count_tags(records, "aging_topics"),
        "by_species": _count_tags(records, "species"),
        "by_ai_status": _count_values(records, "ai_status", "pending"),
        "by_year": dict(sorted(years.items(), key=lambda item: item[0], reverse=True)),
        "by_type": _count_values(records, "article_type", "其他"),
        "top_journals": dict(sorted(journals.items(), key=lambda item: (-item[1], item[0]))[:20]),
    }


def _frontend_record(record: dict) -> dict:
    public = {}
    for field in FRONTEND_FIELDS:
        if field in LIST_FIELDS:
            public[field] = _unique_strings(record.get(field))
            if field == "classification_evidence":
                public[field] = [item[:120] for item in public[field]][:12]
        elif field in {"schema_version", "relevance_score"}:
            try:
                public[field] = int(record.get(field) or 0)
            except (TypeError, ValueError):
                public[field] = 0
        elif field == "ai_done":
            public[field] = bool(record.get(field))
        else:
            public[field] = record.get(field, "")
    return public


def _year_for(record: dict) -> str:
    match = re.match(r"^((?:19|20)\d{2})", str(record.get("pub_date") or ""))
    return match.group(1) if match else "unknown"


def write_web_data(records: list) -> None:
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # These are generated files. Removing them prevents legacy years from the
    # upstream metagenomics site appearing in the new index.
    for stale in DATA_DIR.glob("*.json"):
        stale.unlink()

    public_records = [_frontend_record(record) for record in records]
    _atomic_write(OUT_FILE, public_records)

    by_year = defaultdict(list)
    for record in public_records:
        by_year[_year_for(record)].append(record)

    index = []
    for year in sorted(by_year, reverse=True):
        filename = "{0}.json".format(year)
        _atomic_write(DATA_DIR / filename, by_year[year])
        index.append({
            "year": "未知" if year == "unknown" else year,
            "count": len(by_year[year]),
            "file": "data/{0}".format(filename),
        })
        log.info("网页分片 %s：%s 篇", filename, len(by_year[year]))
    _atomic_write(DATA_DIR / "index.json", index, pretty=True)


def run() -> dict:
    records = merge_all()
    by_name, by_issn = load_journal_lookup()
    for record in records:
        journal = lookup_journal(record, by_name, by_issn)
        record["journal_if"] = journal["if"]
        record["journal_jcr"] = journal["jcr"]
        record["journal_cas"] = journal["cas"]

    write_web_data(records)
    stats = build_stats(records)
    _atomic_write(STATS_FILE, stats, pretty=True)
    log.info("构建完成：%s 篇；未按期刊指标删除任何相关论文", len(records))
    return stats


if __name__ == "__main__":
    run()
