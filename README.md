# 衰老测序文献雷达

一个面向衰老研究的自动化文献网站：每天从 PubMed 检索与二代/短读长、三代/长读长测序相关的新收录论文，生成中文结构化解读，并发布为静态网页。

本项目由 [Meta-SeuBiomed](https://github.com/yin-huamin/meta.seubiomed.com) 改造而来，沿用 MIT 许可证。

## 能做什么

- 使用“衰老概念 **AND** 测序概念”的分组查询检索 PubMed，而不是把所有关键词简单地用 `OR` 连接。
- 以 PubMed 创建日期为默认增量日期，降低晚收录论文被漏掉的概率。
- 对明确报告的平台区分“二代/短读长”“三代/长读长”“二代+三代”；摘要没有平台证据时标为“题目/摘要未明确平台”。
- 识别 RNA-seq、单细胞/单核 RNA-seq、ATAC-seq、ChIP-seq、WGS、WES、甲基化测序、空间转录组、Iso-Seq 和直接 RNA 测序等实验类型。
- 将 EPIC/Infinium、450K/850K、BeadChip 等纯芯片研究与测序分开，不再因为出现 `Illumina` 公司名而误判为二代测序。
- 可在后端用 easyScholar 为期刊补充 IF、JCR 分区和中科院分区，并把非敏感结果按期刊缓存。
- 从标题和摘要生成中文标题、研究设计、主要发现、创新点、局限性、物种、组织、样本量等结构化字段。
- 每天北京时间 **08:17** 自动更新，并由 GitHub Actions 发布到 GitHub Pages。

## 研究范围

默认查询聚焦以下交集：

- 衰老主题：自然衰老、细胞衰老、寿命与健康寿命、长寿、衰弱、炎症性衰老、生物学年龄、表观遗传时钟和年轻化干预等。
- 测序技术：Illumina、MGI/DNBSEQ/BGISEQ 等短读长平台，Oxford Nanopore、PacBio/SMRT/HiFi/Iso-Seq 等长读长平台，以及常见组学测序实验。
- 研究对象：不预先限定人或某一种模式生物；具体物种和组织依据摘要提取。
- 数据源：当前只使用 PubMed 的题录和摘要，不下载或镜像论文全文。

`RNA-seq`、`scRNA-seq` 等名称本身不能证明使用了二代平台。网站只有在题目或摘要出现明确平台、短读长或长读长证据时才判断测序代际。
作者关键词和 MeSH 中的长读长术语只标为“主题证据”，不代表论文实际使用了三代平台。纯芯片记录会从测序站点数据中排除。

## Fork 以后，本地和网页端分别做什么

GitHub Desktop 和 GitHub 网页端都需要，但用途不同：

| 位置 | 主要用途 |
| --- | --- |
| GitHub Desktop / 本地文件夹 | 查看修改、提交、推送、拉取自动生成的数据，以及本地预览网站 |
| GitHub 网页端 | 保存 API Secrets、启用 Actions、手动运行工作流、配置 GitHub Pages |

如果已经 Fork 并 Clone 成功，就不需要再次 Clone。GitHub Desktop 中显示的仓库如果位于同名的两层文件夹内，请以含有 `metaweb.py`、`scripts/` 和 `.github/` 的内层文件夹为项目根目录。

## 第一次上线：网页端操作

### 1. 添加 GitHub Actions Secrets

打开自己的仓库，进入：

`Settings → Secrets and variables → Actions → New repository secret`

逐项添加以下名称。Secret 名必须完全一致，值不要发到 Issue、聊天记录或提交到仓库。

| Secret 名 | 是否必需 | 填写内容 |
| --- | ---: | --- |
| `LLM_API_URL` | 必需 | OpenAI 兼容的 Chat Completions 地址，例如服务商提供的 `/v1/chat/completions` 地址 |
| `LLM_API_KEY` | 必需 | 你的 LLM API 密钥 |
| `LLM_MODEL` | 必需 | 服务商支持的模型名称 |
| `NCBI_EMAIL` | 必需 | 你自己的真实联系邮箱，供 NCBI 在请求异常时联系 |
| `NCBI_API_KEY` | 推荐 | NCBI 账户中申请的 API Key；不填也能运行，但请求额度较低 |
| `EASYSCHOLAR_SECRET_KEY` | 可选 | easyScholar 开放接口密钥；只在 GitHub Actions 后端使用，不会写入网页 |

不要创建 `SEARCH_KEYWORDS` Secret。项目已经内置正确分组的“衰老 AND 测序”查询；如需高级定制，请先在本地测试 `PUBMED_BASE_QUERY`。

easyScholar 是可选增强项。不配置时，网站仍会使用仓库现有的本地期刊表；配置后只查询尚未缓存、缺少指标的期刊。密钥不要发送到聊天、Issue 或提交记录。API 返回值不包含可核验的数据年份，因此网页只记录来源和查询日期，不会自行虚构年份。把第三方期刊指标展示在公开网站前，请自行确认账户条款与数据权利允许这种用途。

使用 DeepSeek 时填写：`LLM_API_URL=https://api.deepseek.com/chat/completions`，
`LLM_MODEL=deepseek-flash`。脚本会对 DeepSeek 的结构化抽取请求关闭思考模式并启用 JSON 输出，
避免思考内容耗尽输出额度后没有返回可解析的正文。

### 2. 允许工作流写入仓库

进入：

`Settings → Actions → General → Workflow permissions`

选择 **Read and write permissions** 并保存。每日工作流需要把不含原始摘要的网页 JSON 提交回 `main`。

Fork 的仓库可能默认停用 Actions。如果 Actions 页面出现启用提示，请确认这是自己的 Fork 后点击启用。

### 3. 启用 GitHub Pages

进入：

`Settings → Pages → Build and deployment → Source`

选择 **GitHub Actions**。不需要选择 `docs/` 文件夹，也不需要 `gh-pages` 分支。

### 4. 首次手动运行

进入：

`Actions → 每日更新与部署 → Run workflow`

选择 `main` 并运行。工作流会依次抓取文献、生成中文报告、构建网页数据、提交生成文件并部署网站。首次运行成功后，后续会在北京时间每天 08:17 自动执行；GitHub 的定时任务可能有几分钟排队延迟。

当前 Fork 的默认网站地址是：

<https://superxiang0516.github.io/aging-sequencing-daily/>

通用格式为：

```text
https://你的GitHub用户名.github.io/仓库名/
```

如以后修改用户名或仓库名，网站地址也会相应改变。自定义域名是可选项，不影响先使用免费 Pages 地址。

## 本地操作

本地只用于开发和预览；云端每日任务不依赖电脑开机。

### 环境

- Python 3.9 或更高版本，推荐 Python 3.11。
- 核心脚本只使用 Python 标准库，不需要安装额外依赖。

### 本地配置

在含有 `metaweb.py` 的仓库根目录执行：

```bash
cp config.env.example config.env
```

然后编辑 `config.env`，填写自己的值。该文件已被 `.gitignore` 忽略。请在提交前仍检查 GitHub Desktop 的变更列表，确保 `config.env` 没有出现。

本地 `config.env` 和 GitHub Secrets 是两套独立配置：前者供你的电脑使用，后者供 GitHub Actions 使用。本地配置不会自动上传到 GitHub。

### 运行与预览

```bash
# 完整执行最近 7 天：抓取、AI 摘要、构建网页数据
python3 metaweb.py auto --days 7

# 只执行每日更新逻辑
python3 metaweb.py daily

# 启动静态网站预览
python3 serve.py
```

浏览器打开 <http://localhost:8089>。

如果本地使用 Ollama，只有本地运行能访问 `localhost`。GitHub 托管的 Runner 无法连接你电脑上的 Ollama，因此云端自动更新需要可从互联网访问的 API 服务。

### 可选：回填历史文献

首次每日任务只追踪最近 7 个 PubMed 创建日。如果希望网站一开始就有历史数据，建议按年份在本地分批执行，便于控制 PubMed 返回量和 LLM 费用：

```bash
python3 metaweb.py auto --start-date 2025-01-01 --end-date 2025-12-31
python3 metaweb.py auto --start-date 2024-01-01 --end-date 2024-12-31
```

每完成一年先检查结果和 API 账单，再继续更早年份。如果任务提示命中数超过 `PUBMED_MAX_RESULTS`，它会停止而不会静默截断；请改为按月回填或谨慎调高上限。原始摘要只留在被忽略的 `data/aging_daily/`；可提交的结构化结果位于 `web/data.json`、`web/data/` 和 `web/stats.json`。确认无误后可在 GitHub Desktop 中提交并推送这些网页数据。

## 日常维护

- `.github/workflows/daily.yml`：北京时间每天 08:17 运行完整流水线，并直接部署 `web/`。
- `.github/workflows/pages.yml`：当有提交推送到 `main`，或在 Actions 页面手动触发时，单独重新发布 Pages。
- `data/aging_daily/` 是被 Git 忽略的本地/Runner 工作缓存；原始 PubMed 摘要不会提交到公开仓库，累计站点数据来自已脱敏的 `web/data.json`。
- `data/journal_metrics_cache.json` 仅保存 IF/JCR 等非敏感派生字段、来源和查询时间；不会保存 easyScholar 密钥或完整原始响应。
- GitHub Actions 每天可能自动向 `main` 增加一个数据提交。在本地开始修改前，先在 GitHub Desktop 点击 **Fetch origin**，有更新时再点击 **Pull origin**，可减少冲突。
- 如需立即刷新，使用 Actions 页面的 **Run workflow**，不必等待第二天。
- 定期检查 LLM 账单、Actions 运行记录和失败通知。API 调用费用由所选服务商收取。

## 可选配置

本地配置模板还提供以下参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_TIMEOUT` | `90` | 单次 LLM 请求超时秒数 |
| `LLM_DELAY` | `1.5` | 论文之间的调用间隔秒数 |
| `LLM_RETRIES` | `3` | 失败重试次数 |
| `LLM_MAX_TOTAL_ATTEMPTS` | `7` | 单篇论文跨运行的自动尝试上限，防止无限计费 |
| `PUBMED_DATE_FIELD` | `crdt` | PubMed 增量日期字段 |
| `PUBMED_MAX_RESULTS` | `2000` | 单次查询最多读取记录数 |
| `PUBMED_PAGE_SIZE` | `200` | PubMed 分页大小 |
| `NCBI_REQUEST_DELAY` | 无 Key 时 `0.34` | 相邻 NCBI 请求的最小等待秒数 |
| `PUBMED_BASE_QUERY` | 空 | 完整覆盖内置主题查询；仅建议熟悉 PubMed 语法的用户使用 |
| `DAILY_LOOKBACK_DAYS` | `7` | 每日重复检索的 PubMed 创建日期窗口（1–30 天） |
| `NO_ABSTRACT_RETRY_DAYS` | `30` | 无摘要记录重新向 PubMed 检查的间隔 |
| `RECLASSIFY_BATCH_SIZE` | `100` | 分类规则升级后，每次重新获取的旧记录数；复用已有中文摘要，不重复调用 LLM |
| `EASYSCHOLAR_REQUEST_DELAY` | `0.6` | easyScholar 相邻请求间隔秒数，保持低于官方每秒 2 次限制 |
| `EASYSCHOLAR_TIMEOUT` | `20` | easyScholar 单次请求超时秒数 |
| `EASYSCHOLAR_RETRIES` | `3` | easyScholar 瞬时失败的有界重试次数 |
| `EASYSCHOLAR_CACHE_DAYS` | `180` | 已匹配 IF/JCR 指标的缓存天数；到期后自动刷新 |
| `EASYSCHOLAR_NOT_FOUND_CACHE_DAYS` | `30` | 未匹配期刊的缓存天数；到期后自动重试 |

这些可选参数目前不需要配置为 GitHub Secrets；工作流会使用代码中的安全默认值。如果确需在云端覆盖，可先修改工作流的 `env`，不要把私密值直接写进 YAML。

## 项目结构

```text
.
├── .github/workflows/
│   ├── daily.yml            # 每日抓取、构建、提交和部署
│   └── pages.yml            # push main / 手动发布 Pages
├── data/aging_daily/        # 本地临时缓存（Git 忽略，不公开原始摘要）
├── data/journal_metrics_cache.json # 非敏感期刊指标缓存
├── scripts/
│   ├── fetch_pubmed.py      # PubMed 检索、分页、解析与初步分类
│   ├── summarize_papers.py  # 中文结构化摘要
│   ├── journal_metrics.py   # easyScholar 查询、校验与缓存
│   ├── daily_update.py      # 每日任务编排
│   └── build_data.py        # 生成前端 JSON
├── web/                     # GitHub Pages 发布目录
├── config.env.example       # 不含真实密钥的本地配置模板
├── metaweb.py               # 命令行入口
└── serve.py                 # 本地预览服务器
```

## 常见问题

### Actions 报“缺少 GitHub Actions Secret”

回到仓库的 Actions Secrets 页面，核对报错中的名称。Secret 名区分字符，不能多空格，也不要把 Secret 只填在本地 `config.env`。

### 网页显示“AI 中文解读尚未生成”

在 `Actions → 每日更新与部署` 中打开最近一次运行，展开“运行每日更新”。如果日志显示
`模型响应中没有 JSON 对象`，请确认使用当前 DeepSeek 配置，并确保仓库已包含对 DeepSeek
关闭思考模式、启用 JSON 输出的兼容代码。失败记录会在后续每日任务或手动运行时自动重试。

### 工作流抓取成功，但 `git push` 返回 403

检查 `Settings → Actions → General → Workflow permissions` 是否为 **Read and write permissions**。如 `main` 有分支保护，还需允许 GitHub Actions 写入，或调整保护规则。

### Pages 没有生成网址

确认 Pages 的 Source 已选择 **GitHub Actions**，再查看 `发布 GitHub Pages` 或 `每日更新与部署` 的 `deploy` 作业是否成功。

### GitHub Desktop 提示本地落后

这是每日工作流提交新数据后的正常现象。先 **Fetch origin**，再 **Pull origin**；不要用强制覆盖或重置来处理。

## 重要免责声明

- 网站中的中文内容由 AI 根据 PubMed 标题和摘要自动生成，可能存在遗漏、误译或分类错误。
- “创新点”“局限性”“测序代际”等字段不替代阅读原论文；平台未在题目/摘要中明确报告时，网站会显示“摘要未明确平台”，不会根据实验名称推测。
- 本站仅用于科研信息筛选，不构成医学建议、诊断、治疗建议、系统综述结论或临床决策依据。
- 收录不代表论文质量背书。发表状态、勘误和撤稿信息应以 PubMed、期刊和出版商页面为准。
- 本站不托管论文全文。题录、摘要和外部链接的权利归各自作者、数据库及出版商所有。

## 许可证与致谢

代码依照 [MIT License](LICENSE) 发布。改造和再发布时请保留原项目的许可证与版权声明。

感谢原项目 [yin-huamin/meta.seubiomed.com](https://github.com/yin-huamin/meta.seubiomed.com) 提供自动抓取、摘要和静态网站的基础实现。
