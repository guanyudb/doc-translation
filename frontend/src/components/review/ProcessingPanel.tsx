import { useEffect, useRef, useState } from "react";
import { Loader2, Clock, AlertCircle, FileText } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { api, ProcessingDocument } from "@/api";

const POLL_MS = 5000;

/** Documents still in flight — everything else drops off the panel. */
const ACTIVE = new Set(["QUEUED", "TRANSLATING"]);

/** Human-readable elapsed time. */
function formatDuration(totalSeconds: number | null): string | null {
  if (totalSeconds == null || totalSeconds < 0) return null;
  if (totalSeconds < 60) return "less than a minute";
  const mins = Math.floor(totalSeconds / 60);
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

type StatusMeta = {
  label: string;
  badge: string; // Tailwind color classes layered on the Badge
  Icon: typeof Clock;
  spin?: boolean;
};

const STATUS_META: Record<string, StatusMeta> = {
  QUEUED: {
    label: "Queued",
    badge: "bg-muted text-muted-foreground",
    Icon: Clock,
  },
  TRANSLATING: {
    label: "Translating",
    badge: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
    Icon: Loader2,
    spin: true,
  },
};

function metaFor(status: string): StatusMeta {
  return (
    STATUS_META[status] ?? {
      label: status,
      badge: "bg-muted text-muted-foreground",
      Icon: FileText,
    }
  );
}

function DocumentRow({ doc }: { doc: ProcessingDocument }) {
  const meta = metaFor(doc.status);
  const elapsed = formatDuration(doc.elapsed_seconds);
  const langs = [doc.source_language, doc.target_language].filter(Boolean).join(" → ");

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1 text-sm">
      <div className="flex min-w-0 items-center gap-2">
        <FileText className="size-4 shrink-0 text-muted-foreground" />
        <span className="truncate font-medium" title={doc.file_name}>
          {doc.file_name}
        </span>
      </div>
      <Badge className={meta.badge}>
        <meta.Icon className={`mr-1 size-3 ${meta.spin ? "animate-spin" : ""}`} />
        {meta.label}
      </Badge>
      <div className="flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
        {elapsed && (
          <span className="tabular-nums">
            {doc.status === "TRANSLATING" ? "running " : ""}
            {elapsed}
          </span>
        )}
        {langs && <span>{langs}</span>}
        {doc.status === "QUEUED" && <span>waiting for the pipeline</span>}
      </div>
    </div>
  );
}

/**
 * Compact strip listing the current user's in-flight uploads (queued or
 * translating). Sits above the review toolbar. Renders nothing when there's
 * nothing in flight, and calls `onSettled` when a document leaves the active
 * set (finished or failed) so the caller can refresh the pair list.
 */
export function ProcessingPanel({ onSettled }: { onSettled?: () => void }) {
  const [docs, setDocs] = useState<ProcessingDocument[]>([]);
  // Track the previous active file names so we can detect completions.
  const prevActive = useRef<Set<string>>(new Set());
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  useEffect(() => {
    let cancelled = false;

    const tick = () =>
      api
        .processingStatus()
        .then((r) => {
          if (cancelled) return;
          const active = r.documents.filter((d) => ACTIVE.has(d.status));
          const activeNames = new Set(active.map((d) => d.file_name));
          // A name that was active last poll but isn't now has settled.
          let settled = false;
          for (const name of prevActive.current) {
            if (!activeNames.has(name)) {
              settled = true;
              break;
            }
          }
          prevActive.current = activeNames;
          setDocs(active);
          if (settled) onSettledRef.current?.();
        })
        .catch(() => {});

    tick(); // immediately, then poll
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  if (docs.length === 0) return null;

  return (
    <div className="rounded-lg border bg-card px-3 py-2">
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Loader2 className="size-3.5 animate-spin" />
        Processing {docs.length} document{docs.length === 1 ? "" : "s"}
      </div>
      <div className="mt-1 divide-y">
        {docs.map((d) => (
          <DocumentRow key={d.file_name} doc={d} />
        ))}
      </div>
    </div>
  );
}
