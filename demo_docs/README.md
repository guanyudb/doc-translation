# Demo documents

Synthetic Japanese clinical documents for exercising the doc-translation
pipeline end-to-end without relying on any customer or proprietary content.

## Files

| File | Structure it mimics | Public template source |
|---|---|---|
| `ja_clinical/01_protocol_synopsis_ja.docx` | Clinical Trial Protocol Synopsis | ICH E6 (R2) — Good Clinical Practice, §6 |
| `ja_clinical/02_ae_summary_ja.docx`        | Periodic Safety Update Report (PSUR) | ICH E2C (R2) — Periodic Benefit-Risk Evaluation Report |
| `ja_clinical/03_ctd_efficacy_ja.docx`      | CTD Module 2.7.3 Clinical Efficacy Summary | ICH M4E (R2) — Common Technical Document |

All three ICH guidelines are public at https://database.ich.org and are
reproduced in Japan via PMDA at https://www.pmda.go.jp/int-activities/int-harmony/ich/.

## What's synthetic — nothing is real

- **Drug name** `DBX-101` is invented (Databricks + eXample).
- **Study codes** `DBX101-JP-001` / `-002` / `-GLB-003` are invented.
- **Company** `Databricks Pharma KK` is fictional.
- **Patient IDs** (`DBX-JP-014`, etc.) are invented.
- **All numerical data** (PFS/OS medians, HRs, AE counts, PK values, patient demographics)
  are made-up illustrative values with no source. Do not cite as reference.
- **Japanese medical terminology** used is generic (公知の医学用語) — no
  customer-derived vocabulary, glossary, or corrections.

The **structure** of each document (section headings, table columns, table
ordering, endpoint labels) follows the corresponding ICH template, which is
public.

## What the pipeline is designed to handle

Each doc is engineered to exercise the OOXML elements the in-place translator walks:

| Element | Coverage |
|---|---|
| `<w:p>` body paragraphs | ✅ translated |
| `<w:tbl>` / `<w:tc>` tables | ✅ translated (paragraphs inside cells) |
| `word/header*.xml` running headers | ✅ translated |
| `word/footer*.xml` running footers | ✅ translated |
| `<w:drawing>` embedded PNG figures | ⏭️ intentionally skipped (raster — pixel text isn't translatable) |
| `<w:hyperlink>` fields | ✅ preserved with English display text |

The single figure in each doc is a matplotlib-rendered PNG (PK profile /
AE frequency bar chart / subgroup forest plot). This is a **realistic mimic**
of clinical figures — real regulatory submissions typically paste charts
from Excel / GraphPad / SAS output as raster images, and those are correctly
untouched by the in-place translator.

## Regenerating

```bash
.venv/bin/python demo_docs/generate_ja_clinical.py
```

Idempotent — overwrites existing files in `demo_docs/ja_clinical/`.

## Using in a demo

Drop any of these three files into your deployment's
`raw_documents/` UC Volume folder. The file-arrival trigger picks them up
within ~60s and translates them into `translated_inplace/` preserving all
tables, styles, headers, footers, and page layout — with the figure passed
through unchanged.
