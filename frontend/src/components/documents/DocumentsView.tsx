import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, RefreshCw, Search, Trash2, ArrowUpDown, ArrowUp, ArrowDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/input";
import { api, PairSummary } from "@/api";

// Sortable columns. `progress` sorts by certified fraction.
type SortKey = "name" | "langs" | "status" | "words" | "progress" | "flagged";

const pct = (n: number, total: number) => (total ? Math.round((n / total) * 100) : 0);
const langOf = (p: PairSummary) => `${p.source_lang ?? "?"} → ${p.target_lang ?? "?"}`;

/**
 * Scalable browse surface for every document pair — a searchable, filterable,
 * sortable table. Complements (does not replace) the in-review dropdown, which
 * doesn't scale past a handful of files. Click a row to open it in Review.
 */
export function DocumentsView({
  isAdmin = false,
  onOpen,
}: {
  isAdmin?: boolean;
  onOpen: (pairId: string) => void;
}) {
  const [pairs, setPairs] = useState<PairSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [langFilter, setLangFilter] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "name", dir: "asc" });

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api.pairs().then(setPairs).catch((e) => setError(String(e))).finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  const statuses = useMemo(
    () => [...new Set(pairs.map((p) => p.lifecycle_state))].sort(),
    [pairs]
  );
  const langPairs = useMemo(() => [...new Set(pairs.map(langOf))].sort(), [pairs]);

  const toggleSort = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    const rows = pairs.filter((p) => {
      if (q && !p.pair_id.toLowerCase().includes(q)) return false;
      if (statusFilter && p.lifecycle_state !== statusFilter) return false;
      if (langFilter && langOf(p) !== langFilter) return false;
      return true;
    });
    const dir = sort.dir === "asc" ? 1 : -1;
    const key = (p: PairSummary): number | string => {
      switch (sort.key) {
        case "langs": return langOf(p);
        case "status": return p.lifecycle_state;
        case "words": return p.total_words ?? -1;
        case "progress": return p.total_paragraphs ? p.certified / p.total_paragraphs : 0;
        case "flagged": return p.flagged;
        default: return p.pair_id.toLowerCase();
      }
    };
    return [...rows].sort((a, b) => {
      const ka = key(a), kb = key(b);
      return (ka < kb ? -1 : ka > kb ? 1 : 0) * dir;
    });
  }, [pairs, search, statusFilter, langFilter, sort]);

  const doDelete = (p: PairSummary, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm(`Permanently delete "${p.pair_id}" — its source, translation, and all review state? This can't be undone.`)) return;
    setBusy(`del-${p.pair_id}`);
    setError(null);
    api
      .deletePair(p.pair_id)
      .then(() => load())
      .catch((err) => setError(String(err)))
      .finally(() => setBusy(null));
  };

  const filtering = Boolean(search || statusFilter || langFilter);

  const SortHeader = ({ col, label, right }: { col: SortKey; label: string; right?: boolean }) => (
    <th className={`px-3 py-2 ${right ? "text-right" : ""}`}>
      <button
        onClick={() => toggleSort(col)}
        className={`inline-flex items-center gap-1 hover:text-foreground ${right ? "flex-row-reverse" : ""}`}
      >
        {label}
        {sort.key === col
          ? (sort.dir === "asc" ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)
          : <ArrowUpDown className="size-3 opacity-40" />}
      </button>
    </th>
  );

  return (
    <div className="space-y-4">
      {/* ---- toolbar ---- */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search documents…"
            className="h-9 w-64 rounded-md border border-input bg-background pl-8 pr-3 text-sm"
          />
        </div>
        <Select className="max-w-[190px]" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">all statuses</option>
          {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
        </Select>
        <Select className="max-w-[190px]" value={langFilter} onChange={(e) => setLangFilter(e.target.value)}>
          <option value="">all languages</option>
          {langPairs.map((lp) => <option key={lp} value={lp}>{lp}</option>)}
        </Select>
        <div className="flex-1" />
        <Button variant="ghost" size="icon-sm" onClick={load} title="Refresh list">
          <RefreshCw />
        </Button>
      </div>

      {/* ---- summary ---- */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline">
          {filtering ? `${visible.length} of ${pairs.length}` : pairs.length} document{pairs.length === 1 ? "" : "s"}
        </Badge>
        <span>Click a document to open it for review.</span>
      </div>

      {error && <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}

      {/* ---- table ---- */}
      {loading ? (
        <div className="py-16 text-center text-muted-foreground"><Loader2 className="mx-auto animate-spin" /></div>
      ) : pairs.length === 0 ? (
        <div className="rounded-lg border py-16 text-center text-sm text-muted-foreground">
          No documents yet. Upload a <code>.docx</code> or <code>.pdf</code> from the Review tab.
        </div>
      ) : visible.length === 0 ? (
        <div className="rounded-lg border py-12 text-center text-sm text-muted-foreground">No documents match your filters.</div>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
              <tr>
                <SortHeader col="name" label="Document" />
                <SortHeader col="langs" label="Languages" />
                <SortHeader col="status" label="Status" />
                <SortHeader col="words" label="Words" right />
                <SortHeader col="progress" label="Certified" />
                <SortHeader col="flagged" label="Flagged" right />
                {isAdmin && <th className="w-10 px-3 py-2" />}
              </tr>
            </thead>
            <tbody>
              {visible.map((p) => (
                <tr
                  key={p.pair_id}
                  onClick={() => onOpen(p.pair_id)}
                  className="cursor-pointer border-t hover:bg-accent"
                >
                  <td className="max-w-[420px] truncate px-3 py-2 font-medium" title={p.pair_id}>{p.pair_id}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{langOf(p)}</td>
                  <td className="px-3 py-2">
                    <Badge className={`status-${p.lifecycle_state.toLowerCase()}`}>{p.lifecycle_state}</Badge>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-xs text-muted-foreground" title="Approximate word count of the translated document (available once opened)">
                    {p.total_words != null ? p.total_words.toLocaleString() : "—"}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <div className="flex h-1.5 w-24 overflow-hidden rounded-full bg-muted">
                        <div className="bg-emerald-500 transition-all" style={{ width: `${pct(p.certified, p.total_paragraphs)}%` }} />
                        <div className="bg-rose-500 transition-all" style={{ width: `${pct(p.flagged, p.total_paragraphs)}%` }} />
                      </div>
                      <span className="tabular-nums text-xs text-muted-foreground">
                        {pct(p.certified, p.total_paragraphs)}%
                        <span className="ml-1 opacity-70">({p.certified}/{p.total_paragraphs})</span>
                      </span>
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {p.flagged > 0 ? <span className="text-rose-600">⚑ {p.flagged}</span> : <span className="text-muted-foreground">—</span>}
                  </td>
                  {isAdmin && (
                    <td className="px-3 py-2 text-right">
                      <Button
                        size="icon-sm"
                        variant="ghost"
                        className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                        disabled={busy !== null || p.locked}
                        title={p.locked ? "Promoted/published documents are protected" : "Delete document (admin)"}
                        onClick={(e) => doDelete(p, e)}
                      >
                        {busy === `del-${p.pair_id}` ? <Loader2 className="animate-spin" /> : <Trash2 />}
                      </Button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
