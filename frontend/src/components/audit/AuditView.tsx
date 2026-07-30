import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { api, AuditEvent } from "@/api";

export function AuditView({ pairId }: { pairId: string | null }) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!pairId) {
      setEvents([]);
      return;
    }
    setLoading(true);
    setError(null);
    api
      .audit(pairId)
      .then(setEvents)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [pairId]);

  if (!pairId) {
    return (
      <div className="py-16 text-center text-muted-foreground">
        Open a document from the Review tab, then click Audit to see its event log.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h2 className="text-sm font-semibold">
        Audit trail · <span className="text-muted-foreground">{pairId}</span>
      </h2>
      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
      {loading ? (
        <div className="py-16 text-center text-muted-foreground">
          <Loader2 className="mx-auto animate-spin" />
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2">When</th>
                <th className="px-3 py-2">Event</th>
                <th className="px-3 py-2">Actor</th>
                <th className="px-3 py-2 text-right">¶</th>
              </tr>
            </thead>
            <tbody>
              {events.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-3 py-10 text-center text-muted-foreground">
                    No audit events recorded yet.
                  </td>
                </tr>
              ) : (
                events.map((e) => (
                  <tr key={e.event_id} className="border-t">
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {new Date(e.event_at).toLocaleString()}
                    </td>
                    <td className="px-3 py-2">
                      <Badge variant="outline">{e.event_type}</Badge>
                    </td>
                    <td className="px-3 py-2">
                      {e.actor}{" "}
                      <span className="text-xs text-muted-foreground">({e.actor_type})</span>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {e.paragraph_idx ?? "—"}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
