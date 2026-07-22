import { useEffect, useMemo, useRef } from "react";

export interface FeedbackMeta {
  status: "pending" | "certified" | "flagged";
  commented: boolean;
  edited: boolean;
}

// One rendered-DOCX pane. The HTML (from server docx_render.render) carries
// data-pidx / data-page on each block. We annotate status dots + page dividers,
// wire click/hover delegation, draw a minimap, and scroll to the active
// paragraph. Navigation is document-centric: you read/scroll the actual doc.
export function DocPane({
  title,
  lang,
  html,
  totalParas,
  feedback,
  activeIdx,
  hoverIdx,
  pageForActive,
  totalPages,
  onActivate,
  onHover,
}: {
  title: string;
  lang: string;
  html: string;
  totalParas: number;
  feedback: Record<number, FeedbackMeta>;
  activeIdx: number | null;
  hoverIdx: number | null;
  pageForActive: number | null;
  totalPages: number;
  onActivate: (idx: number) => void;
  onHover: (idx: number | null) => void;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const docRef = useRef<HTMLDivElement>(null);

  // Keep latest callbacks in refs so the delegated listeners (wired once per
  // html change) always call the current handlers.
  const cb = useRef({ onActivate, onHover });
  cb.current = { onActivate, onHover };

  // Structural annotation + event wiring — runs when the HTML changes.
  useEffect(() => {
    const root = docRef.current;
    if (!root) return;

    // Wrap wide tables so they scroll horizontally instead of the whole pane.
    root.querySelectorAll("table").forEach((tbl) => {
      if (tbl.parentElement?.classList.contains("table-scroll")) return;
      const wrap = document.createElement("div");
      wrap.className = "table-scroll";
      tbl.parentNode?.insertBefore(wrap, tbl);
      wrap.appendChild(tbl);
    });

    // Insert page dividers before the first paragraph of each new page.
    let lastPage = 1;
    root.querySelectorAll("[data-pidx]").forEach((el) => {
      const page = parseInt(el.getAttribute("data-page") || "1", 10);
      if (page > lastPage) {
        const prev = el.previousElementSibling;
        if (!prev || !prev.classList?.contains("page-divider")) {
          const div = document.createElement("div");
          div.className = "page-divider";
          div.textContent = "Page " + page;
          el.parentNode?.insertBefore(div, el);
        }
        lastPage = page;
      }
    });

    const onClick = (e: Event) => {
      // Don't hijack a click that's really a text selection — let the user
      // select + copy source/translation text without it snapping the panes.
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim().length > 0) return;
      const t = (e.target as HTMLElement).closest("[data-pidx]");
      if (t) cb.current.onActivate(parseInt(t.getAttribute("data-pidx")!, 10));
    };
    const onOver = (e: Event) => {
      const t = (e.target as HTMLElement).closest("[data-pidx]");
      if (t) cb.current.onHover(parseInt(t.getAttribute("data-pidx")!, 10));
    };
    const onLeave = () => cb.current.onHover(null);
    root.addEventListener("click", onClick);
    root.addEventListener("mouseover", onOver);
    root.addEventListener("mouseleave", onLeave);
    return () => {
      root.removeEventListener("click", onClick);
      root.removeEventListener("mouseover", onOver);
      root.removeEventListener("mouseleave", onLeave);
    };
  }, [html]);

  // Status attributes — runs when feedback changes.
  useEffect(() => {
    const root = docRef.current;
    if (!root) return;
    root.querySelectorAll("[data-pidx]").forEach((el) => {
      const idx = parseInt(el.getAttribute("data-pidx")!, 10);
      const fb = feedback[idx];
      el.setAttribute("data-status", fb?.status || "pending");
      el.setAttribute("data-commented", fb?.commented ? "1" : "0");
      el.setAttribute("data-edited", fb?.edited ? "1" : "0");
    });
  }, [feedback, html]);

  // Highlight only (active + hover). Cheap class toggles — safe to run on
  // every hover. Crucially this does NOT scroll, so hovering a paragraph or a
  // re-render never yanks the viewport.
  useEffect(() => {
    const root = docRef.current;
    if (!root) return;
    root.querySelectorAll("[data-pidx].active").forEach((el) => el.classList.remove("active"));
    root.querySelectorAll("[data-pidx].pair-hover").forEach((el) => el.classList.remove("pair-hover"));
    if (activeIdx !== null) {
      root.querySelector(`[data-pidx="${activeIdx}"]`)?.classList.add("active");
    }
    if (hoverIdx !== null && hoverIdx !== activeIdx) {
      root.querySelector(`[data-pidx="${hoverIdx}"]`)?.classList.add("pair-hover");
    }
  }, [activeIdx, hoverIdx, html]);

  // Scroll-to-active — runs ONLY when the active paragraph actually changes,
  // never on hover or re-render. This is what fixes the "snap back to the
  // highlighted paragraph while I'm scrolling" bug: scrolling/hovering no
  // longer re-triggers a scrollIntoView. We also skip the scroll when the
  // active paragraph is already comfortably in view.
  const lastScrolled = useRef<number | null>(null);
  useEffect(() => {
    const root = docRef.current;
    const body = bodyRef.current;
    if (!root || !body || activeIdx === null) return;
    if (lastScrolled.current === activeIdx) return;
    lastScrolled.current = activeIdx;
    const el = root.querySelector<HTMLElement>(`[data-pidx="${activeIdx}"]`);
    if (!el) return;
    const bodyRect = body.getBoundingClientRect();
    const elRect = el.getBoundingClientRect();
    const fullyVisible = elRect.top >= bodyRect.top && elRect.bottom <= bodyRect.bottom;
    if (!fullyVisible) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [activeIdx]);

  const segments = useMemo(() => {
    if (!totalParas) return [];
    return Array.from({ length: totalParas }, (_, i) => {
      const fb = feedback[i];
      const status = fb?.status || "pending";
      const cls = status === "pending" && fb?.commented ? "commented" : status;
      return { i, cls };
    });
  }, [totalParas, feedback]);

  return (
    <div className="flex min-h-0 flex-col rounded-lg border bg-card">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
            {title}
          </span>
          <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] font-semibold uppercase text-secondary-foreground">
            {lang}
          </span>
        </div>
        {pageForActive !== null && (
          <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
            p. {pageForActive} / {totalPages || "?"}
          </span>
        )}
      </div>
      <div ref={bodyRef} className="relative max-h-[calc(100vh-250px)] overflow-y-auto px-4 py-3">
        <div ref={docRef} className="docx-doc" dangerouslySetInnerHTML={{ __html: html }} />
        <div className="doc-minimap">
          {segments.map((s) => (
            <div
              key={s.i}
              className={`seg ${s.cls}`}
              style={{ top: `${(s.i / totalParas) * 100}%`, height: `${Math.max(100 / totalParas, 0.5)}%` }}
              title={`¶${s.i} · ${s.cls}`}
              onClick={() => onActivate(s.i)}
            />
          ))}
          {activeIdx !== null && totalParas > 0 && (
            <div className="active-marker" style={{ top: `${(activeIdx / totalParas) * 100}%` }} />
          )}
        </div>
      </div>
    </div>
  );
}
