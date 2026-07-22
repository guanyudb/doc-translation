import { useRef, useState } from "react";
import { UploadCloud, Loader2, FileText } from "lucide-react";
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
import { api } from "@/api";

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
  const inputRef = useRef<HTMLInputElement>(null);

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
