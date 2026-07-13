import { useEffect, useState } from "react";
import { CheckCircle2, Flag, MessageSquare, Pencil, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/input";
import { cn } from "@/lib/utils";
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
  return <Badge className={cls}>conf {pct}%</Badge>;
}

export function ParagraphCard({
  para,
  active,
  locked,
  onFocus,
  onStatus,
  onComment,
  onEdit,
}: {
  para: Paragraph;
  active: boolean;
  locked: boolean;
  onFocus: () => void;
  onStatus: (s: ParaStatus) => void;
  onComment: (c: string) => void;
  onEdit: (t: string | null) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(para.edited_text ?? para.translated);
  const [commenting, setCommenting] = useState(false);
  const [comment, setComment] = useState(para.comment ?? "");

  useEffect(() => {
    setDraft(para.edited_text ?? para.translated);
    setComment(para.comment ?? "");
  }, [para.edited_text, para.translated, para.comment]);

  const shownTranslation = para.edited_text ?? para.translated;

  return (
    <div
      onClick={onFocus}
      className={cn(
        "cursor-pointer rounded-lg border bg-card p-3 text-sm transition-colors",
        active ? "border-primary ring-1 ring-primary/40" : "hover:border-muted-foreground/40",
        para.status === "certified" && "border-l-4 border-l-emerald-500",
        para.status === "flagged" && "border-l-4 border-l-rose-500"
      )}
    >
      <div className="mb-1.5 flex items-center gap-2">
        <span className="text-xs font-medium text-muted-foreground">
          ¶{para.idx} · p.{para.page}
        </span>
        {confidenceBadge(para.confidence)}
        {para.edited_text !== null && <Badge variant="secondary">edited</Badge>}
        {para.status === "certified" && <Badge className="status-approved">certified</Badge>}
        {para.status === "flagged" && <Badge className="status-failed">flagged</Badge>}
      </div>

      <p className="mb-1 text-muted-foreground">{para.source}</p>

      {editing ? (
        <div className="space-y-2" onClick={(e) => e.stopPropagation()}>
          <Textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={4} />
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => {
                onEdit(draft === para.translated ? null : draft);
                setEditing(false);
              }}
            >
              Save edit
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <p className="font-medium">{shownTranslation}</p>
      )}

      {para.comment && !commenting && (
        <p className="mt-1.5 rounded bg-muted px-2 py-1 text-xs italic text-muted-foreground">
          💬 {para.comment}
        </p>
      )}

      {!locked && (
        <div className="mt-2 flex flex-wrap gap-1.5" onClick={(e) => e.stopPropagation()}>
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
          <Button size="sm" variant="ghost" onClick={() => setEditing((v) => !v)}>
            <Pencil /> Edit
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setCommenting((v) => !v)}>
            <MessageSquare /> Comment
          </Button>
          {para.edited_text !== null && (
            <Button size="sm" variant="ghost" onClick={() => onEdit(null)}>
              <RotateCcw /> Revert
            </Button>
          )}
        </div>
      )}

      {commenting && (
        <div className="mt-2 space-y-2" onClick={(e) => e.stopPropagation()}>
          <Textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            rows={2}
            placeholder="Reviewer comment…"
          />
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => {
                onComment(comment);
                setCommenting(false);
              }}
            >
              Save comment
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setCommenting(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
