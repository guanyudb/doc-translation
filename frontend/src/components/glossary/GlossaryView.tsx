import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Loader2, RefreshCw, Pickaxe, UploadCloud, ChevronRight, ChevronDown,
  Trash2, Pencil, AlertTriangle, Search,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { api, GlossaryEntry } from "@/api";

const SOURCE_STYLES: Record<string, string> = {
  tenant: "bg-blue-100 text-blue-900 dark:bg-blue-900/30 dark:text-blue-200",
  seed: "bg-violet-100 text-violet-900 dark:bg-violet-900/30 dark:text-violet-200",
  customer: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/30 dark:text-emerald-200",
};
const CHECKBOX = "size-4 shrink-0 cursor-pointer accent-primary disabled:opacity-50";

export function GlossaryView({ deltaSyncEnabled }: { deltaSyncEnabled: boolean }) {
  const [entries, setEntries] = useState<GlossaryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [langFilter, setLangFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [importOpen, setImportOpen] = useState(false);
  const [importName, setImportName] = useState("");
  const [importFile, setImportFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    setLoading(true);
    api
      .glossary() // all lists, approved + not, so the UI can toggle
      .then(setEntries)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  const run = async (label: string, fn: () => Promise<string>) => {
    setBusy(label); setError(null); setMsg(null);
    try { setMsg(await fn()); load(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(null); }
  };

  // ---- derive lists + filters from the flat entry list -----------------
  const lists = useMemo(() => {
    const m = new Map<string, GlossaryEntry[]>();
    for (const e of entries) {
      const k = e.list_name || "(unnamed)";
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(e);
    }
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [entries]);

  const langPairs = useMemo(() => {
    const s = new Set<string>();
    for (const e of entries) s.add(`${e.source_lang} → ${e.target_lang}`);
    return [...s].sort();
  }, [entries]);

  const conflictCount = useMemo(() => entries.filter((e) => e.conflict).length, [entries]);
  const approvedCount = useMemo(() => entries.filter((e) => e.approved).length, [entries]);
  const filtering = Boolean(search || langFilter);

  const matches = (e: GlossaryEntry) => {
    if (langFilter && `${e.source_lang} → ${e.target_lang}` !== langFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return e.model_phrase.toLowerCase().includes(q) || e.correction.toLowerCase().includes(q);
    }
    return true;
  };

  const toggleExpand = (name: string) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      n.has(name) ? n.delete(name) : n.add(name);
      return n;
    });

  const toggleList = (name: string, es: GlossaryEntry[]) => {
    const allApproved = es.every((e) => e.approved);
    run(`list-${name}`, async () => {
      const r = await api.approveGlossaryBatch({ list_name: name, approved: !allApproved });
      return `${!allApproved ? "Approved" : "Unapproved"} ${r.updated} entries in "${name}".`;
    });
  };

  const toggleEntry = (e: GlossaryEntry) =>
    run(`e-${e.entry_id}`, async () => {
      await api.approveGlossary(e.entry_id, !e.approved);
      return e.approved ? "Entry unapproved." : "Entry approved.";
    });

  const doDelete = (name: string) => {
    if (!window.confirm(`Delete the entire "${name}" list and all its terms? This can't be undone.`)) return;
    run(`del-${name}`, async () => {
      const r = await api.deleteGlossaryList(name);
      return `Deleted list "${name}" (${r.deleted} terms).`;
    });
  };

  const doRename = (name: string) => {
    const nn = window.prompt(`Rename list "${name}" to:`, name);
    if (!nn || !nn.trim() || nn.trim() === name) return;
    run(`ren-${name}`, async () => {
      await api.renameGlossaryList(name, nn.trim());
      return `Renamed "${name}" → "${nn.trim()}".`;
    });
  };

  const doImport = () => {
    if (!importFile) return;
    run("import", async () => {
      const r = await api.importGlossary(importFile, importName.trim() || undefined);
      setImportOpen(false); setImportFile(null); setImportName("");
      return `Imported ${r.imported} entries into "${r.list_name}".`;
    });
  };

  return (
    <div className="space-y-4">
      {/* ---- toolbar ---- */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search terms…"
            className="h-9 w-56 rounded-md border border-input bg-background pl-8 pr-3 text-sm"
          />
        </div>
        <Select className="max-w-[200px]" value={langFilter} onChange={(e) => setLangFilter(e.target.value)}>
          <option value="">all languages</option>
          {langPairs.map((lp) => <option key={lp} value={lp}>{lp}</option>)}
        </Select>
        <div className="flex-1" />
        <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => setImportOpen(true)}>
          {busy === "import" ? <Loader2 className="animate-spin" /> : <UploadCloud />} Import CSV
        </Button>
        <Button
          variant="outline" size="sm" disabled={busy !== null}
          onClick={() => run("mine", async () => `Mined ${(await api.mineGlossary()).mined} entries from reviewer edits.`)}
        >
          {busy === "mine" ? <Loader2 className="animate-spin" /> : <Pickaxe />} Mine from edits
        </Button>
        <Button
          variant="default" size="sm" disabled={busy !== null || !deltaSyncEnabled}
          title={deltaSyncEnabled ? "" : "No SQL warehouse configured"}
          onClick={() => run("sync", async () => {
            const r = await api.syncGlossary();
            return r.skipped ? "Delta sync skipped (no warehouse)." : `Synced ${r.rows} entries to Delta.`;
          })}
        >
          {busy === "sync" ? <Loader2 className="animate-spin" /> : <RefreshCw />} Sync to Delta
        </Button>
      </div>

      {/* ---- summary chips ---- */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline">{approvedCount} approved term{approvedCount === 1 ? "" : "s"} active</Badge>
        <Badge variant="outline">{lists.length} list{lists.length === 1 ? "" : "s"}</Badge>
        {conflictCount > 0 && (
          <Badge className="bg-amber-500/15 text-amber-600 dark:text-amber-400">
            <AlertTriangle className="mr-1 size-3" /> {conflictCount} conflicting term{conflictCount === 1 ? "" : "s"}
          </Badge>
        )}
        <span>Approved terms are injected into the translation prompt. Remember to <b>Sync to Delta</b> after changes.</span>
      </div>

      {msg && <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm">{msg}</div>}
      {error && <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}

      {/* ---- lists (layer 1) → words (layer 2) ---- */}
      {loading ? (
        <div className="py-10 text-center text-muted-foreground"><Loader2 className="mx-auto animate-spin" /></div>
      ) : lists.length === 0 ? (
        <div className="rounded-lg border py-10 text-center text-sm text-muted-foreground">
          No glossary entries yet. Import a CSV, mine from edits, or enable the seed glossary.
        </div>
      ) : (
        <div className="space-y-2">
          {lists.map(([name, es]) => {
            const visible = filtering ? es.filter(matches) : es;
            if (filtering && visible.length === 0) return null;
            const allApproved = es.every((e) => e.approved);
            const someApproved = es.some((e) => e.approved);
            const isOpen = expanded.has(name) || filtering;
            const listConflicts = es.filter((e) => e.conflict).length;
            const approvedInList = es.filter((e) => e.approved).length;
            return (
              <div key={name} className="overflow-hidden rounded-lg border">
                <div className="flex items-center gap-2.5 bg-muted/40 px-3 py-2">
                  <button onClick={() => toggleExpand(name)} className="text-muted-foreground hover:text-foreground" title={isOpen ? "Collapse" : "Expand"}>
                    {isOpen ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                  </button>
                  <input
                    type="checkbox" className={CHECKBOX} disabled={busy !== null}
                    checked={allApproved}
                    ref={(el) => { if (el) el.indeterminate = someApproved && !allApproved; }}
                    onChange={() => toggleList(name, es)}
                    title="Approve / unapprove all terms in this list"
                  />
                  <span className="truncate font-medium" title={name}>{name}</span>
                  <Badge className={SOURCE_STYLES[es[0].source] ?? ""}>{es[0].source}</Badge>
                  <span className="text-xs tabular-nums text-muted-foreground">{approvedInList}/{es.length} approved</span>
                  {listConflicts > 0 && (
                    <Badge className="bg-amber-500/15 text-amber-600 dark:text-amber-400">
                      <AlertTriangle className="mr-1 size-3" /> {listConflicts}
                    </Badge>
                  )}
                  <div className="flex-1" />
                  <Button size="icon-sm" variant="ghost" disabled={busy !== null} onClick={() => doRename(name)} title="Rename list"><Pencil /></Button>
                  <Button size="icon-sm" variant="ghost" disabled={busy !== null} onClick={() => doDelete(name)} title="Delete list"><Trash2 /></Button>
                </div>

                {isOpen && (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead className="bg-muted/20 text-left text-xs text-muted-foreground">
                        <tr>
                          <th className="w-8 px-3 py-1.5" />
                          <th className="px-3 py-1.5">Source phrase</th>
                          <th className="px-3 py-1.5">Required translation</th>
                          <th className="px-3 py-1.5">Langs</th>
                          <th className="px-3 py-1.5 text-right">Seen</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visible.map((e) => (
                          <tr key={e.entry_id} className={`border-t ${e.conflict ? "bg-amber-500/5" : ""}`}>
                            <td className="px-3 py-1.5">
                              <input type="checkbox" className={CHECKBOX} disabled={busy !== null}
                                checked={e.approved} onChange={() => toggleEntry(e)}
                                title={e.approved ? "Approved — uncheck to disable" : "Not approved — check to enable"} />
                            </td>
                            <td className="px-3 py-1.5">{e.model_phrase}</td>
                            <td className="px-3 py-1.5 font-medium">
                              {e.correction}
                              {e.conflict && (
                                <span title="Conflicts with another approved term for the same source phrase">
                                  <AlertTriangle className="ml-1.5 inline size-3.5 text-amber-500" />
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-1.5 text-xs text-muted-foreground">{e.source_lang} → {e.target_lang}</td>
                            <td className="px-3 py-1.5 text-right tabular-nums">{e.occurrences}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* ---- import dialog ---- */}
      <Dialog open={importOpen} onOpenChange={(v) => !busy && setImportOpen(v)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Import a glossary list</DialogTitle>
            <DialogDescription>
              CSV columns (order-free): <code>source_lang, target_lang, model_phrase, correction</code>.
              Entries land in a named list you can enable/disable or delete as a unit.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">List name</label>
              <input
                value={importName}
                onChange={(e) => setImportName(e.target.value)}
                placeholder={importFile ? importFile.name.replace(/\.csv$/i, "") : "e.g. Oncology terms"}
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              />
            </div>
            <input ref={fileRef} type="file" accept=".csv" className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) { setImportFile(f); if (!importName) setImportName(f.name.replace(/\.csv$/i, "")); } e.target.value = ""; }} />
            <div onClick={() => fileRef.current?.click()}
              className="flex cursor-pointer flex-col items-center gap-1 rounded-lg border border-dashed p-5 text-center text-sm text-muted-foreground hover:bg-accent">
              <UploadCloud className="size-5" />
              <span>{importFile ? importFile.name : "Click to choose a .csv"}</span>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setImportOpen(false)} disabled={busy !== null}>Cancel</Button>
            <Button size="sm" onClick={doImport} disabled={busy !== null || !importFile}>
              {busy === "import" ? <Loader2 className="animate-spin" /> : <UploadCloud />} Import
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
