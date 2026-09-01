import { useEffect, useRef, useState } from "react";
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
import { api, Prompt } from "@/api";

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
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [promptId, setPromptId] = useState<number | "">("");
  const [existingNames, setExistingNames] = useState<Set<string>>(new Set());
  const inputRef = useRef<HTMLInputElement>(null);

  // On open: load the prompt library (required selection; pre-select if there's
  // exactly one) and the set of existing document names (to warn on a same-name
  // upload before the user commits, since pair_id is keyed to the filename).
  useEffect(() => {
    if (!open) return;
    api
      .prompts()
      .then((ps) => {
        setPrompts(ps);
        setPromptId((cur) => (cur === "" && ps.length === 1 ? ps[0].prompt_id : cur));
      })
      .catch(() => setPrompts([]));
    api
      .documents()
      .then((r) => setExistingNames(new Set(r.documents.map((d) => d.file_name))))
      .catch(() => setExistingNames(new Set()));
  }, [open]);

  const reset = () => {
    setFile(null);
    setMsg(null);
    setError(null);
    setBusy(false);
  };

  // Does the picked filename already exist? pair_id is derived from the filename
  // stem, so re-using a name would otherwise inherit the existing document's
  // review state — we surface the choice (replace vs copy) instead of guessing.
  const collision = file ? existingNames.has(file.name) : false;

  const submit = async (onConflict: "rename" | "replace" = "rename") => {
    if (!file || promptId === "") return;
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const r = await api.upload(file, target, promptId as number, onConflict);
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

          {collision && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
              <b>{file?.name}</b> already exists in the pipeline. Because a document's
              review state is keyed to its filename,{" "}
              <b>Replace existing</b> will overwrite it and clear its certifications,
              edits, and comments. <b>Upload as a copy</b> keeps both by adding a
              numbered suffix.
            </div>
          )}

          <div>
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Translation prompt <span className="text-destructive">*</span>
            </label>
            <Select
              value={promptId === "" ? "" : String(promptId)}
              onChange={(e) => setPromptId(e.target.value === "" ? "" : Number(e.target.value))}
            >
              <option value="">— select a prompt —</option>
              {prompts.map((p) => (
                <option key={p.prompt_id} value={String(p.prompt_id)}>
                  {p.name}
                </option>
              ))}
            </Select>
            {prompts.length === 0 && (
              <p className="mt-1 text-[11px] text-amber-600">
                No prompts available. Create one in the Instructions tab first.
              </p>
            )}
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

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          {collision ? (
            <>
              <Button
                variant="outline"
                disabled={busy || promptId === ""}
                onClick={() => submit("replace")}
              >
                {busy ? <Loader2 className="animate-spin" /> : null}
                Replace existing
              </Button>
              <Button disabled={busy || promptId === ""} onClick={() => submit("rename")}>
                {busy ? <Loader2 className="animate-spin" /> : <UploadCloud />}
                Upload as a copy
              </Button>
            </>
          ) : (
            <Button
              disabled={!file || promptId === "" || busy}
              onClick={() => submit("rename")}
            >
              {busy ? <Loader2 className="animate-spin" /> : <UploadCloud />}
              Upload &amp; translate
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
