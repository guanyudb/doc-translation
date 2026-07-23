import { useEffect, useRef, useState } from "react";
import { UploadCloud, Loader2, FileText, CheckCircle2, XCircle, Clock } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { api, DocumentStatus } from "@/api";

function StatusRow({ d }: { d: DocumentStatus }) {
  const map: Record<string, { icon: JSX.Element; cls: string; label: string }> = {
    QUEUED: { icon: <Clock className="size-3.5" />, cls: "bg-muted text-muted-foreground", label: "queued" },
    TRANSLATING: {
      icon: <Loader2 className="size-3.5 animate-spin" />,
      cls: "bg-amber-100 text-amber-900 dark:bg-amber-900/30 dark:text-amber-200",
      label: "translating",
    },
    TRANSLATED: {
      icon: <CheckCircle2 className="size-3.5" />,
      cls: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/30 dark:text-emerald-200",
      label: "translated",
    },
    FAILED_TRANSLATION: {
      icon: <XCircle className="size-3.5" />,
      cls: "bg-rose-100 text-rose-900 dark:bg-rose-900/30 dark:text-rose-200",
      label: "failed",
    },
  };
  const m = map[d.status] || map.QUEUED;
  return (
    <div className="flex items-center gap-2 border-t py-1.5 text-xs">
      <FileText className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate" title={d.file_name}>
        {d.file_name}
      </span>
      {d.target_language && <span className="text-muted-foreground">→ {d.target_language}</span>}
      <Badge className={`gap-1 ${m.cls}`}>
        {m.icon}
        {m.label}
      </Badge>
    </div>
  );
}

// A curated set of target languages the demo commonly needs. The value is the
// full English name (the pipeline slugifies it for filenames).
const LANGUAGES = [
  "English",
  "Japanese",
  "Chinese",
  "Korean",
  "Spanish",
  "French",
  "German",
  "Portuguese",
];

export function UploadDialog({
  open,
  onOpenChange,
  defaultTarget,
  onUploaded,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  defaultTarget: string;
  onUploaded: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [target, setTarget] = useState(defaultTarget || "English");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [docs, setDocs] = useState<DocumentStatus[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  // Poll pipeline status while the dialog is open so the user sees their
  // upload move queued → translating → translated without leaving the dialog.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const tick = () =>
      api
        .documents()
        .then((r) => {
          if (!cancelled) setDocs(r.documents);
        })
        .catch(() => {});
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [open]);

  const reset = () => {
    setFile(null);
    setMsg(null);
    setError(null);
    setBusy(false);
  };

  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const r = await api.upload(file, target);
      setMsg(r.message);
      setFile(null);
      onUploaded();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) reset();
        onOpenChange(v);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Upload a document for translation</DialogTitle>
          <DialogDescription>
            Drop a source <code>.docx</code>. It's translated automatically on arrival and
            appears in the review list when done (usually 1–3 min). Source language is
            auto-detected.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div
            onClick={() => inputRef.current?.click()}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              const f = e.dataTransfer.files?.[0];
              if (f) setFile(f);
            }}
            className="flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground hover:bg-accent"
          >
            {file ? (
              <>
                <FileText className="size-6 text-foreground" />
                <span className="font-medium text-foreground">{file.name}</span>
                <span className="text-xs">{(file.size / 1024).toFixed(0)} KB · click to replace</span>
              </>
            ) : (
              <>
                <UploadCloud className="size-6" />
                <span>Click or drag a .docx here</span>
              </>
            )}
            <input
              ref={inputRef}
              type="file"
              accept=".docx"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) setFile(f);
                e.target.value = "";
              }}
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Target language
            </label>
            <Select value={target} onChange={(e) => setTarget(e.target.value)}>
              {LANGUAGES.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </Select>
          </div>

          {msg && (
            <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm">
              {msg}
            </div>
          )}
          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          {docs.length > 0 && (
            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Pipeline status
                </span>
                <span className="text-[10px] text-muted-foreground">auto-refreshing</span>
              </div>
              <div className="max-h-48 overflow-y-auto rounded-md border px-2">
                {docs.map((d) => (
                  <StatusRow key={d.file_name} d={d} />
                ))}
              </div>
              {docs.some((d) => d.status === "FAILED_TRANSLATION" && d.error) && (
                <p className="mt-1 text-[11px] text-rose-600">
                  {docs.find((d) => d.status === "FAILED_TRANSLATION")?.error}
                </p>
              )}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          <Button disabled={!file || busy} onClick={submit}>
            {busy ? <Loader2 className="animate-spin" /> : <UploadCloud />}
            Upload &amp; translate
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
