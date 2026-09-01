import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, ScrollText, UploadCloud, Award, Loader2, FilePlus2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/input";
import { api, PairDetail, PairSummary, Paragraph } from "@/api";
import { DocPane, FeedbackMeta } from "@/components/review/DocPane";
import { ActiveParagraphPanel } from "@/components/review/ActiveParagraphPanel";
import { UploadDialog } from "@/components/review/UploadDialog";
import { ProcessingPanel } from "@/components/review/ProcessingPanel";

export function ReviewView({
  activePair,
  setActivePair,
  onOpenAudit,
  defaultTarget,
}: {
  activePair: string | null;
  setActivePair: (id: string | null) => void;
  onOpenAudit: (id: string) => void;
  defaultTarget: string;
}) {
  const [uploadOpen, setUploadOpen] = useState(false);
  const [pairs, setPairs] = useState<PairSummary[]>([]);
  const [detail, setDetail] = useState<PairDetail | null>(null);
  const [origHtml, setOrigHtml] = useState("");
  const [tranHtml, setTranHtml] = useState("");
  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadPairs = useCallback(() => {
    api.pairs().then(setPairs).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    loadPairs();
  }, [loadPairs]);

  // Auto-refresh the pair list while anything is still going through the
  // pipeline. Uploading only writes to raw_documents/ — translation takes
  // 1–3 min, so refreshing at upload time (which is all we used to do) always
  // came back before the pair existed and the new document never showed up
  // until you hit refresh manually. Poll /api/documents; while any file is
  // QUEUED or TRANSLATING, re-pull the pairs so the document appears on its
  // own the moment translation finishes. Stops polling once everything settles.
  const [pipelineBusy, setPipelineBusy] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const r = await api.documents();
        const inflight = r.documents.some(
          (d) => d.status === "QUEUED" || d.status === "TRANSLATING"
        );
        if (cancelled) return;
        setPipelineBusy(inflight);
        if (inflight) {
          loadPairs();
          timer = window.setTimeout(poll, 10000);
        }
      } catch {
        /* transient — try again on the next cycle */
        if (!cancelled) timer = window.setTimeout(poll, 15000);
      }
    };
    poll();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [loadPairs]);

  const loadDetail = useCallback((id: string) => {
    setLoading(true);
    setError(null);
    // The pair detail is required; the two HTML previews are secondary — a
    // failed preview should leave that pane empty, not blank the whole view.
    api
      .pair(id)
      .then(async (d) => {
        setDetail(d);
        setActiveIdx(null);
        const [o, t] = await Promise.allSettled([
          api.preview(id, "original"),
          api.preview(id, "translated"),
        ]);
        setOrigHtml(o.status === "fulfilled" ? o.value : "");
        setTranHtml(t.status === "fulfilled" ? t.value : "");
        if (o.status === "rejected" || t.status === "rejected") {
          setError("A document preview failed to render; review actions still work.");
        }
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (activePair) loadDetail(activePair);
    else setDetail(null);
  }, [activePair, loadDetail]);

  const feedback = useMemo<Record<number, FeedbackMeta>>(() => {
    const m: Record<number, FeedbackMeta> = {};
    if (detail) {
      for (const p of detail.paragraphs) {
        m[p.idx] = {
          status: p.status,
          commented: !!(p.comment && p.comment.trim()),
          edited: p.edited_text !== null,
        };
      }
    }
    return m;
  }, [detail]);

  const progress = useMemo(() => {
    if (!detail) return { certified: 0, flagged: 0, pending: 0, total: 0 };
    let c = 0, f = 0, p = 0;
    for (const para of detail.paragraphs) {
      if (para.status === "certified") c++;
      else if (para.status === "flagged") f++;
      else p++;
    }
    return { certified: c, flagged: f, pending: p, total: detail.paragraphs.length };
  }, [detail]);

  const totalPages = useMemo(
    () => (detail ? detail.paragraphs.reduce((mx, p) => Math.max(mx, p.page), 1) : 1),
    [detail]
  );

  const activePara: Paragraph | null =
    detail && activeIdx !== null ? detail.paragraphs.find((p) => p.idx === activeIdx) ?? null : null;

  const patchPara = (updated: Paragraph) =>
    setDetail((d) =>
      d ? { ...d, paragraphs: d.paragraphs.map((x) => (x.idx === updated.idx ? updated : x)) } : d
    );

  const locked = detail?.locked ?? false;

  // Light refresh: re-fetch ONLY the review JSON (statuses/edits/lifecycle),
  // leaving the rendered HTML, the active paragraph, and both panes' scroll
  // positions untouched. Used after certify/publish/promote so the view
  // doesn't jump back to the top of the document.
  const refreshReviewState = useCallback(() => {
    if (!activePair) return Promise.resolve();
    return api
      .pair(activePair)
      .then((d) => setDetail((prev) => (prev ? { ...d } : d)))
      .catch((e) => setError(String(e)));
  }, [activePair]);

  const act = async (label: string, fn: () => Promise<unknown>, reload = false) => {
    setBusy(label);
    setError(null);
    try {
      await fn();
      if (reload) await refreshReviewState();
      loadPairs();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  // ---- Synchronized scrolling between the two panes ----------------------
  // Design notes (this is the third attempt — the earlier top-anchor +
  // suppress-counter version drifted badly on chart/table-heavy docs):
  //   * POINTER-DRIVEN: only the pane the cursor is over drives the other.
  //     One-way at a time → no feedback loop, so no guard/suppress counter
  //     is needed at all (the guard was the source of the racy drift when
  //     images finished loading mid-scroll and fired stray scroll events).
  //   * CENTER-ANCHORED with a FRACTIONAL offset: align the paragraph at the
  //     vertical center of the driving pane, and preserve how far *through*
  //     that paragraph you are. Tall blocks (charts, big tables) that render
  //     at different heights on each side then stay smoothly aligned instead
  //     of snapping.
  //   * rAF-THROTTLED: at most one alignment per frame.
  const origBody = useRef<HTMLDivElement | null>(null);
  const tranBody = useRef<HTMLDivElement | null>(null);
  const driver = useRef<"orig" | "tran" | null>(null);
  const rafPending = useRef(false);

  const alignFrom = useCallback((src: HTMLDivElement | null, dst: HTMLDivElement | null) => {
    if (!src || !dst) return;
    const srcRect = src.getBoundingClientRect();
    const focusY = srcRect.top + srcRect.height / 2; // vertical center of the driving pane

    // Find the paragraph spanning the center line, and how far through it we are.
    let anchorIdx: number | null = null;
    let frac = 0;
    for (const el of Array.from(src.querySelectorAll<HTMLElement>("[data-pidx]"))) {
      const r = el.getBoundingClientRect();
      if (r.bottom >= focusY && r.top <= focusY) {
        anchorIdx = parseInt(el.getAttribute("data-pidx")!, 10);
        frac = r.height > 0 ? (focusY - r.top) / r.height : 0;
        break;
      }
      // Fallback: first paragraph below the center line (gaps between blocks).
      if (r.top > focusY) {
        anchorIdx = parseInt(el.getAttribute("data-pidx")!, 10);
        frac = 0;
        break;
      }
    }
    if (anchorIdx === null) return;

    const target = dst.querySelector<HTMLElement>(`[data-pidx="${anchorIdx}"]`);
    if (!target) return;
    const dstRect = dst.getBoundingClientRect();
    const tRect = target.getBoundingClientRect();
    // Put the same fractional point of the same paragraph on dst's center line.
    const targetPointTop = tRect.top + frac * tRect.height;
    const delta = targetPointTop - (dstRect.top + dstRect.height / 2);
    const maxScroll = dst.scrollHeight - dst.clientHeight;
    const next = Math.max(0, Math.min(maxScroll, dst.scrollTop + delta));
    if (Math.abs(next - dst.scrollTop) > 0.5) dst.scrollTop = next;
  }, []);

  const onPaneScroll = useCallback(
    (who: "orig" | "tran") => {
      // Only the pane under the pointer drives; the mirrored pane's own scroll
      // event is ignored because it isn't the driver. No suppression needed.
      if (driver.current !== who) return;
      if (rafPending.current) return;
      rafPending.current = true;
      requestAnimationFrame(() => {
        rafPending.current = false;
        if (who === "orig") alignFrom(origBody.current, tranBody.current);
        else alignFrom(tranBody.current, origBody.current);
      });
    },
    [alignFrom]
  );

  const stepActive = (dir: 1 | -1) => {
    if (!detail || !detail.paragraphs.length) return;
    const cur = activeIdx ?? (dir === 1 ? -1 : detail.paragraphs.length);
    const next = Math.min(Math.max(cur + dir, 0), detail.paragraphs.length - 1);
    setActiveIdx(next);
  };

  return (
    <div className="space-y-4">
      {/* In-flight uploads: queued / translating docs, above the toolbar */}
      <ProcessingPanel onSettled={loadPairs} />

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3">
        <Select
          className="max-w-md"
          value={activePair ?? ""}
          onChange={(e) => setActivePair(e.target.value || null)}
        >
          <option value="">— select a document pair —</option>
          {pairs.map((p) => (
            <option key={p.pair_id} value={p.pair_id}>
              {p.lifecycle_state === "PUBLISHED" ? "📦 " : p.locked ? "🔒 " : ""}
              {p.pair_id} · {p.certified}/{p.total_paragraphs} certified
              {p.flagged ? ` · ⚑${p.flagged}` : ""}
            </option>
          ))}
        </Select>

        <Button variant="outline" size="sm" onClick={() => setUploadOpen(true)}>
          <FilePlus2 /> Upload
        </Button>
        <Button variant="ghost" size="icon-sm" onClick={loadPairs} title="Refresh list">
          <RefreshCw />
        </Button>
        {pipelineBusy && (
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" />
            translating — this list updates automatically
          </span>
        )}

        {detail && (
          <>
            <Badge variant="outline">
              {detail.source_lang ?? "?"} → {detail.target_lang ?? "?"}
            </Badge>
            <Badge className={`status-${detail.lifecycle_state.toLowerCase()}`}>
              {detail.lifecycle_state}
            </Badge>
            <div className="flex-1" />
            <Button
              variant="outline"
              size="sm"
              disabled={locked || busy !== null || activePara === null}
              title={activePara ? `Certify every paragraph on page ${activePara.page}` : "Select a paragraph first"}
              onClick={() =>
                activePara &&
                act("certify-page", () => api.certifyPage(detail.pair_id, activePara.page), true)
              }
            >
              {busy === "certify-page" ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
              Certify page{activePara ? ` ${activePara.page}` : ""}
            </Button>
            <Button
              variant="success"
              size="sm"
              disabled={locked || busy !== null}
              onClick={() => act("certify", () => api.certifyAll(detail.pair_id), true)}
            >
              {busy === "certify" ? <Loader2 className="animate-spin" /> : <CheckCircle2 />} Certify all
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={locked || busy !== null}
              onClick={() => act("publish", () => api.publish(detail.pair_id), true)}
            >
              {busy === "publish" ? <Loader2 className="animate-spin" /> : <UploadCloud />} Publish
            </Button>
            <Button
              variant="default"
              size="sm"
              disabled={busy !== null}
              onClick={() => act("promote", () => api.promote(detail.pair_id), true)}
            >
              {busy === "promote" ? <Loader2 className="animate-spin" /> : <Award />} Promote to gold
            </Button>
            <Button variant="ghost" size="sm" onClick={() => onOpenAudit(detail.pair_id)}>
              <ScrollText /> Audit
            </Button>
          </>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {detail && (
        <div className="space-y-1.5">
          {/* Thin completion bar: certified (green) + flagged (rose) vs total */}
          <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="bg-emerald-500 transition-all"
              style={{ width: `${progress.total ? (progress.certified / progress.total) * 100 : 0}%` }}
            />
            <div
              className="bg-rose-500 transition-all"
              style={{ width: `${progress.total ? (progress.flagged / progress.total) * 100 : 0}%` }}
            />
          </div>
          <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">
              {progress.total ? Math.round((progress.certified / progress.total) * 100) : 0}% certified
            </span>
            <span>·</span>
            <span>{progress.total} paragraphs</span>
            <span className="text-emerald-600">✓ {progress.certified} certified</span>
            <span className="text-rose-600">⚑ {progress.flagged} flagged</span>
            <span>◦ {progress.pending} not yet reviewed</span>
            {locked && <span className="font-medium text-amber-600">· locked (read-only)</span>}
          </div>
        </div>
      )}

      {loading && (
        <div className="flex items-center gap-2 py-16 text-muted-foreground">
          <Loader2 className="animate-spin" /> Loading document…
        </div>
      )}

      {!loading && !detail && (
        <div className="py-16 text-center text-muted-foreground">
          Select a document pair above to begin reviewing.
        </div>
      )}

      {/* Document-centric layout: two synced panes + compact inspector */}
      {!loading && detail && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_340px]">
          <div className="grid grid-cols-2 gap-3">
            <DocPane
              title="Source"
              lang={(detail.source_lang ?? "src").toUpperCase()}
              html={origHtml}
              totalParas={progress.total}
              feedback={feedback}
              activeIdx={activeIdx}
              hoverIdx={hoverIdx}
              pageForActive={activePara?.page ?? null}
              totalPages={totalPages}
              onActivate={setActiveIdx}
              onHover={setHoverIdx}
              onBodyMount={(el) => (origBody.current = el)}
              onEnter={() => (driver.current = "orig")}
              onScroll={() => onPaneScroll("orig")}
            />
            <DocPane
              title="Translation"
              lang={(detail.target_lang ?? "tgt").toUpperCase()}
              html={tranHtml}
              totalParas={progress.total}
              feedback={feedback}
              activeIdx={activeIdx}
              hoverIdx={hoverIdx}
              pageForActive={activePara?.page ?? null}
              totalPages={totalPages}
              onActivate={setActiveIdx}
              onHover={setHoverIdx}
              onBodyMount={(el) => (tranBody.current = el)}
              onEnter={() => (driver.current = "tran")}
              onScroll={() => onPaneScroll("tran")}
            />
          </div>

          <ActiveParagraphPanel
            para={activePara}
            locked={locked}
            onStatus={(status) =>
              activePara &&
              act(`status-${activePara.idx}`, async () =>
                patchPara(await api.setStatus(detail.pair_id, activePara.idx, status))
              )
            }
            onComment={(comment) =>
              activePara &&
              act(`comment-${activePara.idx}`, async () =>
                patchPara(await api.setComment(detail.pair_id, activePara.idx, comment))
              )
            }
            onEdit={(text) =>
              activePara &&
              act(`edit-${activePara.idx}`, async () =>
                patchPara(await api.setEdit(detail.pair_id, activePara.idx, text))
              )
            }
            onPrev={() => stepActive(-1)}
            onNext={() => stepActive(1)}
          />
        </div>
      )}

      <UploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        defaultTarget={defaultTarget}
        onUploaded={loadPairs}
      />
    </div>
  );
}
