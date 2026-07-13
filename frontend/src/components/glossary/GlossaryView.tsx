import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, RefreshCw, Pickaxe, UploadCloud, Check, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/input";
import { api, GlossaryEntry } from "@/api";

const SOURCE_STYLES: Record<string, string> = {
  tenant: "bg-blue-100 text-blue-900 dark:bg-blue-900/30 dark:text-blue-200",
  seed: "bg-violet-100 text-violet-900 dark:bg-violet-900/30 dark:text-violet-200",
  customer: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/30 dark:text-emerald-200",
};

export function GlossaryView({ deltaSyncEnabled }: { deltaSyncEnabled: boolean }) {
  const [entries, setEntries] = useState<GlossaryEntry[]>([]);
  const [sourceFilter, setSourceFilter] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    setLoading(true);
    api
      .glossary(sourceFilter ? { source: sourceFilter } : undefined)
      .then(setEntries)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [sourceFilter]);

  useEffect(() => {
    load();
  }, [load]);

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

  const onImport = (file: File) =>
    run("import", async () => {
      const r = await api.importGlossary(file);
      return `Imported ${r.imported} customer entries.`;
    });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Select className="max-w-[200px]" value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)}>
          <option value="">all sources</option>
          <option value="tenant">tenant (mined)</option>
          <option value="seed">seed (shipped)</option>
          <option value="customer">customer (imported)</option>
        </Select>
        <div className="flex-1" />

        <input
          ref={fileRef}
          type="file"
          accept=".csv"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onImport(f);
            e.target.value = "";
          }}
        />
        <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => fileRef.current?.click()}>
          {busy === "import" ? <Loader2 className="animate-spin" /> : <UploadCloud />}
          Import CSV
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={busy !== null}
          onClick={() =>
            run("mine", async () => {
              const r = await api.mineGlossary();
              return `Mined ${r.mined} entries from reviewer edits.`;
            })
          }
        >
          {busy === "mine" ? <Loader2 className="animate-spin" /> : <Pickaxe />}
          Mine from edits
        </Button>
        <Button
          variant="default"
          size="sm"
          disabled={busy !== null || !deltaSyncEnabled}
          title={deltaSyncEnabled ? "" : "No SQL warehouse configured"}
          onClick={() =>
            run("sync", async () => {
              const r = await api.syncGlossary();
              return r.skipped ? "Delta sync skipped (no warehouse)." : `Synced ${r.rows} entries to Delta.`;
            })
          }
        >
          {busy === "sync" ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          Sync to Delta
        </Button>
      </div>

      {msg && <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm">{msg}</div>}
      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        Entries with source <b>tenant</b> are mined from reviewer corrections; <b>seed</b> ships with the app;{" "}
        <b>customer</b> is imported. Approved entries are injected into the translation prompt (exact-match) so the
        next translation uses the required terminology automatically. Remember to <b>Sync to Delta</b> after changes —
        the translation pipeline reads the Delta mirror.
      </p>

      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-2">Source phrase</th>
              <th className="px-3 py-2">Required translation</th>
              <th className="px-3 py-2">Langs</th>
              <th className="px-3 py-2">Origin</th>
              <th className="px-3 py-2 text-right">Seen</th>
              <th className="px-3 py-2 text-right">Reviewers</th>
              <th className="px-3 py-2 text-center">Approved</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={7} className="px-3 py-10 text-center text-muted-foreground">
                  <Loader2 className="mx-auto animate-spin" />
                </td>
              </tr>
            ) : entries.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-3 py-10 text-center text-muted-foreground">
                  No glossary entries. Mine from edits, import a CSV, or enable the seed glossary.
                </td>
              </tr>
            ) : (
              entries.map((e) => (
                <tr key={e.entry_id} className="border-t">
                  <td className="px-3 py-2">{e.model_phrase}</td>
                  <td className="px-3 py-2 font-medium">{e.correction}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {e.source_lang} → {e.target_lang}
                  </td>
                  <td className="px-3 py-2">
                    <Badge className={SOURCE_STYLES[e.source] ?? ""}>{e.source}</Badge>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">{e.occurrences}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{e.distinct_reviewers}</td>
                  <td className="px-3 py-2 text-center">
                    <Button
                      size="icon-sm"
                      variant={e.approved ? "success" : "outline"}
                      onClick={() =>
                        run(`approve-${e.entry_id}`, async () => {
                          await api.approveGlossary(e.entry_id, !e.approved);
                          return e.approved ? "Entry unapproved." : "Entry approved.";
                        })
                      }
                      title={e.approved ? "Approved — click to disable" : "Not approved — click to approve"}
                    >
                      {e.approved ? <Check /> : <X />}
                    </Button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
