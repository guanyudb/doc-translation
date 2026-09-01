import { useCallback, useEffect, useState } from "react";
import { Loader2, Plus, Copy, Pencil, Trash2, RotateCcw } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import { api, Prompt } from "@/api";

const MAX_BODY_LEN = 8000;
const MAX_NAME_LEN = 200;

export function InstructionsView() {
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Editor dialog. `editing === null` while closed; a Prompt for edit; or the
  // sentinel {prompt_id: 0} for a fresh create.
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null); // null = create
  const [fName, setFName] = useState("");
  const [fDesc, setFDesc] = useState("");
  const [fBody, setFBody] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [template, setTemplate] = useState<string>("");

  const load = useCallback(() => {
    setLoading(true);
    api
      .prompts()
      .then(setPrompts)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Fetch the default template once so "New" can pre-fill and "Reset" works.
  useEffect(() => {
    api
      .promptTemplate()
      .then((r) => setTemplate(r.body))
      .catch(() => setTemplate(""));
  }, []);

  const run = async (label: string, fn: () => Promise<string>) => {
    setBusy(label);
    setError(null);
    setMsg(null);
    try {
      setMsg(await fn());
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const openCreate = () => {
    setEditingId(null);
    setFName("");
    setFDesc("");
    setFBody(template); // seeded-editable: start from the default template
    setFormError(null);
    setEditorOpen(true);
  };

  const openEdit = (p: Prompt) => {
    setEditingId(p.prompt_id);
    setFName(p.name);
    setFDesc(p.description ?? "");
    setFBody(p.body);
    setFormError(null);
    setEditorOpen(true);
  };

  const saveEditor = async () => {
    const name = fName.trim();
    const body = fBody.trim();
    if (!name || !body) {
      setFormError("Name and prompt body are required.");
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const payload = { name, body, description: fDesc.trim() || null };
      if (editingId === null) {
        const p = await api.createPrompt(payload);
        setMsg(`Created prompt “${p.name}”.`);
      } else {
        const p = await api.updatePrompt(editingId, payload);
        setMsg(`Saved prompt “${p.name}”.`);
      }
      setEditorOpen(false);
      setError(null);
      load();
    } catch (e) {
      setFormError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h2 className="text-sm font-semibold">Translation prompts</h2>
          <p className="text-xs text-muted-foreground">
            The system prompt the model receives. Pick one per document at upload — its text is
            frozen with that document, so editing or deleting a prompt here never changes what a
            past document was translated with.
          </p>
        </div>
        <div className="flex-1" />
        <Button size="sm" disabled={busy !== null} onClick={openCreate}>
          <Plus />
          New prompt
        </Button>
      </div>

      {msg && <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm">{msg}</div>}
      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading ? (
        <div className="py-16 text-center text-muted-foreground">
          <Loader2 className="mx-auto animate-spin" />
        </div>
      ) : prompts.length === 0 ? (
        <div className="rounded-lg border py-16 text-center text-sm text-muted-foreground">
          No prompts yet. Create one to get started — new prompts start from the built-in default.
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {prompts.map((p) => (
            <div key={p.prompt_id} className="flex flex-col rounded-lg border bg-card p-4">
              <div className="mb-1 flex items-start justify-between gap-2">
                <h3 className="font-medium leading-tight">{p.name}</h3>
              </div>
              {p.description && (
                <p className="mb-2 text-xs text-muted-foreground">{p.description}</p>
              )}
              <pre className="mb-3 max-h-32 flex-1 overflow-y-auto whitespace-pre-wrap rounded-md bg-muted/50 p-2 text-[11px] leading-snug text-muted-foreground">
                {p.body}
              </pre>
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-[10px] text-muted-foreground" title={p.updated_by ?? ""}>
                  {p.updated_by ? `edited by ${p.updated_by}` : ""}
                </span>
                <div className="flex gap-1">
                  <Button size="icon-sm" variant="outline" title="Edit" onClick={() => openEdit(p)}>
                    <Pencil />
                  </Button>
                  <Button
                    size="icon-sm"
                    variant="outline"
                    title="Clone"
                    disabled={busy !== null}
                    onClick={() =>
                      run(`clone-${p.prompt_id}`, async () => {
                        const c = await api.clonePrompt(p.prompt_id);
                        return `Cloned “${p.name}” → “${c.name}”.`;
                      })
                    }
                  >
                    <Copy />
                  </Button>
                  <Button
                    size="icon-sm"
                    variant="destructive"
                    title="Delete"
                    disabled={busy !== null}
                    onClick={() => {
                      if (!window.confirm(`Delete prompt “${p.name}”? This can't be undone.`)) return;
                      run(`delete-${p.prompt_id}`, async () => {
                        await api.deletePrompt(p.prompt_id);
                        return `Deleted “${p.name}”.`;
                      });
                    }}
                  >
                    <Trash2 />
                  </Button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create / edit editor */}
      <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{editingId === null ? "New prompt" : "Edit prompt"}</DialogTitle>
            <DialogDescription>
              The prompt body fully replaces the built-in system prompt at translation time. Keep the{" "}
              <code>{"{lang}"}</code> token — it's replaced with the document's target language. Glossary
              terms are still appended automatically.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div>
              <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Name
              </label>
              <Input
                value={fName}
                maxLength={MAX_NAME_LEN}
                placeholder="e.g. Legal contracts (formal)"
                onChange={(e) => setFName(e.target.value)}
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Description <span className="font-normal normal-case">(optional)</span>
              </label>
              <Input
                value={fDesc}
                placeholder="Short note on when to use this prompt"
                onChange={(e) => setFDesc(e.target.value)}
              />
            </div>
            <div>
              <div className="mb-1 flex items-center justify-between">
                <label className="block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Prompt body
                </label>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] text-muted-foreground tabular-nums">
                    {fBody.length}/{MAX_BODY_LEN}
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={!template}
                    title="Replace the body with the built-in default template"
                    onClick={() => setFBody(template)}
                  >
                    <RotateCcw />
                    Reset to default
                  </Button>
                </div>
              </div>
              <Textarea
                value={fBody}
                maxLength={MAX_BODY_LEN}
                rows={14}
                className="min-h-[280px] font-mono text-xs"
                placeholder="You are a professional translator. Translate the user's text to {lang}…"
                onChange={(e) => setFBody(e.target.value)}
              />
            </div>

            {formError && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                {formError}
              </div>
            )}
          </div>

          <DialogFooter>
            <Button variant="ghost" onClick={() => setEditorOpen(false)}>
              Cancel
            </Button>
            <Button disabled={saving || !fName.trim() || !fBody.trim()} onClick={saveEditor}>
              {saving ? <Loader2 className="animate-spin" /> : null}
              {editingId === null ? "Create prompt" : "Save changes"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
