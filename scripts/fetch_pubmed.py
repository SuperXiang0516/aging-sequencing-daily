#!/usr/bin/env python3
"""Fetch aging-and-sequencing papers from PubMed.

The search deliberately requires both an aging concept and a sequencing
concept. New records are stored in ``data/aging_daily`` so the upstream
metagenomics archive remains intact but is never mixed into this site.
"""

import argparse
import datetime
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DAILY_DIR = BASE_DIR / "data" / "aging_daily"
WEB_DATA_FILE = BASE_DIR / "web" / "data.json"
DATASET_ID = "aging-sequencing-v1"
SCHEMA_VERSION = 3
CLASSIFICATION_VERSION = "sequencing-evidence-v2"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


_load_env_file(BASE_DIR / "config.env")

NCBI_EMAIL = os.environ.get("NCBI_EMAIL", "").strip()
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()
MAX_RESULTS = min(9999, max(1, int(os.environ.get("PUBMED_MAX_RESULTS", "2000"))))
SEARCH_PAGE_SIZE = min(500, max(20, int(os.environ.get("PUBMED_PAGE_SIZE", "200"))))
DATE_FIELD = os.environ.get("PUBMED_DATE_FIELD", "crdt").strip() or "crdt"
if DATE_FIELD not in {"crdt", "pdat", "edat", "mdat"}:
    DATE_FIELD = "crdt"
CUSTOM_BASE_QUERY = os.environ.get("PUBMED_BASE_QUERY", "").strip()
REQUEST_DELAY = max(
    0.0,
    float(os.environ.get("NCBI_REQUEST_DELAY", "0.11" if NCBI_API_KEY else "0.34")),
)
NO_ABSTRACT_RETRY_DAYS = max(
    1, int(os.environ.get("NO_ABSTRACT_RETRY_DAYS", "30"))
)
RECLASSIFY_BATCH_SIZE = min(
    500, max(1, int(os.environ.get("RECLASSIFY_BATCH_SIZE", "100")))
)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


AGING_TERMS = (
    '"aging"[Title/Abstract]',
    '"ageing"[Title/Abstract]',
    '"cellular senescence"[Title/Abstract]',
    '"senescent cell"[Title/Abstract]',
    '"age-related"[Title/Abstract]',
    '"age-associated"[Title/Abstract]',
    '"senescence"[Title/Abstract]',
    '"biological age"[Title/Abstract]',
    '"epigenetic clock"[Title/Abstract]',
    '"inflammaging"[Title/Abstract]',
    '"geroscience"[Title/Abstract]',
    '"healthspan"[Title/Abstract]',
    '"longevity"[Title/Abstract]',
    '"lifespan"[Title/Abstract]',
    '"rejuvenation"[Title/Abstract]',
    '"centenarian"[Title/Abstract]',
    '"frailty"[Title/Abstract]',
    '"Aging"[Mesh]',
    '"Cellular Senescence"[Mesh]',
    '"Longevity"[Mesh]',
)

SEQUENCING_TERMS = (
    '"next-generation sequencing"[Title/Abstract]',
    '"high-throughput sequencing"[Title/Abstract]',
    '"massively parallel sequencing"[Title/Abstract]',
    '"short-read sequencing"[Title/Abstract]',
    '"long-read sequencing"[Title/Abstract]',
    '"third-generation sequencing"[Title/Abstract]',
    '"RNA-seq"[Title/Abstract]',
    '"RNA sequencing"[Title/Abstract]',
    '"single-cell RNA sequencing"[Title/Abstract]',
    '"single-nucleus RNA sequencing"[Title/Abstract]',
    '"single-cell transcriptomics"[Title/Abstract]',
    '"single-nucleus transcriptomics"[Title/Abstract]',
    '"scRNA-seq"[Title/Abstract]',
    '"snRNA-seq"[Title/Abstract]',
    '"ATAC-seq"[Title/Abstract]',
    '"ChIP-seq"[Title/Abstract]',
    '"whole genome sequencing"[Title/Abstract]',
    '"whole-genome sequencing"[Title/Abstract]',
    '"whole exome sequencing"[Title/Abstract]',
    '"whole-exome sequencing"[Title/Abstract]',
    '"bisulfite sequencing"[Title/Abstract]',
    '"methylome sequencing"[Title/Abstract]',
    '"spatial transcriptomics"[Title/Abstract]',
    '"Illumina sequencing"[Title/Abstract]',
    '"Illumina reads"[Title/Abstract]',
    '"NovaSeq"[Title/Abstract]',
    '"NextSeq"[Title/Abstract]',
    '"HiSeq"[Title/Abstract]',
    '"MiSeq"[Title/Abstract]',
    '"DNBSEQ"[Title/Abstract]',
    '"BGISEQ"[Title/Abstract]',
    '"Oxford Nanopore"[Title/Abstract]',
    '"nanopore sequencing"[Title/Abstract]',
    '"PacBio"[Title/Abstract]',
    '"Pacific Biosciences"[Title/Abstract]',
    '"SMRT sequencing"[Title/Abstract]',
    '"HiFi sequencing"[Title/Abstract]',
    '"Iso-Seq"[Title/Abstract]',
    '"direct RNA sequencing"[Title/Abstract]',
    '"High-Throughput Nucleotide Sequencing"[Mesh]',
)


PLATFORM_RULES = (
    # ``Illumina`` by itself is deliberately not sufficient evidence. The
    # company name also occurs in the names of EPIC/Infinium microarrays.
    (
        "Illumina",
        "二代/短读长",
        r"\b(?:novaseq|nextseq|hiseq|miseq|iseq|miniseq)(?:[ -]?[a-z0-9]+)*\b"
        r"|\b(?:illumina\s+)?(?:genome analyzer(?:\s+ii[x]?)?|gaiix|solexa)\b"
        r"|\billumina(?:[- ]based)?\s+(?:(?:paired|single)[ -]end\s+)?"
        r"(?:sequencing|reads?|platform|sequencer|instrument)\b"
        r"|\b(?:sequenc(?:ed|ing)|libraries?)\b[^.;]{0,60}"
        r"\b(?:on|using|with)\b[^.;]{0,25}\billumina\b",
    ),
    (
        "MGI/DNBSEQ",
        "二代/短读长",
        r"\b(?:dnbseq|bgiseq|mgiseq)(?:[ -]?[a-z0-9]+)*\b"
        r"|\bmgi(?:[- ]based)?\s+(?:sequencing|reads?|platform|sequencer)\b",
    ),
    (
        "Ion Torrent",
        "二代/短读长",
        r"\bion torrent\b|\bion (?:personal genome machine|pgm|proton|s5|genexus)\b"
        r"|\b(?:pgm|proton|s5|genexus)\s+(?:sequencer|platform|system)\b",
    ),
    (
        "SOLiD/454",
        "二代/短读长",
        r"\b(?:applied biosystems\s+)?solid(?:\s+\d+)?\s+"
        r"(?:sequencing|platform|system|sequencer)\b"
        r"|\b(?:roche\s+)?454\s+(?:pyro)?sequencing\b"
        r"|\b(?:roche\s+)?gs flx\b",
    ),
    (
        "Element/Ultima",
        "二代/短读长",
        r"\b(?:element biosciences\s+)?aviti\b|\bultima genomics\b|\bug\s*100\b",
    ),
    (
        "Oxford Nanopore",
        "三代/长读长",
        r"\boxford nanopore(?: technologies)?\b|\bnanopore (?:sequencing|reads?|data)\b"
        r"|\b(?:minion|gridion|promethion|flongle)\b"
        r"|\bont (?:sequencing|reads?|data|platform)\b",
    ),
    (
        "PacBio",
        "三代/长读长",
        r"\bpacbio\b|\bpacific biosciences\b"
        r"|\b(?:sequel(?:\s+ii)?|revio|rs ii)\s+(?:system|platform|sequencer)\b"
        r"|\b(?:smrt|single[ -]molecule real[ -]time) sequencing\b|\bsmrtbell\b"
        r"|\b(?:hifi|ccs) (?:sequencing|reads?|data)\b"
        r"|\bcircular consensus sequencing\b|\biso[ -]?seq\b",
    ),
    (
        "长读长平台未注明",
        "三代/长读长",
        r"\blong[ -]read sequencing\b|\blong reads? (?:were )?(?:generated|sequenced)\b"
        r"|\blong[ -]read data\b|\bthird[ -]generation sequencing\b"
        r"|\bdirect rna sequencing\b",
    ),
    (
        "短读长平台未注明",
        "二代/短读长",
        r"\bshort[ -]read sequencing\b|\bshort reads? (?:were )?(?:generated|sequenced)\b"
        r"|\bshort[ -]read data\b",
    ),
)

ARRAY_RULES = (
    (
        "Illumina EPIC/Infinium芯片",
        r"\b(?:illumina\s+)?(?:infinium\s+)?(?:methylation)?epic(?:\s+v?2(?:\.0)?)?\b"
        r"|\binfinium\b|\bhumanmethylation(?:450|850)k\b"
        r"|\b(?:450k|850k)\b|\bbeadchip\b",
    ),
    (
        "微阵列/芯片",
        r"\bmicroarrays?\b|\bmethylation arrays?\b|\barray[ -]based profiling\b",
    ),
)

ASSAY_RULES = (
    ("单细胞RNA测序", r"\bscrna[ -]?seq\b|\bsingle[ -]cell rna sequencing\b|\bsingle cell transcriptom"),
    ("单核RNA测序", r"\bsnrna[ -]?seq\b|\bsingle[ -]nucleus rna sequencing\b|\bsingle nucleus transcriptom"),
    ("RNA测序", r"\brna[ -]?seq\b|\brna sequencing\b|\btranscriptome sequencing\b"),
    ("ATAC-seq", r"\batac[ -]?seq\b"),
    ("ChIP-seq", r"\bchip[ -]?seq\b"),
    ("全基因组测序", r"\bwhole[ -]genome sequencing\b|\bwgs\b"),
    ("全外显子组测序", r"\bwhole[ -]exome sequencing\b|\bwes\b"),
    ("DNA甲基化测序", r"\bbisulfite sequencing\b|\bwgbs\b|\brrbs\b|\bmethylome sequencing\b"),
    ("空间转录组", r"\bspatial transcriptom"),
    ("Iso-Seq", r"\biso[ -]?seq\b"),
    ("直接RNA测序", r"\bdirect rna sequencing\b"),
    ("宏基因组测序", r"\bmetagenom(?:e|ic|ics)\b|\bshotgun metagenom"),
)

AGING_TOPIC_RULES = (
    ("细胞衰老", r"\bcellular senescence\b|\bsenescent cell"),
    ("长寿与寿命", r"\blongevity\b|\blifespan\b|\bhealthspan\b|\bcentenarian"),
    ("生物年龄与衰老时钟", r"\bbiological age\b|\bepigenetic clock\b|\baging clock\b|\bageing clock\b"),
    ("衰老干预与年轻化", r"\brejuvenat|\bgeroprotect|\bsenolytic|\bcaloric restriction\b|\banti[ -]aging"),
    ("免疫衰老与炎性衰老", r"\bimmunosenescence\b|\binflammaging\b"),
    ("虚弱与肌少症", r"\bfrailty\b|\bsarcopen"),
    ("年龄相关疾病", r"\bage[ -]related disease|\balzheimer|\bparkinson|\bneurodegener"),
    ("生理性衰老", r"\baging\b|\bageing\b|\baged\b|\bolder adult|\bold age\b"),
)

SPECIES_RULES = (
    ("人", r"\bhuman\b|\bpatients?\b|\bparticipants?\b|\bcohort\b|\bhomo sapiens\b"),
    ("小鼠", r"\bmice\b|\bmouse\b|\bmurine\b|\bmus musculus\b"),
    ("大鼠", r"\brats?\b|\brattus norvegicus\b"),
    ("线虫", r"\bc\.? elegans\b|\bcaenorhabditis elegans\b"),
    ("果蝇", r"\bdrosophila\b"),
    ("酵母", r"\bsaccharomyces\b|\byeast\b"),
    ("短命鱼", r"\bkillifish\b|\bnothobranchius furzeri\b"),
    ("非人灵长类", r"\bmacaque\b|\bnon[ -]human primate\b|\bmonkey\b"),
)

PLANT_ONLY_RE = re.compile(
    r"\barabidopsis\b|\bplant senescence\b|\bleaf senescence\b|\bcrop aging\b|\bseed aging\b|\bfruit ripening\b",
    re.IGNORECASE,
)
BIOMEDICAL_RE = re.compile(
    r"\bhuman\b|\bpatients?\b|\bmice\b|\bmouse\b|\bmurine\b|\brats?\b|\bdrosophila\b|\bc\.? elegans\b|\byeast\b|\bkillifish\b|\bmacaque\b|\bcell line\b",
    re.IGNORECASE,
)

MONTH_TO_NUM = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    "january": "01", "february": "02", "march": "03", "april": "04",
    "june": "06", "july": "07", "august": "08", "september": "09",
    "october": "10", "november": "11", "december": "12",
}


def build_topic_query() -> str:
    """Return the versioned, reviewable topic query."""
    if CUSTOM_BASE_QUERY:
        return CUSTOM_BASE_QUERY
    aging = " OR ".join(AGING_TERMS)
    sequencing = " OR ".join(SEQUENCING_TERMS)
    return "(({0}) AND ({1}))".format(aging, sequencing)


def build_query(start_date: str, end_date: str = None) -> str:
    """Build an incremental PubMed query using record creation date."""
    if end_date and end_date != start_date:
        date_clause = "{0}:{1}[{2}]".format(start_date, end_date, DATE_FIELD)
    else:
        date_clause = "{0}[{1}]".format(start_date, DATE_FIELD)
    return "{0} AND {1}".format(build_topic_query(), date_clause)


def _month_to_num(value: str) -> str:
    value = (value or "").strip().lower()
    if value.isdigit():
        return value.zfill(2)
    return MONTH_TO_NUM.get(value, "")


def _parse_date_node(node) -> str:
    if node is None:
        return ""
    year = (node.findtext("Year") or "").strip()
    month = _month_to_num(node.findtext("Month") or "")
    day = (node.findtext("Day") or "").strip().zfill(2)
    if not year:
        medline_date = (node.findtext("MedlineDate") or "").strip()
        match = re.search(r"\b(19|20)\d{2}\b", medline_date)
        year = match.group(0) if match else ""
    if year and month and day:
        return "{0}-{1}-{2}".format(year, month, day)
    if year and month:
        return "{0}-{1}".format(year, month)
    return year


def _get(url: str, params: dict, retries: int = 5, delay: float = 2.0) -> bytes:
    request_params = dict(params)
    if NCBI_API_KEY:
        request_params["api_key"] = NCBI_API_KEY
    request_params["tool"] = "aging-sequencing-daily"
    if NCBI_EMAIL:
        request_params["email"] = NCBI_EMAIL
    full_url = url + "?" + urllib.parse.urlencode(request_params)
    for attempt in range(retries):
        try:
            time.sleep(REQUEST_DELAY)
            with urllib.request.urlopen(full_url, timeout=60) as response:
                return response.read()
        except Exception as exc:
            if attempt == retries - 1:
                raise RuntimeError("无法访问 {0}: {1}".format(url, exc)) from exc
            wait = delay * (attempt + 1)
            log.warning("请求失败 (%s/%s): %s；%.1f 秒后重试", attempt + 1, retries, exc, wait)
            time.sleep(wait)
    return b""


def search_pmids(query: str) -> list:
    """Search all pages up to ``MAX_RESULTS`` instead of silently truncating."""
    pmids = []
    total = None
    retstart = 0
    while total is None or retstart < total:
        remaining = MAX_RESULTS - len(pmids)
        if remaining <= 0:
            break
        retmax = min(SEARCH_PAGE_SIZE, remaining)
        params = {
            "db": "pubmed",
            "term": query,
            "retstart": retstart,
            "retmax": retmax,
            "retmode": "json",
            "sort": "pub date",
        }
        raw = _get(ESEARCH_URL, params)
        data = json.loads(raw or b"{}")
        result = data.get("esearchresult", {})
        if total is None:
            total = int(result.get("count", 0))
            if total > MAX_RESULTS:
                raise RuntimeError(
                    "PubMed 查询命中 {0} 篇，超过 PUBMED_MAX_RESULTS={1}；"
                    "请缩小日期范围或谨慎调高上限".format(total, MAX_RESULTS)
                )
        page = result.get("idlist", [])
        if not page:
            break
        pmids.extend(str(item) for item in page)
        retstart += len(page)
    unique_pmids = list(dict.fromkeys(pmids))
    log.info("PubMed 查询命中 %s 篇，读取 %s 个 PMID", total or 0, len(unique_pmids))
    return unique_pmids


def fetch_details(pmids: list) -> list:
    if not pmids:
        return []
    records = []
    for offset in range(0, len(pmids), 100):
        batch = pmids[offset:offset + 100]
        raw = _get(EFETCH_URL, {
            "db": "pubmed",
            "id": ",".join(batch),
            "retmode": "xml",
            "rettype": "abstract",
        })
        records.extend(_parse_xml(raw))
    return records


def _parse_xml(raw: bytes) -> list:
    root = ET.fromstring(raw)
    records = []
    for article in root.findall(".//PubmedArticle"):
        try:
            records.append(_extract_article(article))
        except Exception as exc:
            log.warning("解析 PubMed 记录失败: %s", exc)
    return records


def _article_text(record: dict) -> str:
    values = [record.get("title", ""), record.get("abstract", "")]
    values.extend(record.get("mesh_terms") or [])
    values.extend(record.get("keywords") or [])
    return " ".join(str(value) for value in values if value).lower()


def _append_generation_evidence(
    evidence: list,
    seen: set,
    label: str,
    source: str,
    phrase: str,
    strength: str,
) -> None:
    """Add one normalized, de-duplicated piece of classification evidence."""
    phrase = re.sub(r"\s+", " ", phrase or "").strip()
    key = (label, source, phrase.casefold(), strength)
    if not phrase or key in seen:
        return
    seen.add(key)
    evidence.append({
        "label": label,
        "source": source,
        "phrase": phrase,
        "strength": strength,
    })


def _matched_rules(text: str, rules: tuple):
    """Yield the first exact phrase matching each rule in ``text``."""
    for rule in rules:
        match = re.search(rule[-1], text or "", re.IGNORECASE)
        if match:
            yield rule, match.group(0)


def classify_record(record: dict) -> dict:
    """Apply conservative, source-aware sequencing and aging tags.

    Only platform/method evidence in the title or abstract is allowed to set
    ``sequencing_generation``. Platform-like terms found solely in author
    keywords or MeSH headings are retained as weak topic evidence instead of
    being treated as proof that the study used that technology.
    """
    text = _article_text(record)
    method_sources = (
        ("title", str(record.get("title") or "")),
        ("abstract", str(record.get("abstract") or "")),
    )
    metadata_sources = (
        ("keywords", record.get("keywords") or []),
        ("mesh_terms", record.get("mesh_terms") or []),
    )

    platforms = []
    generations = set()
    generation_evidence = []
    evidence_seen = set()

    for source, source_text in method_sources:
        for (platform, generation, _), phrase in _matched_rules(source_text, PLATFORM_RULES):
            platforms.append(platform)
            generations.add(generation)
            _append_generation_evidence(
                generation_evidence,
                evidence_seen,
                platform,
                source,
                phrase,
                "strong",
            )

    # Metadata can describe a paper's topic without documenting its methods.
    # Keep it visible, but never let it change the generation or platform list.
    for source, values in metadata_sources:
        for value in values:
            value_text = str(value or "")
            for (platform, _, _), phrase in _matched_rules(value_text, PLATFORM_RULES):
                _append_generation_evidence(
                    generation_evidence,
                    evidence_seen,
                    platform,
                    source,
                    phrase,
                    "weak",
                )

    assays = [label for label, pattern in ASSAY_RULES if re.search(pattern, text, re.IGNORECASE)]
    method_text = " ".join(value for _, value in method_sources if value)
    method_assays = [
        label
        for label, pattern in ASSAY_RULES
        if re.search(pattern, method_text, re.IGNORECASE)
    ]
    topics = [label for label, pattern in AGING_TOPIC_RULES if re.search(pattern, text, re.IGNORECASE)]
    species = [label for label, pattern in SPECIES_RULES if re.search(pattern, text, re.IGNORECASE)]

    array_matches = []
    for source, source_text in method_sources:
        for (label, _), phrase in _matched_rules(source_text, ARRAY_RULES):
            array_matches.append((label, source, phrase))

    # Calling an EPIC/Infinium assay "sequencing" is a harmful false positive.
    # Use a non-sequencing state only when no sequencing method is reported in
    # the same title/abstract; mixed array + sequencing studies remain unknown
    # unless a real platform or read length is stated.
    mentions_sequencing_method = bool(
        generations
        or method_assays
        or re.search(r"\bsequenc(?:e|ed|er|ers|ing)\b", method_text, re.IGNORECASE)
    )
    array_only = bool(array_matches) and not mentions_sequencing_method

    if "二代/短读长" in generations and "三代/长读长" in generations:
        generation = "二代+三代"
    elif "三代/长读长" in generations:
        generation = "三代/长读长"
    elif "二代/短读长" in generations:
        generation = "二代/短读长"
    elif array_only:
        generation = "非测序/芯片"
        for label, source, phrase in array_matches:
            _append_generation_evidence(
                generation_evidence,
                evidence_seen,
                label,
                source,
                phrase,
                "strong",
            )
    else:
        generation = "平台未报告"

    if generations:
        evidence_scope = "methods"
    elif array_only:
        evidence_scope = "non_sequencing"
    elif any(item["strength"] == "weak" for item in generation_evidence):
        evidence_scope = "topic"
    else:
        evidence_scope = "none"

    score = 40
    if topics:
        score += 20
    if assays:
        score += 20
    if platforms:
        score += 20

    compatibility_evidence = []
    for item in generation_evidence:
        label = item["label"]
        if item["strength"] == "weak":
            label += "（主题证据）"
        compatibility_evidence.append(label)
    compatibility_evidence.extend(assays)
    compatibility_evidence.extend(topics)

    return {
        "classification_version": CLASSIFICATION_VERSION,
        "sequencing_generation": generation,
        "sequencing_assays": list(dict.fromkeys(assays)),
        "platforms": list(dict.fromkeys(platforms)),
        "aging_topics": list(dict.fromkeys(topics)) or ["衰老研究"],
        "species": list(dict.fromkeys(species)),
        "tissues": [],
        "relevance_score": min(score, 100),
        "generation_evidence": generation_evidence,
        "evidence_scope": evidence_scope,
        "classification_evidence": list(dict.fromkeys(compatibility_evidence)),
    }


def should_exclude_article(record: dict) -> tuple:
    """Exclude obvious plant-only senescence records from the biomedical site."""
    text = _article_text(record)
    if PLANT_ONLY_RE.search(text) and not BIOMEDICAL_RE.search(text):
        return True, "仅涉及植物衰老"
    return False, ""


def _get_text(element, path: str, default: str = "") -> str:
    node = element.find(path) if element is not None else None
    return (node.text or "").strip() if node is not None else default


def _extract_article(article) -> dict:
    medline = article.find("MedlineCitation")
    art = medline.find("Article")
    title_node = art.find("ArticleTitle")
    title = "".join(title_node.itertext()).strip() if title_node is not None else ""

    abstract_parts = []
    for node in art.findall(".//AbstractText"):
        text = "".join(node.itertext()).strip()
        label = node.get("Label") or ""
        if text:
            abstract_parts.append("{0}: {1}".format(label, text) if label else text)

    article_date = _parse_date_node(art.find(".//ArticleDate"))
    journal_date = _parse_date_node(art.find(".//Journal/JournalIssue/PubDate"))
    pub_date = article_date or journal_date

    created_date = ""
    for node in article.findall(".//PubMedPubDate"):
        if node.get("PubStatus") in ("entrez", "pubmed"):
            created_date = _parse_date_node(node)
            if created_date:
                break

    doi = ""
    for node in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if (node.get("IdType") or "").lower() == "doi":
            doi = (node.text or "").strip()
            break

    authors = []
    for author in art.findall(".//Author"):
        collective = _get_text(author, "CollectiveName")
        last_name = _get_text(author, "LastName")
        fore_name = _get_text(author, "ForeName")
        name = collective or " ".join(part for part in (last_name, fore_name) if part)
        if name:
            authors.append(name)
    author_text = "; ".join(authors[:8])
    if len(authors) > 8:
        author_text += " et al."

    pub_types = [(node.text or "").strip() for node in art.findall(".//PublicationType") if node.text]
    mesh_terms = [
        (node.text or "").strip()
        for node in medline.findall(".//MeshHeading/DescriptorName")
        if node.text
    ]
    keywords = [(node.text or "").strip() for node in medline.findall(".//Keyword") if node.text]

    record = {
        "schema_version": SCHEMA_VERSION,
        "tracking_domain": DATASET_ID,
        "source": "PubMed",
        "pmid": _get_text(medline, "PMID"),
        "doi": doi,
        "title": title,
        "title_zh": "",
        "journal": _get_text(art, "Journal/Title"),
        "journal_abbr": _get_text(medline, "MedlineJournalInfo/MedlineTA"),
        "issn": _get_text(art, "Journal/ISSN"),
        "issn_linking": _get_text(medline, "MedlineJournalInfo/ISSNLinking"),
        "pub_date": pub_date,
        "created_date": created_date,
        "authors": author_text,
        "abstract": "\n".join(abstract_parts),
        "pub_types": pub_types,
        "article_type": _classify_type(pub_types, title, " ".join(abstract_parts)),
        "mesh_terms": mesh_terms,
        "keywords": keywords,
        "summary_zh": "",
        "main_finding": "",
        "innovation": "",
        "limitation": "",
        "study_object": "",
        "study_design": "",
        "disease": "",
        "sample_size": "",
        "ai_status": "pending",
        "ai_error": "",
        "ai_attempts": 0,
        "ai_done": False,
    }
    record.update(classify_record(record))
    return record


def _classify_type(pub_types: list, title: str, abstract: str) -> str:
    publication_types = " ".join(pub_types).lower()
    text = "{0} {1}".format(title, abstract).lower()
    if "systematic review" in publication_types or "meta-analysis" in publication_types:
        return "系统综述/Meta分析"
    if "review" in publication_types:
        return "综述"
    if "clinical trial" in publication_types or "randomized" in publication_types:
        return "临床试验"
    if "case report" in publication_types or "case study" in publication_types:
        return "案例报告"
    if "benchmark" in text or ("comparison" in text and "tool" in text):
        return "Benchmark"
    if "journal article" in publication_types:
        return "研究论文"
    return "其他"


def _load_public_records() -> list:
    if not WEB_DATA_FILE.exists():
        return []
    try:
        with open(WEB_DATA_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [
        record for record in payload
        if isinstance(record, dict)
        and record.get("tracking_domain") == DATASET_ID
        and str(record.get("pmid") or "").strip()
    ]


def _no_abstract_retry_due(record: dict) -> bool:
    if record.get("ai_status") != "skipped_no_abstract":
        return False
    raw_date = str(record.get("fetch_date") or "")[:10]
    try:
        last_fetch = datetime.datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        return True
    return (datetime.date.today() - last_fetch).days >= NO_ABSTRACT_RETRY_DAYS


def load_existing_pmids() -> set:
    """Return records that need no PubMed refetch in this workspace."""
    seen = set()
    for path in DAILY_DIR.glob("*.json"):
        try:
            with open(path, encoding="utf-8") as handle:
                for record in json.load(handle):
                    if record.get("tracking_domain") == DATASET_ID and record.get("pmid"):
                        seen.add(str(record["pmid"]))
        except (OSError, ValueError, TypeError):
            continue
    for record in _load_public_records():
        status = record.get("ai_status")
        if (
            status in {"success", "failed_terminal"}
            or record.get("ai_done") is True
            or (status == "skipped_no_abstract" and not _no_abstract_retry_due(record))
        ):
            seen.add(str(record["pmid"]))
    return seen


def load_retry_pmids() -> list:
    """Return sanitized public records whose AI analysis should be retried."""
    return list(dict.fromkeys(
        str(record["pmid"])
        for record in _load_public_records()
        if record.get("ai_status") in {"pending", "error"}
        or _no_abstract_retry_due(record)
    ))


def load_reclassification_pmids() -> list:
    """Return public PMIDs produced by an older deterministic classifier."""
    return list(dict.fromkeys(
        str(record["pmid"])
        for record in _load_public_records()
        if record.get("classification_version") != CLASSIFICATION_VERSION
    ))


def _load_daily(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _merge_record(old: dict, fresh: dict) -> dict:
    """Refresh bibliographic tags without erasing a successful AI analysis."""
    ai_keys = {
        "title_zh", "summary_zh", "main_finding", "innovation", "limitation",
        "study_object", "study_design", "disease", "sample_size", "tissues",
        "ai_status", "ai_error", "ai_attempts", "ai_done", "ai_model",
        "ai_prompt_version", "ai_completed_at",
    }
    deterministic_classification_keys = {
        "classification_version", "sequencing_generation", "sequencing_assays",
        "platforms", "evidence_scope", "generation_evidence",
        "classification_evidence", "relevance_score",
    }
    result = dict(old)
    for key, value in fresh.items():
        # A newer deterministic classifier must also be able to remove stale
        # platform/assay evidence.  Empty lists are therefore meaningful here.
        if key in deterministic_classification_keys:
            result[key] = value
        elif key not in ai_keys and value not in (None, "", []):
            result[key] = value
    if old.get("ai_status") != "success" and not old.get("ai_done"):
        for key in ai_keys:
            if key in fresh:
                result[key] = fresh[key]
    result["schema_version"] = SCHEMA_VERSION
    result["tracking_domain"] = DATASET_ID
    return result


def _atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(str(temp_path), str(path))


def _save_daily(out_date: str, fresh_records: list) -> list:
    path = DAILY_DIR / "{0}.json".format(out_date)
    merged = {}
    for record in _load_daily(path):
        if record.get("pmid"):
            merged[str(record["pmid"])] = record
    for record in fresh_records:
        pmid = str(record.get("pmid", ""))
        if not pmid:
            continue
        merged[pmid] = _merge_record(merged[pmid], record) if pmid in merged else record
    records = sorted(merged.values(), key=lambda item: item.get("pub_date", "") or "", reverse=True)
    _atomic_write_json(path, records)
    log.info("保存 %s 篇记录至 %s", len(records), path)
    return records


def _process_pmids(pmids: list, out_date: str, existing: set,
                   manual: bool = False, retry: bool = False,
                   refresh_classification: bool = False) -> list:
    requested = list(dict.fromkeys(str(pmid) for pmid in pmids if str(pmid)))
    new_pmids = (
        requested
        if refresh_classification
        else [pmid for pmid in requested if pmid not in existing]
    )
    if not new_pmids:
        _save_daily(out_date, [])
        log.info("%s 无新增 PMID", out_date)
        return []

    public_by_pmid = {
        str(record["pmid"]): record for record in _load_public_records()
    }
    accepted = []
    for record in fetch_details(new_pmids):
        excluded, reason = should_exclude_article(record)
        if excluded and not manual:
            log.info("排除 PMID %s：%s", record.get("pmid"), reason)
            continue
        record["fetch_date"] = out_date
        record["manual_import"] = bool(manual)
        record["retry_fetch"] = bool(retry)
        record["classification_refresh"] = bool(refresh_classification)
        previous = public_by_pmid.get(str(record.get("pmid") or ""), {})
        if previous:
            try:
                record["ai_attempts"] = max(0, int(previous.get("ai_attempts", 0)))
            except (TypeError, ValueError):
                record["ai_attempts"] = 0
            record["ai_prompt_version"] = previous.get("ai_prompt_version", "")
            if refresh_classification:
                # Refresh deterministic evidence without paying to regenerate
                # a Chinese summary that already succeeded.
                record = _merge_record(previous, record)
        accepted.append(record)
        existing.add(str(record.get("pmid", "")))
    _save_daily(out_date, accepted)
    return accepted


def run(target_date: str = None, days_back: int = 1,
        start_date: str = None, end_date: str = None,
        pmids: list = None):
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    if not NCBI_EMAIL:
        log.warning("尚未配置 NCBI_EMAIL；正式自动运行前请在 GitHub Secrets 中添加")
    existing = load_existing_pmids()

    if pmids:
        out_date = target_date or datetime.date.today().strftime("%Y-%m-%d")
        return _process_pmids([str(item) for item in pmids], out_date, existing, manual=True)

    retry_date = target_date or datetime.date.today().strftime("%Y-%m-%d")
    refresh_pmids = load_reclassification_pmids()[:RECLASSIFY_BATCH_SIZE]
    refreshed = []
    if refresh_pmids:
        log.info(
            "重新获取 %s 篇旧版分类记录（分类规则 %s）",
            len(refresh_pmids), CLASSIFICATION_VERSION,
        )
        refreshed = _process_pmids(
            refresh_pmids,
            retry_date,
            existing,
            retry=True,
            refresh_classification=True,
        )

    retry_pmids = [pmid for pmid in load_retry_pmids() if pmid not in existing]
    retried = []
    if retry_pmids:
        log.info("重新获取 %s 篇 pending/error 论文，供 AI 重试", len(retry_pmids))
        retried = _process_pmids(retry_pmids, retry_date, existing, retry=True)

    if start_date:
        start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt = (
            datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
            if end_date else datetime.date.today()
        )
        if end_dt < start_dt:
            raise ValueError("end_date 不能早于 start_date")
        query = build_query(start_dt.strftime("%Y/%m/%d"), end_dt.strftime("%Y/%m/%d"))
        return refreshed + retried + _process_pmids(
            search_pmids(query), end_dt.strftime("%Y-%m-%d"), existing,
        )

    if not target_date:
        target_date = datetime.date.today().strftime("%Y-%m-%d")
    base_date = datetime.datetime.strptime(target_date, "%Y-%m-%d").date()
    all_new = list(refreshed) + list(retried)
    for offset in range(max(1, days_back)):
        day = base_date - datetime.timedelta(days=offset)
        search_date = day.strftime("%Y/%m/%d")
        out_date = day.strftime("%Y-%m-%d")
        log.info("按 PubMed 创建日期搜索 %s", out_date)
        all_new.extend(_process_pmids(search_pmids(build_query(search_date)), out_date, existing))
    new_count = sum(1 for record in all_new if not record.get("retry_fetch"))
    retry_count = len(all_new) - new_count
    log.info("本次新增 %s 篇，重新获取待处理论文 %s 篇", new_count, retry_count)
    return all_new


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取衰老与二代/三代测序相关 PubMed 文献")
    parser.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD")
    parser.add_argument("--days-back", type=int, default=1, help="向前重叠抓取天数")
    parser.add_argument("--start-date", default=None, help="范围起始 YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="范围结束 YYYY-MM-DD")
    parser.add_argument("--pmid", nargs="+", default=None, help="手动导入 PMID")
    args = parser.parse_args()
    run(args.date, args.days_back, args.start_date, args.end_date, args.pmid)


if __name__ == "__main__":
    main()
