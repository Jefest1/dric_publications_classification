This project determines whether University of Cape Coast publications explicitly acknowledge funding or support from the Directorate of Research, Innovation and Consultancy (DRIC). The workflow proceeds in two stages and produces, for each publication, a single classification of YES or NO, with NF recorded only when the content cannot be retrieved for assessment.

We begin by assembling the list of awardees for a given academic period and then collecting their publications from the UCC Scholar website using `scripts/fetch_ucc_scholar.py`. To maximise coverage, we search by multiple variants of each awardee’s name—such as the full name, first initial with last name, middle initials with last name, concatenated initials with last name, and a first-name plus initials pattern—so that differences in naming conventions do not cause us to miss results. With Playwright, we open the UCC Scholar site, apply the search, sort by year so that the newest items appear first, and scroll to load all relevant rows within the target period. For each hit we capture the authors as displayed on the site, the title, the year, and the Scholar link, and we write these to `Data/<period>/raw_publications.csv` after removing exact duplicates. We intentionally preserve author names exactly as they appear; beyond the temporary name variants used for searching, we do not normalise or reconcile names.

Next, we resolve and classify each record with `scripts/check_dric.py`. For every row, we open the Scholar record and attempt to resolve a usable full‑text link. We first try the main title link on the page and, if that is not suitable, fall back to a link found in the citation table. When a link points to the UCC Institutional Repository (`ir.ucc.edu.gh/xmlui/handle/...`), we open the repository item and extract the actual file link from the item’s file list, converting it to an absolute URL when necessary. We then fetch the page or file content through Firecrawl, using modest rate limiting and retries to be robust against transient errors. The retrieved text is passed to an LLM classifier with a strict prompt that permits only YES when there is an explicit funding or acknowledgement statement attributing support to DRIC of the University of Cape Coast; in all other cases the answer is NO. If we cannot obtain usable text at all, we record NF. The results are saved as `authors`, `title`, `year`, and `dric` to `Data/<period>/preprocessed_files/rsg_<period>_preprocessed.csv`.

When a run is interrupted—for example, due to third‑party API rate limits—we can resume from a specific position using a temporary convenience option in `scripts/check_dric.py`. By supplying `--start-row N`, we process the input from that row onward and overwrite the output from the same point, while preserving the earlier rows already written. This allows us to complete a period without reprocessing everything from the beginning.

There are a few constraints that we deliberately do not handle at this time. If a link leads only to an abstract with no accessible full text, if the navigation loops back to Google Scholar rather than a publisher or repository page, or if the content is paywalled or requires authentication, we do not attempt to bypass these barriers; in such cases, the outcome is typically NF because we cannot obtain sufficient text, or NO when some minimal text is available but contains no acknowledgement. Likewise, we do not perform OCR on scanned PDFs and we do not apply author name normalisation beyond the temporary search variants described above. These limitations keep the pipeline clear and predictable for managers while focusing the system on the core question: whether DRIC is explicitly acknowledged for funding or support.

### Results and Findings

This section summarises the current processed periods using the outputs you shared. Counts are the number of publications per period classified as YES, NO, or NF (not found). Percentages reflect the share of YES within each period.

#### Per‑period counts and YES share

| Period | YES | NO | NF | Total | YES% |
|---|---:|---:|---:|---:|---:|
| 2015-2016 | 1 | 11 | 3 | 15 | 6.7% |
| 2016-2017 | 0 | 49 | 19 | 68 | 0.0% |
| 2017-2018 | 0 | 18 | 11 | 29 | 0.0% |
| 2018-2019 | 3 | 111 | 28 | 142 | 2.1% |
| 2020 | 0 | 107 | 24 | 131 | 0.0% |
| 2021 | 6 | 132 | 24 | 162 | 3.7% |
| 2022 | 4 | 174 | 18 | 196 | 2.0% |
| 2023 | 15 | 237 | 39 | 291 | 5.2% |

#### Overall totals

| YES | NO | NF | Total | YES% |
|---:|---:|---:|---:|---:|
| 29 | 839 | 166 | 1034 | 2.8% |

#### Top authors with YES (real names only)

- 2018 (1 each): D Obiri‑Yeboah; E Obboh; F Pappoe; J Adu; P Nsiah; Y Asante Awuku
- 2019 (1 each): AH Benjamin; C Asiedu; D Obiri‑Yeboah; E Obboh; G Adjei; KA Pereko; NI Ebu; O Cudjoe; S Akaba
- 2021 (1 each): A Twum; B Kyei‑Asante; C Kyereme; D Obiri‑Yeboah; D Sakyi‑Arthur; DO Yawson; E Afutu; E Agyare; Ernest Obese
- 2022: EE Abano (2); then 1 each: DO Yawson; E Afutu; EA Ampofo; F Kumi; G Anyebuno; IT Commey; J Akanson; J Ampofo‑Asiama
- 2023: PA Asare (3); then 2 each: D Miezah; CLY Amuah; E Teye; E Arthur; DA Tuoyire; EA Agyare; F Kumi; PB Obour

Notes:
- The list above excludes placeholder author entries such as “...”.
- Counts reflect appearances across YES‑labelled publications; a single multi‑author paper contributes to multiple authors.

