# POR 新能源汽车舆情知识库与 RAG 专项评测集

资料与来源复核日期为 **2026 年 11 月 25 日（北京时间）**。验收基准为 GitHub `main` 的 `2066fe21`。 [基准提交](https://github.com/kkaaakk/public_opinion_research/commit/2066fe21a097bdddd565d655bed591ff8a4edab7)

数据从 `3a2286f7` 进入 PR #68，本轮验收和评测基础设施沿用同一 PR。 [PR #68](https://github.com/kkaaakk/public_opinion_research/pull/68) · [原始数据提交](https://github.com/kkaaakk/public_opinion_research/commit/3a2286f79c8f9567694edb3525548b7c6e157320)

## 交付文件

| 路径 | 用途 |
| --- | --- |
| [knowledge/public_opinion](knowledge/public_opinion) | 162 份供检索的业务资料，按现有五类目录组织 |
| [metadata/documents.jsonl](metadata/documents.jsonl) | 162 条文档级清单记录，每行对应一份实体文件 |
| [benchmark/dev.jsonl](benchmark/dev.jsonl) | 30 道开发题 |
| [benchmark/test.jsonl](benchmark/test.jsonl) | 120 道测试题 |
| [scripts/validate_por_dataset.py](../scripts/validate_por_dataset.py) | 离线验证清单、文件、证据、划分和当前 Loader 的可重复检查 |

交付包括业务数据、清单、评测题、离线校验脚本和 RAG 检索评测入口。派生索引沿用项目原有目录与 Git 忽略规则。建议规模达到约 150 份的目标，分类分布偏向有编号、处置路径和实物范围可核对的历史案例。

## 文档新增与既有资料

本次新增 **159 份**，保留并核验重写 **3 份**，共 **162 份**。旧库正文没有逐字沿用。三个沿用的路径为 [中国动力蓄电池标准](knowledge/public_opinion/compliance/china_regulatory_landscape.md)、[Tesla 美国 Model 3 手册事实](knowledge/public_opinion/product_docs/tesla_battery_technical_facts.md)及 [Tesla 美国电池质保 FAQ](knowledge/public_opinion/faq/tesla_battery_customer_faq.md)。原稿里无法核对的企业合规结论、产品配置和售后承诺已去除，重写材料分别注明适用范围和可核验出处。

旧 `tesla_china_crisis_playbook.md`、`tesla_battery_crises_review.md` 和 `media_response_faq.json` 混有来源不明的企业口径、内部流程或复盘叙述，退出检索根目录。原目录说明由本报告承担，这样 Loader 只扫描业务资料。原稿可在 [基线提交的知识库目录](https://github.com/kkaaakk/public_opinion_research/tree/2066fe21a097bdddd565d655bed591ff8a4edab7/data/knowledge/public_opinion)复查。

### 业务分类

| 分类 | 公开 | 模拟 | 合计 |
| --- | ---: | ---: | ---: |
| `product_docs` 产品资料 | 21 | 13 | 34 |
| `historical_cases` 事件及历史案例 | 41 | 23 | 64 |
| `pr_playbooks` 舆情响应流程 | 4 | 16 | 20 |
| `compliance` 法规及合规文件 | 18 | 10 | 28 |
| `faq` 客服与媒体问答 | 5 | 11 | 16 |
| **合计** | **89（54.9%）** | **73（45.1%）** | **162** |

### 业务场景

同一资料可绑定多个场景，以下各行按标签计数。

| 场景 | 公开 | 模拟 | 合计 | 模拟事件 |
| --- | ---: | ---: | ---: | --- |
| 电池安全 | 50 | 8 | 58 | `LZ-BAT-2411` 东湾体验站驻车冒烟 |
| 智能驾驶争议 | 14 | 7 | 21 | `LZ-ADAS-2502` 引航状态短视频 |
| 产品质量与召回 | 55 | 7 | 62 | `LZ-RCL-2503` 尾门门锁线束 |
| 虚假宣传争议 | 29 | 7 | 36 | `LZ-ADS-2504` 泊青版续航海报 |
| 售后服务投诉 | 13 | 8 | 21 | `LZ-SVC-2505` 异响诊断计费 |
| 用户隐私泄露 | 5 | 8 | 13 | `LZ-PII-2506` 预约报表越权分享 |
| 产品交付延期 | 2 | 8 | 10 | `LZ-ETA-2507` 泊青版交付窗口 |
| 产品价格调整 | 7 | 7 | 14 | `LZ-PRC-2508` A17 订单价保 |
| 企业高管言论 | 2 | 7 | 9 | `LZ-EXE-2509` 区域访谈字幕 |
| 网络谣言与虚假信息 | 4 | 7 | 11 | `LZ-RUM-2510` 检测报告暂停传言 |

模拟企业为**砾舟智行**。三个车型系列分别为 SUV `LZ-A17`、轿车 `LZ-C42` 和多用途车 `LZ-V09`。`C42-LR-S` 是 C42 泊青长续航版配置。产品安全组处置风险事件，整车验证与驾驶辅助等工程组负责核验，法务合规组和数据保护办公室按事项审批，企业传播组、客户体验组、交付运营组分别处理对外发布、用户沟通和履约。事件码贯穿产品资料、案情、审批、响应和问答。同场景的版本或转传材料通过文档 ID 关联有效出处。73 份模拟资料均带 `synthetic` 标记。

### 文件格式

| 格式 | 公开 | 模拟 | 合计 |
| --- | ---: | ---: | ---: |
| Markdown | 32 | 42 | 74 |
| PDF | 23 | 6 | 29 |
| JSON | 34 | 10 | 44 |
| PNG | 0 | 9 | 9 |
| TXT | 0 | 6 | 6 |

原始 PDF 包括联邦公报、多列表格的调查报告、中文法规公报和带图形封面的官方召回指南。模拟包有两份整页栅格扫描 PDF、四份表格或工程 PDF、流程图和柱图。JSON 同时包含 EPA 原始记录、按活动归并的 NHTSA 车辆数组及模拟审批账本。TXT 用于逐时段校录及可核对的短文本线索。

## 公开资料来源与许可

- **中国监管与法规**。保存 11 份立法、规章或机关行政指导 PDF 原件。2025 年版汽车召回用户指南保留自撰事实摘要，图文版只链接原站。国家市场监督管理总局及召回中心的新闻、召回公告和标准目录以自撰的事实摘要收录，区分备案日期、范围、处置和适用地域。法规及行政性质官方文件留存依据见著作权法第五条。 [国家版权局《中华人民共和国著作权法》](https://www.ncac.gov.cn/xxfb/flfg/flfg_532/202103/t20210309_50530.html) · [缺陷汽车产品召回管理条例](https://xzfg.moj.gov.cn/front/law/detail?LawID=360) · [比亚迪秦 PLUS DM-i 召回](https://www.samr.gov.cn/zw/zh/art/2025/art_94d5b59d5f4c4420baae386c463c9717.html)
- **美国联邦资料**。保存 12 份 NHTSA、ODI、DOT OIG 或联邦公报原件。17 份 NHTSA API 记录按唯一活动号归并相同字段，车型年款仍保存在数组，接口字段原值保持。接口回报日期单独保存原串，发布日期无证据时填 `null`。官方开放数据目录给出政府作品使用依据。 [USA.gov 政府作品使用规则](https://www.usa.gov/government-works) · [Data.gov 召回数据许可](https://catalog.data.gov/dataset/recalls-data) · [NHTSA API 说明](https://www.nhtsa.gov/nhtsa-datasets-and-apis)
- **美国能效资料**。17 条 FuelEconomy.gov 车型 JSON 按 API 原始字节保存，字段单位以该站说明为准。Courtesy of FuelEconomy.gov, U.S. Department of Energy and U.S. Environmental Protection Agency. [FuelEconomy.gov 版权与署名](https://www.fueleconomy.gov/feg/disclaimer.shtml) · [车辆接口及字段](https://fueleconomy.gov/feg/ws/)
- **企业、执法和沟通资料**。Tesla、小米、FTC、California DMV 和 CDC 采用带出处的自撰事实摘要。企业手册、财报图片和授权不明确的图版原件留在上游。CDC 名称用于来源署名。 [Tesla 美国质保](https://www.tesla.com/support/vehicle-warranty) · [小米 2024 年一季度公告](https://ir.mi.com/system/files-encrypted/nasdaq_kms/assets/2024/05/23/5-38-14/24Q1_CN_Announcement.pdf) · [FTC GM/OnStar 案](https://www.ftc.gov/legal-library/browse/cases-proceedings/2423052-general-motors-llc-et-al-matter) · [CDC CERC 手册](https://www.cdc.gov/cerc/php/cerc-manual/index.html)
- **模拟资料与题目**。本项目原创模拟资料、评测题和校验脚本适用仓库的 [MIT 许可](../LICENSE)。公开材料的使用依据按清单 `license` 字段核对。

在数据构建阶段的在线复核中，原始候选清单的 24 份 PDF 与 17 条 EPA JSON 的源文件返回值与仓库 SHA-256 一致。另有 38 个入口通过直接请求复核，其中 17 个为召回 API、21 个为事实摘要来源。10 个事实摘要的入口在批量请求中遇到 403、404 或 TLS 错误，使用浏览检索返回的原站内容复核。FTC 命令、部分 NHTSA 报告及 CDC 章节采用事实摘要，保留来源。Fuel Economy Guide 原封面含摄影图，保留能效标签事实摘要及独立 API 数据。NHTSA 22V702000 原接口把通知日期写作 2032 年，该条退出清单。 [NHTSA 22V702000 API](https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=22V702000)

## 文档元数据

| 字段 | 含义 |
| --- | --- |
| `doc_id`、`title`、`organization` | 稳定文档 ID、标题、来源或编制组织。车型由相应原文确定 |
| `category`、`event_tags`、`source_group` | 业务目录、可多选场景、分组划分时使用的事件或来源族 |
| `file_path`、`file_type`、`language` | 相对于 `knowledge/public_opinion` 的 POSIX 路径、扩展格式、语种 |
| `data_origin`、`representation` | `public` 或 `synthetic`，以及 PDF 原件、API 原始记录、API 归并记录、事实整理、模拟原件的表示方式 |
| `source_url`、`published_at`、`accessed_at`、`license` | 主要来源、已核实的发布或文件日期、公开材料采集日期、使用依据与依据链接。未知信息用 `null` |
| `version`、`source_status` | 已知版本及 `authoritative`、`deprecated` 或 `misleading`。公开资料的权威范围限于文件发布主体、时点和管辖地。模拟资料的权威范围限于砾舟演练 |
| `series_id`、`effective_from`、`valid_until`、`superseded_by`、`conflicts_with` | 同制度版本、生效/截止时间、后继版本或已核实的冲突来源，有记录时填写 |
| `sha256` | 文件原始字节的 SHA-256。资料目录的 Git 属性维持跨平台检出后字节一致 |
| 其他按需字段 | `event_id`、`issuing_team`、`jurisdiction`、`page_count`、`scan_only`、`source_format`、`publication_url`、`model_year`、`report_received_date_raw`、`attribution`、`replaces_standard` |

## 专项评测集

| 问题类型 | dev | test | 合计 |
| --- | ---: | ---: | ---: |
| 基础事实 `single_hop` | 5 | 19 | 24 |
| 精确检索 `exact_match` | 4 | 16 | 20 |
| 语义检索 `semantic_match` | 3 | 17 | 20 |
| 跨文档 `multi_hop` | 7 | 24 | 31 |
| 版本与冲突 `version_conflict` | 3 | 10 | 13 |
| 表格及图片 `table_image` | 3 | 14 | 17 |
| 误导来源 `misleading` | 1 | 9 | 10 |
| 无答案 `unanswerable` | 4 | 11 | 15 |
| **合计** | **30** | **120** | **150** |

开发集使用砾舟售后计费、交付延期、Hyundai/Kia ICCU、Volkswagen ID.4 四个来源族。其余题目进入测试集。独立核对正证据、排除来源及无答案审核引用，开发集涉及 26 份文档、测试集涉及 104 份，两组交集为零。50 题基于公开资料，100 题基于模拟资料。135 道有答案题共设 240 条金标准证据。31 道多跳题分别要求跨文档合并时间与时限、事实与规则、事件与版本或多个独立记录。

`question_id`、`question`、`question_type`、`difficulty`、`answerable`、`expected_answer` 保持评测主结构。金标准 `gold_evidence` 绑定 `doc_id`，可通过 Markdown 标题路径、PDF 一基页码、JSON Pointer、TXT 一基行号或图像区域回到原件。`region` 的原点为左上角，使用 PDF 点或图像像素，已人工看图的区域用 `visual_check` 和裁切像素摘要记录。`excluded_doc_ids` 与 `exclusion_reasons` 用于旧版、未核实转传或跨辖区误引。`absence_check` 记录缺少的事实、全库排查词与最接近的资料。旧版文档作为历史事实证据时标注 `evidence_role: historical_record`。

### 代表性样例

| 题目 | 证据及判定 |
| --- | --- |
| `T004` 虚构砾舟东湾冒烟最迟何时启动应急响应？ | [S01B 时间线](knowledge/public_opinion/historical_cases/lz-bat-2411-incident-timeline.md)记录 09:19 确认，[S01C v2.0 预案](knowledge/public_opinion/pr_playbooks/lz-battery-emergency-response-v2.md)规定确认后 30 分钟内，答案 09:49 |
| `D006` 北岸店柜台扫描回执中的增项签名代码是什么？ | [S05F 扫描 PDF](knowledge/public_opinion/faq/lz-service-reception-form-scan.pdf)第 1 页指定单元格显示 `QF-218` |
| `T084` 美国 EA22002 分析附件中，正面撞击行驶路径上的车辆、物体或人员有多少起？ | [P002 原始分析 PDF](knowledge/public_opinion/historical_cases/nhtsa-ea22002-crash-analysis.pdf)第 3 页 Table 1 的总计列为 211 |
| `T100` 中国 2026 版汽车召回用户指南新增哪个板块？ | [C034 发布要点](knowledge/public_opinion/faq/samr-recall-guide-2026-release.md)为 2026 年版本证据，2025 版事实摘要 P027 作为范围外来源 |
| `T048` 能否把群聊声称的家庭住址和完整行程列为虚构砾舟已确认的泄露字段？ | S06D 审核记录和 S06A 字段字典确认预约别名、手机号后四位和城市代码，排除 S06H 未核实转传 |
| `T119` 理想 MEGA 本项召回到 2025 年末的实际完成比例是多少？ | C002 只有本项潜在涉及量，C014 只有行业年度汇总。整库缺少实际完成量，答案应说明证据不足 |

### 参考基准

- EnterpriseRAG-Bench 的企业多系统资料按共同业务背景组织。本集在一个虚构企业内按事件、车型和职责衔接案例、审批、操作记录和对外问答。 [EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench)
- Bank RAG Benchmark 的中文企业 PDF、难度、预期答案和黄金文档字段用于中文问题组织，重点沿用版本更新和相似材料干扰的设计思路。 [Bank RAG Benchmark](https://github.com/sjtu-suzhou-ai-zhiwei-tech/bank-rag-benchmark)
- FinanceBench 的文档与页内证据分开记录。本集采用一基页码和文件位置。MultiHop-RAG 启发需综合多个文档的问法。CRUD-RAG 启发中文业务材料中的信息更新与版本任务。 [FinanceBench](https://github.com/patronus-ai/financebench) · [MultiHop-RAG](https://github.com/yixuantt/MultiHop-RAG) · [CRUD-RAG](https://github.com/IAAR-Shanghai/CRUD_RAG)

## 数据质量校验

从仓库根目录执行下列命令。脚本有专门的 Loader 选项，运行文件枚举和纯文本读入。

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
.venv/Scripts/python.exe scripts/validate_por_dataset.py --check-loader
```

- 文件清单、路径一一对应、文件格式、SHA-256、唯一 ID、唯一引用文件名、公开来源、模拟标记、版本衔接和冲突链接通过校验。检查了相同哈希及规范化后的重复全文。
- 数据构建阶段曾渲染候选清单中 30 份 PDF 的 286 页。本轮独立看图与源数据复核范围见下文。
- 校验器逐条检查证据定位、图像裁切摘要及源文本，逐对检查 dev/test 的规范化问题近重复。每道无答案题的排查词、证据缺口和近邻资料保存在 `absence_check`。
- 数据构建阶段的 Windows 沙箱曾在分支和同期主线均遇到 `test_abrupt_process_kill_loses_only_unexported_memory` 的 `taskkill /T /F` 返回 1。该结果保留在 PR 的首轮记录中。 [PR #68](https://github.com/kkaaakk/public_opinion_research/pull/68)

## Loader 与评测器兼容性

- 当前 `config.yaml` 将检索根目录设在 `data/knowledge/public_opinion`。实际 Loader 枚举 162 份候选文件，与清单完全一致。纯文本运行成功读入 151 份文件，共产生 401 条记录。评测集、清单和本说明位于检索根目录之外，当前 Loader 的扩展名白名单也不包括 JSONL。
- Markdown、TXT、JSON 和含文本层的 PDF 可直接读入。PDF 按页产出记录，JSON 保有结构化路径。HTML 由代码 Loader 作为源文本读取。CSV、XLSX、DOCX 和 SVG 无原生入口，本批使用 PDF、PNG 和 JSON 表达表格与图。无文本层的 PDF 页与图片依赖 OCR 或视觉配置。含文本层的 PDF 页按现行逻辑读取文字，页内图片的图形关系需要独立评测。离线检查关闭了 OCR 与视觉，所以这些页面和图片的检索质量留待人工验收后测量。
- 当前生产 Loader 根据文件及页级数据工作，`documents.jsonl` 中的 `doc_id`、哈希、分组和来源状态由评测侧关联。合成旧版文本标题与误导文件名采用现有来源推断能识别的标记。原始法律文档和 API 记录仍以其原始证据内容为准，运行时状态可能与清单不同。

- 评测器用稳定文档 ID 和 `gold_evidence` 计算内部选中 Chunk 与最终下发引用的两套指标。旧别名仍供历史脚本读入。现行评测、记录查询及对比命令见实验页。 [RAG 评测与历史记录](../docs/evaluation/rag-retrieval-test-records.md)

## 独立验收记录

- **公共原件和数据接口**。抽取 15 份监管 PDF 与 6 条 EPA 车型记录重新请求原链接，21 份原件字节哈希一致。另比对 10 条 NHTSA 归并记录的保留字段与车型年款数组，均与接口相符。PDF 样本为 P001/P002/P004/P006/P012/P014/P015/P017/P018/P019/P022/P023/P031/P034/P035，EPA 样本为 E003–E006、E008、E009，NHTSA API 样本为 R001/R004/R005/R012–R015/R018/R019/R021。按高风险条款、车型/处置版本和两个题集的来源分层取样。 [NHTSA 召回数据](https://catalog.data.gov/dataset/recalls-data) · [EPA Vehicle API](https://fueleconomy.gov/feg/ws/) · [中国行政法规库](https://xzfg.moj.gov.cn/front/law/detail?LawID=360)
- **公共事实摘要**。复核比亚迪、理想、小米及 Tesla 中国召回（C001/C002/C004/C006）、GB 38031-2025（C018）、组合驾驶辅助/OTA 时间窗口（C019）、Tesla 美国手册、质保及接触器召回（C024–C026）、小米两代 SU7（C027/C028）、FTC 同意令及加州 DMV 决定（C015/C016）、CDC 风险沟通（C033）和两版中国召回用户指南（P027/C034）。C028 补齐官方配置表，并将 Max 版 835 公里与三款车的 19 英寸长续航胎配置绑定。C024/C027/C028/P023 补录可核验的手册、产品代际或法规版本。 [国家标准平台](https://std.samr.gov.cn/gb/search/gbDetailed?id=31D72E7FB9BF92EDE06397BE0A0AC2E0) · [小米汽车配置](https://www.xiaomiev.com/xiaomi/car-config) · [Tesla 美国质保](https://www.tesla.com/support/vehicle-warranty) · [FTC 案件档案](https://www.ftc.gov/legal-library/browse/cases-proceedings/2423052-general-motors-llc-et-al-matter)
- **模拟资料和评测题**。抽查覆盖五类业务目录、十个模拟事件、八种题型、三种难度和两个题集。按事件将产品及配置、责任组、时间线、标准有效版本、台账及对外问答对读。检查各高风险题的证据充分性，例如 T004 的确认时点与时限、D025 的两个 EPA 车型、T064 的原片校录与车辆配置、T101 的两个文件和跨页条款。复核 15 道无答案题的近邻资料和缺少的事实，例如潜在召回数量、快照和退款确认均无法推出实际完成量或实付金额。
- **图表与证据修订**。复看 9 张 PNG 和 6 个 PDF 页面的样例，覆盖两份扫描回执、模拟表格及美国监管表格。T014/T017 将时间与状态纳入同一行的裁框，T057/T077 将动作和责任方纳入同一节点，T084 将车型/物体/人员碰撞行与总数一起定位。D004 补入签名片段，D021 明确 VOQ 统计覆盖两个年款，D023 增加旧备案号，T101 补齐第十一条和两页出境定义，T108 补入轮胎配置。变动的证据裁切和文档 SHA-256 已重算。
- **许可抽查**。中国法规和行政文件按著作权法第五条核对，原始企业手册、财报及图片留在来源页。P027 旧版指南含独立美术图版，未核实图版再分发许可，因此保留同一 `doc_id`、历史版本和排除关系，将原 PDF 改为自撰的事实摘要。美国联邦报告与车辆数据分别核对政府作品和机构使用说明，FuelEconomy.gov 署名保留。 [国家版权局](https://www.ncac.gov.cn/xxfb/flfg/flfg_532/202103/t20210309_50530.html) · [2025 年版指南发布公告](https://www.samr.gov.cn/xw/zj/art/2025/art_724c75094b2f46c7a6706c3c143e98fb.html) · [USAGov](https://www.usa.gov/government-copyright) · [FuelEconomy.gov](https://www.fueleconomy.gov/feg/disclaimer.shtml)
- **集间隔离**。全量结构校验通过，开发集与测试集引用的正证据、排除来源及近邻来源无交集，来源族无交集。归一化问题跨集无重复，较高相似对复查后皆为不同事件或任务。两集本来使用同一知识库，评测题文件和答案均在检索根外。
- **复核范围**。书面和视觉验收采用分层抽样。其余公共条目和逐句许可沿用原构建来源记录。无答案只表示冻结语料缺少必要事实。扫描件与图片采用目视确认证据，实际 OCR/Vision 的召回、实时网站后续变化及远程模型行为未在本轮测量。

## 数据冻结

`dataset-v1` 包含 **162 份知识资料**（89 公开、73 模拟）、**150 道题**（dev 30、test 120）和 **240 条金标准证据**。全数据指纹为 `b0017ecb387d3ba9ef918e1f05096e9211fde3d7352211467aeeab86b910f82d`。指纹覆盖 162 份文档、文档清单和两个题集的实际字节。合并提交由带注释的 Git Tag 记录。 [dataset-v1](https://github.com/kkaaakk/public_opinion_research/tree/dataset-v1)

```powershell
.venv/Scripts/python.exe tests/evaluate_rag_retrieval.py dataset-fingerprint
```
