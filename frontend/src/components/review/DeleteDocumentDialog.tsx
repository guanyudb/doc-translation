import { useState } from "react";
import { Trash2, Loader2, AlertTriangle } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { api } from "@/api";

// Admin-only: permanently delete a document — its source, translation, per-file
// sidecars, and Lakebase review state. Irreversible. The append-only Delta audit
// archive is preserved. Promoted/published documents are refused server-side.
export function DeleteDocumentDialog({
  open,
  onOpenChange,
  pairId,
  onDeleted,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  pairId: string | null;
  onDeleted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!pairId) return;
    setBusy(true);
    setError(null);
    try {
      await api.deletePair(pairId);
      onDeleted();
      onOpenChange(false);
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
        if (!busy) {
          setError(null);
          onOpenChange(v);
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete document</DialogTitle>
          <DialogDescription>
            Permanently delete <span className="font-medium text-foreground">{pairId}</span> — its
            source file, translation, and all review state (certifications, edits, comments). This
            cannot be undone. The immutable audit archive is preserved.
          </DialogDescription>
        </DialogHeader>
        {error && (
          <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={submit} disabled={busy || !pairId}>
            {busy ? <Loader2 className="animate-spin" /> : <Trash2 />} Delete permanently
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
