import { useEffect, useState } from "react";
import { RefreshCw, Loader2, AlertTriangle } from "lucide-react";
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

// Re-translate a pair with a different Instruction, in place. The source
// document is unchanged; only the translation is regenerated. Resets the
// document's review state server-side (certifications + edits), so we warn.
export function RetranslateDialog({
  open,
  onOpenChange,
  pairId,
  onStarted,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  pairId: string | null;
  onStarted: () => void;
}) {
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [promptId, setPromptId] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    api
      .prompts()
      .then((ps) => {
        setPrompts(ps);
        setPromptId((cur) => (cur === "" && ps.length ? ps[0].prompt_id : cur));
      })
      .catch(() => setPrompts([]));
  }, [open]);

  const submit = async () => {
    if (!pairId || promptId === "") return;
    setBusy(true);
    setError(null);
    try {
      await api.retranslate(pairId, promptId as number);
      onStarted();
      onOpenChange(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !busy && onOpenChange(v)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Re-translate this document</DialogTitle>
          <DialogDescription>
            Pick an Instruction and re-run the translation. The source document is
            unchanged; only the translation is regenerated. Target language stays the same.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Instruction</label>
            <Select
              value={promptId === "" ? "" : String(promptId)}
              onChange={(e) => setPromptId(e.target.value ? Number(e.target.value) : "")}
            >
              <option value="" disabled>
                Select an instruction…
              </option>
              {prompts.map((p) => (
                <option key={p.prompt_id} value={p.prompt_id}>
                  {p.name}
                </option>
              ))}
            </Select>
          </div>

          <div className="flex gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
            <AlertTriangle className="size-4 shrink-0 text-amber-500" />
            <span>
              This regenerates the translation and <b>clears this document's certifications
              and edits</b> — they were made against the previous translation.
            </span>
          </div>

          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" onClick={submit} disabled={busy || promptId === ""}>
            {busy ? <Loader2 className="animate-spin" /> : <RefreshCw />} Re-translate
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
