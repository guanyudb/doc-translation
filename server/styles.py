"""All Streamlit-side CSS for the doc-translation app, kept out of app.py
so the markup stays readable. Editorial / refined / workshop aesthetic:
warm parchment palette, Fraunces (display) + IBM Plex Sans (UI) + Source
Serif 4 (body), single oxidized-teal accent, hairline rules instead of
hard borders."""

CSS = """<style>
  @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&display=swap');

  /* ===== Design tokens ===== */
  :root {
    --bg:          #fbfaf6;
    --bg-soft:     #f4f1e9;
    --bg-card:     #ffffff;
    --rule:        #e2dccc;
    --rule-soft:   #ece7da;

    --ink:         #1a1d22;
    --ink-2:       #3f4651;
    --ink-mute:    #7f7768;
    --ink-soft:    #a59c8a;

    --accent:        #0f766e;  /* oxidized teal */
    --accent-strong: #0b5650;
    --accent-soft:   #ccfbf1;

    --warn:    #b45309;
    --danger:  #9d174d;
    --info:    #1d4ed8;
    --success: #15803d;

    --serif-display: 'Fraunces', 'Source Serif 4', Georgia, serif;
    --serif-body:    'Source Serif 4', 'Iowan Old Style', Georgia, serif;
    --sans:          'IBM Plex Sans', system-ui, sans-serif;
    --mono:          'IBM Plex Mono', ui-monospace, monospace;
  }

  /* ===== Streamlit chrome ===== */
  html, body, .stApp { background: var(--bg) !important; }
  body { font-family: var(--sans); color: var(--ink); }
  .block-container {
    padding: 0.7rem 1.4rem 0 1.4rem !important;
    max-width: 100% !important;
  }
  /* Keep the Streamlit header structurally present (height: 0) so the
     sidebar reopen chevron — which Streamlit positions inside stToolbar
     inside stHeader — stays available even when the user collapses the
     sidebar. We hide chrome PER-CHILD instead of hiding stToolbar wholesale,
     because the expand button is a sibling of the chrome inside the toolbar. */
  header[data-testid="stHeader"] {
    background: transparent;
    height: 0;
    min-height: 0;
    overflow: visible;
  }
  [data-testid="stStatusWidget"],
  [data-testid="stDecoration"],
  [data-testid="stAppDeployButton"] { display: none !important; }
  [data-testid="stToolbar"] { background: transparent !important; }
  /* Pin the sidebar expand chevron at top-left, fully on-screen and tappable.
     Otherwise it inherits the zero-height header above and is half clipped. */
  [data-testid="stExpandSidebarButton"] {
    position: fixed !important;
    top: 10px !important;
    left: 12px !important;
    visibility: visible !important;
    opacity: 1 !important;
    pointer-events: auto !important;
    z-index: 999 !important;
    width: 32px !important;
    height: 32px !important;
    background: var(--bg-card) !important;
    border: 1px solid var(--rule) !important;
    border-radius: 4px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.06);
  }
  [data-testid="stExpandSidebarButton"]:hover {
    background: var(--bg-soft) !important;
    border-color: var(--ink-soft) !important;
  }
  [data-testid="stSidebarCollapseButton"] {
    visibility: visible !important;
    pointer-events: auto !important;
  }
  #MainMenu, footer { display: none !important; }
  div[data-testid="stVerticalBlock"] { gap: 0.5rem !important; }
  [data-testid="stMarkdown"], [data-testid="stMarkdownContainer"],
  [data-testid="stVerticalBlock"], [data-testid="stHorizontalBlock"] {
    overflow: visible !important;
  }

  /* ===== Sidebar ===== */
  section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #f6f2e7 0%, #fbfaf6 60%);
    border-right: 1px solid var(--rule);
  }
  section[data-testid="stSidebar"] .block-container { padding-top: 1.2rem; }

  /* ===== Force the dual-pane component iframe to fill the viewer column ===== */
  div[data-testid="stColumn"]:nth-of-type(1) iframe[title*="dual_pane"],
  div[data-testid="stColumn"]:nth-of-type(1) [data-testid="stCustomComponentV1"] iframe {
    min-height: 80vh !important;
    height: 80vh !important;
  }

  /* ===== Top header ===== */
  .app-title {
    font-family: var(--serif-display);
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.012em;
    line-height: 1.2;
    color: var(--ink);
    margin: 0 0 4px 0;
  }
  .app-title .lang-tag {
    font-family: var(--sans);
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    background: var(--accent-soft);
    color: var(--accent-strong);
    padding: 3px 8px;
    border-radius: 2px;
    margin-left: 10px;
    vertical-align: middle;
  }
  /* Pair-name title gets a small accent rule under it */
  .main-title-block .app-title::after {
    content: '';
    display: block;
    width: 32px; height: 2px;
    background: var(--accent);
    margin-top: 8px;
  }
  .app-sub {
    font-family: var(--sans);
    font-size: 11px;
    color: var(--ink-mute);
    margin: 0 0 2px 0;
  }
  .app-sub code {
    font-family: var(--mono);
    font-size: 10.5px;
    color: var(--ink-2);
    background: transparent;
    padding: 0 2px;
  }

  /* ===== Progress bar ===== */
  .progress-shell {
    height: 8px; border-radius: 2px; overflow: hidden;
    background: var(--rule-soft); display: flex;
    margin: 8px 0 6px 0;
    box-shadow: inset 0 0 0 1px rgba(0,0,0,0.02);
  }
  .progress-shell > div { transition: width 200ms ease; }
  .progress-meta {
    font-family: var(--sans);
    font-size: 11px; color: var(--ink-2);
    font-variant-numeric: tabular-nums;
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
    letter-spacing: 0.005em;
  }
  .progress-meta .ok { color: var(--success); font-weight: 500; }
  .progress-meta .fl { color: var(--danger);  font-weight: 500; }
  .progress-meta .cm { color: var(--info);    font-weight: 500; }
  .progress-meta .pn { color: var(--ink-mute);font-weight: 500; }
  .progress-meta .total {
    color: var(--ink); font-weight: 600;
    margin-left: auto; font-size: 13px; font-family: var(--mono);
  }

  /* Lifecycle chip on the header — sits next to the lang-tag. */
  .lifecycle-chip {
    display: inline-block;
    margin-left: 10px;
    padding: 2px 8px;
    font-family: var(--sans);
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    border-radius: 2px;
    vertical-align: middle;
  }
  /* Progress chip — shows the certified/total + percent so the selected pair's
     completion is visible even though the sidebar is just a dropdown. */
  .progress-chip {
    display: inline-block;
    margin-left: 8px;
    padding: 2px 8px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    border-radius: 2px;
    vertical-align: middle;
  }

  /* Read-only banner. Shown at the top of the rail when the document is
     PUBLISHED — every write control below it is also disabled. */
  .locked-banner {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    margin: 0 0 12px 0;
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    border-left: 3px solid #15803d;
    border-radius: 3px;
    font-family: var(--sans);
    font-size: 11px;
    color: #14532d;
    line-height: 1.4;
  }
  .locked-banner .glyph { font-size: 18px; line-height: 1; }
  .locked-banner b { color: #14532d; }

  /* ===== Right rail ===== */
  .rail-section-label {
    font-family: var(--sans) !important;
    font-size: 10px !important;
    font-weight: 600 !important;
    letter-spacing: 0.14em !important;
    text-transform: uppercase !important;
    color: var(--ink-mute) !important;
    line-height: 1 !important;
    padding: 18px 0 14px 0 !important;
    margin: 0 !important;
    display: flex !important;
    align-items: center !important;
    gap: 10px !important;
    min-height: 24px !important;
    position: relative !important;
    z-index: 2 !important;
  }
  /* Belt-and-braces: add a top gap to the FIRST widget container that follows
     a rail-section-label, so Streamlit's collapsing element margins can't make
     the label rule overlap the input below it. */
  .review-rail [data-testid="stMarkdownContainer"]:has(.rail-section-label) + div {
    margin-top: 4px !important;
  }
  .rail-section-label::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--rule);
  }
  /* Native textarea label uses same look */
  div[data-testid="stColumn"]:nth-of-type(2) .stTextArea label,
  div[data-testid="stColumn"]:nth-of-type(2) .stTextArea label p {
    font-family: var(--sans) !important;
    font-size: 10px !important;
    font-weight: 600 !important;
    letter-spacing: 0.14em !important;
    text-transform: uppercase !important;
    color: var(--ink-mute) !important;
    line-height: 1 !important;
    margin: 12px 0 6px 0 !important;
  }
  /* Original / Translated preview boxes */
  .para-text {
    font-family: var(--serif-body);
    font-size: 13.5px; line-height: 1.55; color: var(--ink);
    background: var(--bg-card);
    border: none;
    border-left: 2px solid var(--accent);
    border-radius: 0 3px 3px 0;
    padding: 10px 14px;
    /* Fixed height — same for short and long paragraphs so the rail layout
       stays stable as you navigate. Long text scrolls inside. */
    height: 100px; overflow-y: auto;
    box-shadow: 0 1px 0 var(--rule), 1px 0 0 var(--rule), -1px 0 0 var(--rule), 0 -1px 0 var(--rule);
  }
  .para-text.translated { border-left-color: var(--info); }
  .para-text::-webkit-scrollbar { width: 6px; }
  .para-text::-webkit-scrollbar-thumb {
    background: var(--rule); border-radius: 3px;
    border: 1.5px solid transparent; background-clip: content-box;
  }

  /* Rail widgets */
  div[data-testid="stColumn"]:nth-of-type(2) div[data-testid="stHorizontalBlock"] button,
  div[data-testid="stColumn"]:nth-of-type(2) .stButton > button {
    font-family: var(--sans) !important;
    padding: 5px 8px !important;
    min-height: 34px !important;
    font-size: 12.5px !important;
    font-weight: 500 !important;
    letter-spacing: -0.01em !important;
    border-radius: 3px !important;
    border: 1px solid var(--rule) !important;
    background: var(--bg-card) !important;
    color: var(--ink) !important;
    box-shadow: none !important;
    transition: border-color 100ms, background 100ms;
    white-space: normal !important;
    line-height: 1.25 !important;
    overflow: visible !important;
    text-overflow: clip !important;
  }
  div[data-testid="stColumn"]:nth-of-type(2) .stButton > button > div,
  div[data-testid="stColumn"]:nth-of-type(2) .stButton > button > div > p {
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
  }
  div[data-testid="stColumn"]:nth-of-type(2) .stButton > button:hover {
    border-color: var(--ink-2) !important;
    background: var(--bg-card) !important;
  }
  /* Primary buttons (e.g. status: certified, certify-page) */
  div[data-testid="stColumn"]:nth-of-type(2) .stButton > button[kind="primary"] {
    background: var(--ink) !important;
    border-color: var(--ink) !important;
    color: var(--bg) !important;
  }
  div[data-testid="stColumn"]:nth-of-type(2) .stButton > button[kind="primary"]:hover {
    background: var(--ink-2) !important;
    border-color: var(--ink-2) !important;
  }

  /* Number input */
  div[data-testid="stColumn"]:nth-of-type(2) .stNumberInput > div > div input {
    font-family: var(--mono) !important;
    padding: 4px 8px !important;
    height: 34px !important;
    font-size: 13px !important;
    border: 1px solid var(--rule) !important;
    background: var(--bg-card) !important;
    color: var(--ink) !important;
    border-radius: 3px !important;
  }
  div[data-testid="stColumn"]:nth-of-type(2) .stNumberInput button {
    height: 34px !important;
    width: 26px !important;
    border: 1px solid var(--rule) !important;
    background: var(--bg-card) !important;
    border-radius: 3px !important;
  }

  /* Textarea */
  div[data-testid="stColumn"]:nth-of-type(2) .stTextArea textarea {
    font-family: var(--serif-body) !important;
    font-size: 13.5px !important;
    line-height: 1.5 !important;
    padding: 10px 12px !important;
    min-height: 84px !important;
    max-height: 130px !important;
    border: 1px solid var(--rule) !important;
    background: var(--bg-card) !important;
    border-radius: 3px !important;
    color: var(--ink) !important;
  }
  div[data-testid="stColumn"]:nth-of-type(2) .stTextArea textarea::placeholder {
    color: var(--ink-soft);
    font-style: italic;
  }

  /* Checkboxes */
  div[data-testid="stColumn"]:nth-of-type(2) .stCheckbox label {
    font-family: var(--sans) !important;
    font-size: 12px !important;
    color: var(--ink-2) !important;
  }
  div[data-testid="stColumn"]:nth-of-type(2) label[data-testid="stWidgetLabel"] { display: none; }

  /* Tighter sub-block gaps in the rail column */
  div[data-testid="stColumn"]:nth-of-type(2) div[data-testid="stVerticalBlock"] { gap: 0.3rem !important; }
  div[data-testid="stColumn"]:nth-of-type(2) div[data-testid="stHorizontalBlock"] { gap: 0.3rem !important; }

  /* ===== Sidebar pair list ===== */
  section[data-testid="stSidebar"] .stButton > button {
    font-family: var(--sans) !important;
    text-align: left !important;
    padding: 10px 12px 10px 14px !important;
    min-height: 44px !important;
    font-size: 12px !important;
    line-height: 1.4 !important;
    white-space: normal !important;
    border: 1px solid var(--rule) !important;
    background: var(--bg-card) !important;
    color: var(--ink) !important;
    border-radius: 3px !important;
    box-shadow: none !important;
    transition: all 120ms;
    position: relative;
    letter-spacing: -0.005em;
  }
  section[data-testid="stSidebar"] .stButton > button:hover {
    border-color: var(--ink-2) !important;
    background: var(--bg-card) !important;
  }
  section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background: var(--bg-card) !important;
    color: var(--ink) !important;
    border-color: var(--rule) !important;
    border-left: 3px solid var(--accent) !important;
    padding-left: 12px !important;
    font-weight: 600 !important;
  }
  section[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
    border-left-color: var(--accent-strong) !important;
  }
  section[data-testid="stSidebar"] .app-title {
    font-family: var(--serif-display);
    font-size: 18px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  section[data-testid="stSidebar"] .app-title::after { display: none; }
  section[data-testid="stSidebar"] .app-sub b {
    font-weight: 500;
    color: var(--ink-2);
  }
  section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] strong {
    font-family: var(--sans);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-mute);
  }
  section[data-testid="stSidebar"] details summary {
    font-family: var(--sans);
    font-size: 11px !important;
    color: var(--ink-2);
  }
  section[data-testid="stSidebar"] .stCaption {
    font-family: var(--sans);
    font-size: 11px;
    color: var(--ink-mute);
  }

  /* st.caption (e.g. "Last edited by ...") */
  div[data-testid="stCaptionContainer"] {
    font-family: var(--sans) !important;
    font-size: 10.5px !important;
    color: var(--ink-mute) !important;
  }

  /* ===== Rail empty state ===== */
  .rail-empty {
    margin-top: 18px;
    padding: 24px 16px;
    border: 1px dashed var(--rule);
    border-radius: 4px;
    background: var(--bg-card);
    text-align: center;
  }
  .rail-empty-glyph {
    font-family: var(--serif-display);
    font-size: 36px;
    font-weight: 500;
    color: var(--ink-soft);
    line-height: 1;
    margin-bottom: 10px;
  }
  .rail-empty-title {
    font-family: var(--sans);
    font-size: 12px;
    font-weight: 600;
    color: var(--ink-2);
    letter-spacing: -0.01em;
    margin-bottom: 4px;
  }
  .rail-empty-help {
    font-family: var(--serif-body);
    font-size: 12px;
    color: var(--ink-mute);
    line-height: 1.5;
    font-style: italic;
  }

  /* ===== Rail legend ===== */
  .rail-legend {
    margin-top: 22px;
    padding-top: 4px;
  }
  .rail-legend .rail-section-label { padding-top: 0 !important; min-height: auto !important; }
  .legend-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px 14px;
    margin: 6px 0 10px 0;
  }
  .legend-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--sans);
    font-size: 11px;
    color: var(--ink-2);
  }
  .legend-row .dot {
    width: 8px; height: 8px; border-radius: 50%;
    flex-shrink: 0;
  }
  .legend-row .pending   { background: var(--ink-soft); }
  .legend-row .certified { background: var(--success); }
  .legend-row .flagged   { background: var(--danger); }
  .legend-row .commented { background: var(--ink-soft); box-shadow: 0 0 0 2px rgba(29,78,216,0.4); }
  .legend-tip {
    font-family: var(--serif-body);
    font-size: 11px;
    line-height: 1.5;
    color: var(--ink-mute);
    font-style: italic;
    padding: 8px 0 0 0;
    border-top: 1px solid var(--rule-soft);
  }
</style>
"""
