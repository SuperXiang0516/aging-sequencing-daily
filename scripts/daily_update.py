#!/usr/bin/env python3
"""Daily fetch -> summarize -> build pipeline.

PubMed record creation can lag behind publication. Each run therefore queries
an overlapping recent window and deduplicates by PMID instead of assuming that
a calendar day is permanently complete.
"""

import datetime
import logging
import os

import build_data
import fetch_pubmed
import summarize_papers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _lookback_days() -> int:
    try:
        return max(1, min(30, int(os.environ.get("DAILY_LOOKBACK_DAYS", "7"))))
    except ValueError:
        log.warning("DAILY_LOOKBACK_DAYS 无效，使用 7 天")
        return 7


def main() -> dict:
    today = datetime.date.today().strftime("%Y-%m-%d")
    lookback = _lookback_days()
    log.info("===== 每日更新 %s（重叠窗口 %s 天）=====", today, lookback)

    log.info("步骤 1/3：按 PubMed 创建日期抓取论文")
    new_records = fetch_pubmed.run(target_date=today, days_back=lookback)

    log.info("步骤 2/3：处理全部 pending/error 中文摘要")
    summary_stats = summarize_papers.run(all_files=True)

    log.info("步骤 3/3：构建静态网站数据")
    build_stats = build_data.run()

    result = {
        "new": sum(1 for record in new_records if not record.get("retry_fetch")),
        "summary": summary_stats,
        "total": build_stats.get("total", 0),
    }
    if summary_stats.get("error", 0):
        message = (
            "中文摘要存在未生成记录：成功 {0}，失败 {1}；"
            "记录已保留，后续运行会按上限重试"
        ).format(
            summary_stats.get("success", 0),
            summary_stats.get("error", 0),
        )
        log.warning(message)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("::warning title=部分中文摘要未生成::{0}".format(message))
    log.info(
        "===== 完成：新增 %s，AI 成功 %s/失败 %s，网站共 %s 篇 =====",
        result["new"],
        summary_stats.get("success", 0),
        summary_stats.get("error", 0),
        result["total"],
    )
    return result


if __name__ == "__main__":
    main()
