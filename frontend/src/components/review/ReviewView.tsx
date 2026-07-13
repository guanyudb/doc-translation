import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, Flag, ScrollText, UploadCloud, Award, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/input";
import { api, PairDetail, PairSummary, Paragraph } from "@/api";
import { PreviewPane } from "@/components/review/PreviewPane";
import { ParagraphCard } from "@/components/review/ParagraphCard";

export function ReviewView({
  activePair,
  setActivePair,
  onOpenAudit,
}: {
  activePair: string | null;
  setActivePair: (id: string | null) => void;
  onOpenAudit: (id: string) => void;
}) {
  const [pairs, setPairs] = useState<PairSummary[]>([]);
  const [detail, setDetail] = useState<PairDetail | null>(null);
  const [origHtml, setOrigHtml] = useState("");
  const [tranHtml, setTranHtml] = useState("");
  const [activeIdx, setActiveIdx] = useState<number | null>(null);
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
    Promise.all([
      api.pair(id),
      api.preview(id, "original"),
      api.preview(id, "translated"),
    ])
      .then(([d, o, t]) => {
        setDetail(d);
        setOrigHtml(o);
        setTranHtml(t);
        setActiveIdx(d.paragraphs.length ? d.paragraphs[0].idx : null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (activePair) loadDetail(activePair);
    else setDetail(null);
  }, [activePair, loadDetail]);

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

  const patchPara = (updated: Paragraph) => {
    setDetail((d) =>
      d
        ? { ...d, paragraphs: d.paragraphs.map((x) => (x.idx === updated.idx ? updated : x)) }
        : d
    );
  };

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

        {detail && (
          <>
            <Badge variant="outline" className="gap-1">
              {detail.source_lang ?? "?"} → {detail.target_lang ?? "?"}
            </Badge>
            <span className="status-badge">
              <Badge className={`status-${detail.lifecycle_state.toLowerCase()}`}>
                {detail.lifecycle_state}
              </Badge>
            </span>
            <div className="flex-1" />
            <Button
              variant="success"
              size="sm"
              disabled={locked || busy !== null}
              onClick={() => act("certify", () => api.certifyAll(detail.pair_id), true)}
            >
              {busy === "certify" ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
              Certify all
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={locked || busy !== null}
              onClick={() => act("publish", () => api.publish(detail.pair_id), true)}
            >
              {busy === "publish" ? <Loader2 className="animate-spin" /> : <UploadCloud />}
              Publish
            </Button>
            <Button
              variant="default"
              size="sm"
              disabled={busy !== null}
              onClick={() => act("promote", () => api.promote(detail.pair_id), true)}
            >
              {busy === "promote" ? <Loader2 className="animate-spin" /> : <Award />}
              Promote to gold
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
        <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
          <span>{progress.total} paragraphs</span>
          <span className="text-emerald-600">✓ {progress.certified} certified</span>
          <span className="text-rose-600">⚑ {progress.flagged} flagged</span>
          <span>… {progress.pending} pending</span>
          {locked && <span className="font-medium text-amber-600">· locked (read-only)</span>}
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

      {/* Two-region layout: preview (left) + action rail (right) */}
      {!loading && detail && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_460px]">
          <div className="grid grid-cols-2 gap-3">
            <PreviewPane title={`Source (${detail.source_lang ?? "?"})`} html={origHtml} activeIdx={activeIdx} />
            <PreviewPane
              title={`Translation (${detail.target_lang ?? "?"})`}
              html={tranHtml}
              activeIdx={activeIdx}
            />
          </div>

          <div className="max-h-[calc(100vh-220px)] space-y-2 overflow-y-auto pr-1">
            {detail.paragraphs.map((para) => (
              <ParagraphCard
                key={para.idx}
                para={para}
                active={para.idx === activeIdx}
                locked={locked}
                onFocus={() => setActiveIdx(para.idx)}
                onStatus={(status) =>
                  act(`status-${para.idx}`, async () =>
                    patchPara(await api.setStatus(detail.pair_id, para.idx, status))
                  )
                }
                onComment={(comment) =>
                  act(`comment-${para.idx}`, async () =>
                    patchPara(await api.setComment(detail.pair_id, para.idx, comment))
                  )
                }
                onEdit={(text) =>
                  act(`edit-${para.idx}`, async () =>
                    patchPara(await api.setEdit(detail.pair_id, para.idx, text))
                  )
                }
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
