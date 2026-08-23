import { useEffect, useRef, useState } from "react";
import {
  Loader2,
  Clock,
  CheckCircle2,
  AlertCircle,
  FileText,
  Activity,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { api, ProcessingDocument, PipelineStatus } from "@/api";

const POLL_MS = 5000;

/** Human-readable elapsed time. Mirrors the "12 minutes" style from the spec. */
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
  TRANSLATED: {
    label: "Translated",
    badge: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
    Icon: CheckCircle2,
  },
  FAILED_TRANSLATION: {
    label: "Failed",
    badge: "bg-rose-500/15 text-rose-600 dark:text-rose-400",
    Icon: AlertCircle,
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

function PipelineBanner({ pipeline }: { pipeline: PipelineStatus }) {
  const elapsed = formatDuration(pipeline.elapsed_seconds);
  return (
    <div className="flex items-center gap-2 rounded-md border bg-card px-3 py-2 text-sm">
      {pipeline.active ? (
        <>
          <Activity className="size-4 shrink-0 text-emerald-500" />
          <span className="font-medium">Pipeline running</span>
          {elapsed && <span className="text-muted-foreground">· started {elapsed} ago</span>}
        </>
      ) : (
        <>
          <Activity className="size-4 shrink-0 text-muted-foreground" />
          <span className="text-muted-foreground">Pipeline idle — no translation run in progress</span>
        </>
      )}
    </div>
  );
}

function DocumentRow({ doc }: { doc: ProcessingDocument }) {
  const meta = metaFor(doc.status);
  const elapsed = formatDuration(doc.elapsed_seconds);
  const langs = [doc.source_language, doc.target_language].filter(Boolean).join(" → ");

  return (
    <div className="flex flex-col gap-1 rounded-lg border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
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
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pl-6 text-xs text-muted-foreground">
        {elapsed && (
          <span className="tabular-nums">
            {doc.status === "TRANSLATING" ? "Running " : ""}
            {elapsed}
            {doc.status === "TRANSLATED" ? " total" : ""}
          </span>
        )}
        {langs && <span>{langs}</span>}
        {doc.status === "QUEUED" && <span>waiting for the pipeline to pick it up</span>}
      </div>

      {doc.error && (
        <div className="mt-1 ml-6 rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-600 dark:text-rose-400">
          {doc.error}
        </div>
      )}
    </div>
  );
}

export function ProcessingStatusView() {
  const [docs, setDocs] = useState<ProcessingDocument[]>([]);
  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [loading, setLoading] = useState(true); // initial load only
  const [error, setError] = useState<string | null>(null);
  const [warehouseConfigured, setWarehouseConfigured] = useState(true);
  // Keep the latest load in a ref so the polling closure never goes stale.
  const firstLoad = useRef(true);

  useEffect(() => {
    let cancelled = false;

    const tick = () =>
      api
        .processingStatus()
        .then((r) => {
          if (cancelled) return;
          setDocs(r.documents);
          setPipeline(r.pipeline);
          setWarehouseConfigured(r.warehouse_configured);
          setError(null);
        })
        .catch((e) => {
          if (!cancelled) setError(String(e));
        })
        .finally(() => {
          if (!cancelled && firstLoad.current) {
            firstLoad.current = false;
            setLoading(false);
          }
        });

    tick(); // immediately, then poll
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h2 className="text-sm font-semibold">Processing status</h2>
          <p className="text-xs text-muted-foreground">
            Documents you've submitted that are queued or translating, plus ones that finished in the
            last 24 hours. Refreshes automatically every {POLL_MS / 1000} seconds.
          </p>
        </div>
      </div>

      {pipeline && <PipelineBanner pipeline={pipeline} />}

      {!warehouseConfigured && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
          The Delta warehouse isn't configured, so per-document status can't be read. Freshly uploaded
          files still appear as queued.
        </div>
      )}

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading ? (
        <div className="py-16 text-center text-muted-foreground">
          <Loader2 className="mx-auto animate-spin" />
        </div>
      ) : docs.length === 0 ? (
        <div className="rounded-lg border py-16 text-center text-sm text-muted-foreground">
          No documents processing. Upload a document from the Review tab and it'll appear here.
        </div>
      ) : (
        <div className="grid gap-3">
          {docs.map((d) => (
            <DocumentRow key={d.file_name} doc={d} />
          ))}
        </div>
      )}
    </div>
  );
}
