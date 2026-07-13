import { useEffect, useState } from "react";
import { CheckCircle2, Flag, RotateCcw, ChevronUp, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/input";
import { ParaStatus, Paragraph } from "@/api";

function confidenceBadge(c: number | null) {
  if (c === null) return null;
  const pct = Math.round(c * 100);
  const cls =
    c >= 0.8
      ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/30 dark:text-emerald-200"
      : c >= 0.5
      ? "bg-amber-100 text-amber-900 dark:bg-amber-900/30 dark:text-amber-200"
      : "bg-rose-100 text-rose-900 dark:bg-rose-900/30 dark:text-rose-200";
  return <Badge className={cls}>confidence {pct}%</Badge>;
}

// Compact inspector for the currently-selected paragraph. All review actions
// live here (not on a long card list) — you pick a paragraph by navigating the
// document, and act on it here.
export function ActiveParagraphPanel({
  para,
  locked,
  onStatus,
  onComment,
  onEdit,
  onPrev,
  onNext,
}: {
  para: Paragraph | null;
  locked: boolean;
  onStatus: (s: ParaStatus) => void;
  onComment: (c: string) => void;
  onEdit: (t: string | null) => void;
  onPrev: () => void;
  onNext: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [comment, setComment] = useState("");

  useEffect(() => {
    setDraft(para ? para.edited_text ?? para.translated : "");
    setComment(para?.comment ?? "");
  }, [para?.idx, para?.edited_text, para?.translated, para?.comment]);

  if (!para) {
    return (
      <div className="rounded-lg border bg-card p-6 text-center text-sm text-muted-foreground">
        Click a paragraph in either pane to review it.
      </div>
    );
  }

  const dirtyEdit = draft !== (para.edited_text ?? para.translated);
  const dirtyComment = comment !== (para.comment ?? "");

  return (
    <div className="sticky top-[72px] space-y-3 rounded-lg border bg-card p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-muted-foreground">
          ¶{para.idx} · page {para.page}
        </span>
        <div className="flex gap-1">
          <Button size="icon-sm" variant="ghost" onClick={onPrev} title="Previous paragraph">
            <ChevronUp />
          </Button>
          <Button size="icon-sm" variant="ghost" onClick={onNext} title="Next paragraph">
            <ChevronDown />
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {confidenceBadge(para.confidence)}
        {para.confidence_flags.map((f) => (
          <Badge key={f} className="bg-rose-100 text-rose-900 dark:bg-rose-900/30 dark:text-rose-200">
            {f}
          </Badge>
        ))}
        {para.status === "certified" && <Badge className="status-approved">certified</Badge>}
        {para.status === "flagged" && <Badge className="status-failed">flagged</Badge>}
        {para.edited_text !== null && <Badge variant="secondary">edited</Badge>}
      </div>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          Source
        </div>
        <p className="rounded bg-muted/50 p-2 text-sm text-muted-foreground">{para.source || "—"}</p>
      </div>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          Translation {para.edited_text !== null && "(edited)"}
        </div>
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={5}
          disabled={locked}
        />
      </div>

      {!locked && (
        <>
          <div className="flex flex-wrap gap-1.5">
            <Button
              size="sm"
              variant={para.status === "certified" ? "success" : "outline"}
              onClick={() => onStatus(para.status === "certified" ? "pending" : "certified")}
            >
              <CheckCircle2 /> Certify
            </Button>
            <Button
              size="sm"
              variant={para.status === "flagged" ? "warning" : "outline"}
              onClick={() => onStatus(para.status === "flagged" ? "pending" : "flagged")}
            >
              <Flag /> Flag
            </Button>
            <Button size="sm" disabled={!dirtyEdit} onClick={() => onEdit(draft === para.translated ? null : draft)}>
              Save edit
            </Button>
            {para.edited_text !== null && (
              <Button size="sm" variant="ghost" onClick={() => onEdit(null)}>
                <RotateCcw /> Revert
              </Button>
            )}
          </div>

          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
              Comment
            </div>
            <Textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              rows={2}
              placeholder="Reviewer comment…"
            />
            <Button
              size="sm"
              variant="outline"
              className="mt-1.5"
              disabled={!dirtyComment}
              onClick={() => onComment(comment)}
            >
              Save comment
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
