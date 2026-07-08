"""Generate synthetic Japanese clinical DOCX files for demoing the
doc-translation pipeline end to end.

All content is 100% synthetic:
  * Drug name "DBX-101" and study code "DBX101-JP-002" are invented.
  * Company "Databricks Pharma KK" is fictional.
  * Patient IDs, lab values, and adverse-event counts are made up.
  * Document structure follows publicly-published regulator templates:
      - ICH E6 (R2) — Good Clinical Practice (protocol synopsis structure)
      - ICH E2C (R2) — Periodic Safety Update (PSUR structure)
      - ICH M4E — Common Technical Document (CTD Module 2.7.3 structure)
    These ICH guidelines are public at database.ich.org.
  * Japanese clinical terminology is generic (公知の医学用語) — no
    customer-derived vocabulary.

Each document exercises the OOXML elements our translator walks:
  * Headings, paragraphs, tables — always translated
  * Header + footer running text                — always translated
  * Native Word tables (`<w:tbl>` → `<w:p>` inside `<w:tc>`)
  * Matplotlib-rendered PNG chart — INTENTIONALLY skipped by translator
    (raster; realistic mimic of clinical figures pasted from Excel/Prism)
"""
from __future__ import annotations
from pathlib import Path
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Cm, Inches, Pt, RGBColor


OUT_DIR = Path(__file__).parent / "ja_clinical"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Chart helpers — matplotlib PNGs embedded as figures
# ---------------------------------------------------------------------------

# Try a Japanese-capable font so axis labels don't render as tofu.
def _mpl_font():
    for name in ("Hiragino Sans", "Yu Gothic", "Noto Sans CJK JP", "IPAGothic", "MS Gothic"):
        for f in font_manager.fontManager.ttflist:
            if f.name == name:
                return name
    return None

_JP_FONT = _mpl_font()
if _JP_FONT:
    plt.rcParams["font.family"] = _JP_FONT


def _pk_profile_png() -> bytes:
    """Hypothetical single-dose PK profile for a fictional oral drug."""
    import numpy as np
    t = np.array([0, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24])
    dose_100 = np.array([0, 45, 120, 180, 210, 195, 165, 120, 85, 45, 22, 8])
    dose_200 = np.array([0, 88, 235, 355, 420, 385, 320, 240, 175, 90, 45, 18])

    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    ax.plot(t, dose_100, marker="o", label="100 mg")
    ax.plot(t, dose_200, marker="s", label="200 mg")
    ax.set_xlabel("投与後時間 (h)" if _JP_FONT else "Time after dose (h)")
    ax.set_ylabel("血漿中濃度 (ng/mL)" if _JP_FONT else "Plasma concentration (ng/mL)")
    ax.set_title("DBX-101 単回投与時 薬物動態プロファイル" if _JP_FONT else "DBX-101 single-dose PK profile")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=140); plt.close(fig)
    return buf.getvalue()


def _ae_bar_png() -> bytes:
    import numpy as np
    socs = ["消化器", "神経系", "皮膚", "肝胆道", "血液系", "全身状態"] if _JP_FONT \
           else ["GI", "Nervous", "Skin", "Hepatobiliary", "Blood", "General"]
    n_100 = [18, 12, 9, 4, 3, 8]
    n_200 = [26, 17, 13, 7, 5, 11]

    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    x = np.arange(len(socs)); w = 0.38
    ax.bar(x - w/2, n_100, w, label="100 mg (n=60)")
    ax.bar(x + w/2, n_200, w, label="200 mg (n=60)")
    ax.set_xticks(x); ax.set_xticklabels(socs, rotation=15)
    ax.set_ylabel("発現例数" if _JP_FONT else "Number of events")
    ax.set_title("器官別大分類 (SOC) 別 有害事象頻度" if _JP_FONT else "AE frequency by SOC")
    ax.legend(); ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=140); plt.close(fig)
    return buf.getvalue()


def _forest_png() -> bytes:
    """Fake subgroup forest plot for primary efficacy endpoint."""
    import numpy as np
    # Use full-width 以上/未満 in JP to sidestep the missing "≥" glyph in Hiragino Sans.
    subgroups = ["全体", "年齢 65歳未満", "年齢 65歳以上", "男性", "女性", "軽症", "重症"] \
        if _JP_FONT else ["Overall", "Age<65", "Age>=65", "Male", "Female", "Mild", "Severe"]
    hr = np.array([0.68, 0.62, 0.75, 0.71, 0.65, 0.58, 0.79])
    lo = np.array([0.55, 0.47, 0.58, 0.55, 0.49, 0.42, 0.60])
    hi = np.array([0.84, 0.83, 0.98, 0.92, 0.87, 0.80, 1.04])
    y  = np.arange(len(subgroups))[::-1]

    fig, ax = plt.subplots(figsize=(6.5, 3.8))
    ax.errorbar(hr, y, xerr=[hr-lo, hi-hr], fmt="s", capsize=4, color="tab:blue")
    ax.axvline(1.0, color="grey", linestyle="--", linewidth=0.8)
    ax.set_yticks(y); ax.set_yticklabels(subgroups)
    ax.set_xlabel("ハザード比 (95% CI)" if _JP_FONT else "Hazard ratio (95% CI)")
    ax.set_title("部分集団別 主要評価項目 (無増悪生存期間) ハザード比" if _JP_FONT
                 else "Primary endpoint HR by subgroup (PFS)")
    ax.set_xlim(0.3, 1.3); ax.grid(True, axis="x", alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=140); plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Header / footer helpers
# ---------------------------------------------------------------------------

def _add_header_footer(doc: Document, header_text: str, footer_text: str) -> None:
    """Wire a running header and footer with page numbering."""
    section = doc.sections[0]
    hdr = section.header.paragraphs[0]
    hdr.text = header_text
    hdr.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for run in hdr.runs:
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    ftr = section.footer.paragraphs[0]
    ftr.text = footer_text + "  |  ページ "
    ftr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in ftr.runs:
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    # Add a PAGE field so page numbers render live
    run = ftr.add_run()
    run.font.size = Pt(9)
    fld = OxmlElement("w:fldSimple"); fld.set(qn("w:instr"), "PAGE")
    r = OxmlElement("w:r"); t = OxmlElement("w:t"); t.text = "1"; r.append(t); fld.append(r)
    run._r.append(fld)


def _shade_row(row, hex_color: str) -> None:
    for cell in row.cells:
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), hex_color)
        tc_pr.append(shd)


def _fill_table(doc: Document, headers: list[str], rows: list[list[str]]) -> None:
    tbl = doc.add_table(rows=1 + len(rows), cols=len(headers))
    tbl.style = "Light Grid Accent 1"
    for i, h in enumerate(headers):
        c = tbl.rows[0].cells[i]
        c.text = h
        for p in c.paragraphs:
            for r in p.runs:
                r.bold = True
    _shade_row(tbl.rows[0], "D9E2F3")
    for r_idx, row in enumerate(rows, start=1):
        for c_idx, val in enumerate(row):
            tbl.rows[r_idx].cells[c_idx].text = str(val)
    for row in tbl.rows:
        for cell in row.cells:
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            for p in cell.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(10)


def _h(doc: Document, text: str, level: int = 1) -> None:
    doc.add_heading(text, level=level)


def _p(doc: Document, text: str) -> None:
    para = doc.add_paragraph(text)
    for r in para.runs:
        r.font.size = Pt(11)


def _fig(doc: Document, png_bytes: bytes, caption: str) -> None:
    doc.add_picture(io.BytesIO(png_bytes), width=Inches(6.0))
    para = doc.add_paragraph(caption)
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in para.runs:
        r.italic = True; r.font.size = Pt(9); r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)


# ---------------------------------------------------------------------------
# Doc 1 — Clinical Trial Protocol Synopsis (JA)
# Structure: ICH E6 (R2) §6 — Protocol and Protocol Amendments
# ---------------------------------------------------------------------------

def build_protocol_synopsis() -> Path:
    doc = Document()
    _add_header_footer(
        doc,
        header_text="治験実施計画書 概要  |  DBX101-JP-002  |  版 2.0 (2026年3月15日)",
        footer_text="Databricks Pharma KK  秘密扱い",
    )

    _h(doc, "治験実施計画書 概要", 0)
    _p(doc, "本書は、DBX-101 (開発コード) を対象とする第II相無作為化二重盲検比較試験の治験実施計画書の概要である。"
             "本試験は、日本国内10施設で実施される多施設共同試験である。")

    _h(doc, "1. 試験の背景と根拠", 1)
    _p(doc, "本疾患領域における現行の標準治療は奏効率が不十分であり、無増悪生存期間の中央値は約6.2か月に留まっている。"
             "DBX-101は新規経路を標的とする小分子化合物であり、非臨床試験において用量依存的な腫瘍増殖抑制作用が確認されている。")
    _p(doc, "本試験は、標準治療に不応または不耐となった患者を対象に、DBX-101の有効性と安全性を評価することを目的とする。"
             "本試験の結果は、後続の第III相試験の設計に用いられる。")

    _h(doc, "2. 主要評価項目および副次評価項目", 1)
    _p(doc, "主要評価項目は無増悪生存期間 (PFS) とし、独立中央判定 (BICR) にて評価する。"
             "副次評価項目は、全生存期間 (OS)、客観的奏効率 (ORR)、および有害事象の発現頻度とする。")

    _h(doc, "3. 用法・用量および投与スケジュール", 1)
    _fill_table(doc,
        ["コホート", "用量", "投与経路", "投与頻度", "投与期間"],
        [
            ["低用量群",    "100 mg", "経口", "1日1回", "疾患進行または投与中止まで"],
            ["高用量群",    "200 mg", "経口", "1日1回", "疾患進行または投与中止まで"],
            ["対照群",      "標準治療", "指定なし", "指定なし", "疾患進行または投与中止まで"],
        ])
    doc.add_paragraph()

    _h(doc, "4. 対象患者と選択・除外基準", 1)
    _p(doc, "選択基準: 20歳以上の成人、Eastern Cooperative Oncology Group (ECOG) パフォーマンスステータス 0-1、"
             "組織学的または細胞学的に確定診断された対象疾患を有すること、および測定可能病変を有すること。")
    _p(doc, "除外基準: 妊娠中または授乳中の女性、活動性の二次悪性腫瘍を有する患者、"
             "コントロール不良な心血管疾患を有する患者、および試験薬剤の成分に対する既知の過敏症を有する患者。")

    _h(doc, "5. 患者背景 (想定)", 1)
    _fill_table(doc,
        ["項目", "低用量群 (n=60)", "高用量群 (n=60)", "対照群 (n=60)"],
        [
            ["年齢 中央値 (範囲)", "62 (24-78)", "64 (28-79)", "63 (26-80)"],
            ["男性 n (%)",         "34 (56.7)",  "36 (60.0)",  "33 (55.0)"],
            ["ECOG 0",             "38 (63.3)",  "36 (60.0)",  "37 (61.7)"],
            ["ECOG 1",             "22 (36.7)",  "24 (40.0)",  "23 (38.3)"],
            ["前治療歴 中央値",     "2 (1-4)",    "2 (1-5)",    "2 (1-4)"],
        ])
    doc.add_paragraph()

    _h(doc, "6. 薬物動態評価", 1)
    _p(doc, "各コホートで単回投与後および反復投与後の血漿中濃度を測定し、"
             "最高血漿中濃度 (Cmax)、血漿中濃度-時間曲線下面積 (AUC)、消失半減期 (t1/2) を算出する。"
             "下図に単回投与時の想定プロファイルを示す。")
    _fig(doc, _pk_profile_png(),
         "図1. DBX-101 単回経口投与時の血漿中濃度推移 (想定値、模擬データ)")

    _h(doc, "7. 統計解析計画", 1)
    _p(doc, "主要解析はintention-to-treat集団を対象とし、Cox比例ハザードモデルを用いて群間のハザード比と95%信頼区間を推定する。"
             "検出力を80%とし、片側有意水準を0.025として症例数を設計している。")

    _h(doc, "8. 参考規制ガイドライン", 1)
    _p(doc, "本試験は、ICH E6 (R2) Good Clinical Practice ガイドラインおよび"
             "医薬品、医療機器等の品質、有効性及び安全性の確保等に関する法律 (医薬品医療機器等法) を遵守して実施される。")

    out = OUT_DIR / "01_protocol_synopsis_ja.docx"
    doc.save(out)
    return out


# ---------------------------------------------------------------------------
# Doc 2 — Adverse Event Summary (JA)
# Structure: ICH E2C (R2) — Periodic Benefit-Risk Evaluation Report
# ---------------------------------------------------------------------------

def build_ae_summary() -> Path:
    doc = Document()
    _add_header_footer(
        doc,
        header_text="定期的安全性最新報告書 概要  |  DBX-101  |  報告期間 2025年10月1日~2026年3月31日",
        footer_text="Databricks Pharma KK  安全性管理部",
    )

    _h(doc, "定期的安全性最新報告書 (PSUR) 概要", 0)
    _p(doc, "本書は、DBX-101 (開発コード) について、報告期間中に収集された安全性情報を要約したものである。"
             "本要約は、ICH E2C (R2) ガイドラインに準拠して作成されている。")

    _h(doc, "1. 報告期間中の総合安全性評価", 1)
    _p(doc, "報告期間中に、120例に対してDBX-101が投与された (低用量群60例、高用量群60例)。"
             "全体として、DBX-101の安全性プロファイルは前回報告書から大きな変化はなく、"
             "新たなシグナルは検出されていない。")
    _p(doc, "最も頻度の高い有害事象は消化器症状 (悪心、下痢) であり、いずれも軽度から中等度であった。"
             "重篤な有害事象の発現率は、両投与群間で有意な差は認められなかった。")

    _h(doc, "2. 器官別大分類 (SOC) 別 有害事象一覧", 1)
    _fill_table(doc,
        ["器官別大分類 (SOC)", "低用量群 n (%)", "高用量群 n (%)", "重篤例 n"],
        [
            ["胃腸障害",              "18 (30.0)", "26 (43.3)", "2"],
            ["神経系障害",            "12 (20.0)", "17 (28.3)", "0"],
            ["皮膚および皮下組織障害",  "9 (15.0)",  "13 (21.7)", "0"],
            ["肝胆道系障害",          "4 (6.7)",   "7 (11.7)",  "1"],
            ["血液およびリンパ系障害",  "3 (5.0)",   "5 (8.3)",   "1"],
            ["一般・全身障害および投与部位状態", "8 (13.3)", "11 (18.3)", "0"],
        ])
    doc.add_paragraph()

    _fig(doc, _ae_bar_png(),
         "図2. 器官別大分類 (SOC) 別 有害事象発現頻度 (両投与群比較、模擬データ)")

    _h(doc, "3. 重篤有害事象一覧", 1)
    _fill_table(doc,
        ["症例ID", "SOC / PT", "重症度", "因果関係", "転帰"],
        [
            ["DBX-JP-014", "胃腸障害 / 消化管出血",    "重度",   "関連あり",     "回復"],
            ["DBX-JP-028", "肝胆道系障害 / 肝機能異常", "中等度", "関連の可能性", "回復"],
            ["DBX-JP-057", "血液系 / 好中球減少症",    "中等度", "関連あり",     "回復"],
            ["DBX-JP-092", "胃腸障害 / 腸閉塞",       "重度",   "関連なし",     "後遺症あり"],
        ])
    doc.add_paragraph()

    _h(doc, "4. シグナル評価", 1)
    _p(doc, "報告期間中、当該医薬品の安全性プロファイルに関する新たなシグナルは、"
             "自発報告、市販後調査、および文献レビューにおいて検出されなかった。"
             "既知のリスクである消化器症状については、添付文書の記載内容と一致していた。")

    _h(doc, "5. リスク・ベネフィット評価", 1)
    _p(doc, "報告期間中に得られた有効性および安全性データを総合的に評価した結果、"
             "DBX-101のリスク・ベネフィットバランスは引き続き良好であると判断される。"
             "現時点で添付文書の改訂を要する情報は認められなかった。")

    out = OUT_DIR / "02_ae_summary_ja.docx"
    doc.save(out)
    return out


# ---------------------------------------------------------------------------
# Doc 3 — CTD Module 2.7.3 Clinical Efficacy Summary (JA)
# Structure: ICH M4E (R2) — Common Technical Document for the Registration of Pharmaceuticals
# ---------------------------------------------------------------------------

def build_ctd_efficacy() -> Path:
    doc = Document()
    _add_header_footer(
        doc,
        header_text="CTD 第2.7.3項  臨床的有効性の概要  |  DBX-101  |  版 1.0",
        footer_text="Databricks Pharma KK  申請資料",
    )

    _h(doc, "第2.7.3項 臨床的有効性の概要", 0)
    _p(doc, "本項では、DBX-101 (開発コード) の臨床開発プログラムにおいて実施された主要な有効性試験の結果を要約する。"
             "本要約は、ICH M4E (R2) Common Technical Document ガイドラインに基づいて作成されている。")

    _h(doc, "1. 有効性試験の概要", 1)
    _fill_table(doc,
        ["試験番号", "相", "デザイン", "対象疾患", "登録症例数", "主要評価項目"],
        [
            ["DBX101-JP-001", "第I相",  "非盲検 用量漸増",       "進行固形癌",    "18",  "最大耐用量"],
            ["DBX101-JP-002", "第II相", "無作為化 二重盲検 比較", "対象疾患",     "180", "無増悪生存期間"],
            ["DBX101-GLB-003", "第II相", "非盲検 単群",          "対象疾患サブタイプ", "45", "客観的奏効率"],
        ])
    doc.add_paragraph()

    _h(doc, "2. 主要有効性結果", 1)
    _p(doc, "第II相比較試験 (DBX101-JP-002) において、主要評価項目である無増悪生存期間の中央値は、"
             "DBX-101高用量群で10.4か月 (95%CI: 8.6-12.1)、対照群で6.5か月 (95%CI: 5.2-7.8) であり、"
             "ハザード比 0.68 (95%CI: 0.55-0.84, 片側 p=0.0004) と統計学的に有意な延長が認められた。")

    _fill_table(doc,
        ["エンドポイント", "DBX-101 100 mg", "DBX-101 200 mg", "対照群", "HR (95%CI)", "p値"],
        [
            ["PFS 中央値 (月)",    "8.2 (6.9-9.8)",   "10.4 (8.6-12.1)", "6.5 (5.2-7.8)", "0.68 (0.55-0.84)", "0.0004"],
            ["OS 中央値 (月)",     "18.3 (15.1-22.0)", "21.7 (18.2-25.9)", "16.1 (13.4-19.2)", "0.79 (0.61-1.02)", "0.068"],
            ["ORR n (%)",         "22 (36.7)",       "31 (51.7)",       "12 (20.0)",     "N/A",              "<0.001"],
            ["奏効期間 中央値 (月)", "7.5 (5.8-9.6)",   "9.8 (7.2-12.4)",  "5.2 (3.6-7.1)", "N/A",              "N/A"],
        ])
    doc.add_paragraph()

    _h(doc, "3. 部分集団解析", 1)
    _p(doc, "事前に規定された部分集団において、主要評価項目のハザード比を評価した。"
             "全ての部分集団において、DBX-101はPFSを延長する方向性を示した。")
    _fig(doc, _forest_png(),
         "図3. 部分集団別 主要評価項目 (PFS) ハザード比のフォレストプロット (模擬データ)")

    _h(doc, "4. 副次評価項目の考察", 1)
    _p(doc, "全生存期間 (OS) については、DBX-101高用量群で対照群に比して延長傾向が認められたが、"
             "本試験のデータカットオフ時点においては統計学的な有意差には至らなかった。"
             "追跡調査は継続中であり、更新解析結果は今後の申請資料補完で提出予定である。")

    _h(doc, "5. 結論", 1)
    _p(doc, "DBX-101は、対象疾患を有する成人患者において、標準治療と比較して"
             "臨床的に意義のある無増悪生存期間の延長を示した。"
             "本項に示した有効性データと第2.7.4項の安全性データを総合的に評価し、"
             "本剤の臨床的有用性は良好であると結論する。")

    out = OUT_DIR / "03_ctd_efficacy_ja.docx"
    doc.save(out)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    outs = [build_protocol_synopsis(), build_ae_summary(), build_ctd_efficacy()]
    for p in outs:
        size_kb = p.stat().st_size / 1024
        print(f"  wrote {p.name}  ({size_kb:.1f} KB)")
    print(f"\n{len(outs)} demo DOCX files ready in {OUT_DIR}")
