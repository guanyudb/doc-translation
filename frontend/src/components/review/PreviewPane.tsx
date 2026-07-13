import { useEffect, useRef } from "react";

// Renders one side of the DOCX as HTML (produced by server docx_render.render,
// which stamps each block with data-pidx). When the active paragraph changes we
// scroll its element into view and highlight it, keeping both panes in sync
// with the action rail.
export function PreviewPane({
  title,
  html,
  activeIdx,
}: {
  title: string;
  html: string;
  activeIdx: number | null;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    root.querySelectorAll("[data-pidx].pidx-active").forEach((el) =>
      el.classList.remove("pidx-active")
    );
    if (activeIdx === null) return;
    const el = root.querySelector<HTMLElement>(`[data-pidx="${activeIdx}"]`);
    if (el) {
      el.classList.add("pidx-active");
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [activeIdx, html]);

  return (
    <div className="flex min-h-0 flex-col rounded-lg border bg-card">
      <div className="border-b px-3 py-2 text-xs font-semibold text-muted-foreground">
        {title}
      </div>
      <div
        ref={ref}
        className="docx-preview max-h-[calc(100vh-260px)] overflow-y-auto px-4 py-3"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}
