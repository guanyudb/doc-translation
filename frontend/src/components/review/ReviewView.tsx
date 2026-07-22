import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, ScrollText, UploadCloud, Award, Loader2, FilePlus2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/input";
import { api, PairDetail, PairSummary, Paragraph } from "@/api";
import { DocPane, FeedbackMeta } from "@/components/review/DocPane";
import { ActiveParagraphPanel } from "@/components/review/ActiveParagraphPanel";
import { UploadDialog } from "@/components/review/UploadDialog";

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

  const loadDetail = useCallback((id: string) => {
    setLoading(true);
    setError(null);
    Promise.all([api.pair(id), api.preview(id, "original"), api.preview(id, "translated")])
      .then(([d, o, t]) => {
        setDetail(d);
        setOrigHtml(o);
        setTranHtml(t);
        setActiveIdx(null);
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

  const act = async (label: string, fn: () => Promise<unknown>, reload = false) => {
    setBusy(label);
    setError(null);
    try {
      await fn();
      if (reload && activePair) loadDetail(activePair);
      loadPairs();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const stepActive = (dir: 1 | -1) => {
    if (!detail || !detail.paragraphs.length) return;
    const cur = activeIdx ?? (dir === 1 ? -1 : detail.paragraphs.length);
    const next = Math.min(Math.max(cur + dir, 0), detail.paragraphs.length - 1);
    setActiveIdx(next);
  };

  return (
    <div className="space-y-4">
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
