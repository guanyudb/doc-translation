// Typed client for the Doc Translation Review API. All endpoints are served
// by the FastAPI backend (server_api.py) under /api.

export type Lifecycle = "UNDER_REVIEW" | "PROMOTING" | "PUBLISHED" | "ARCHIVED";
export type ParaStatus = "pending" | "certified" | "flagged";

export interface PairSummary {
  pair_id: string;
  original_path: string;
  translated_path: string;
  source_lang: string | null;
  target_lang: string | null;
  total_paragraphs: number;
  lifecycle_state: Lifecycle;
  locked: boolean;
  certified: number;
  flagged: number;
  pending: number;
}

export interface Paragraph {
  idx: number;
  page: number;
  source: string;
  translated: string;
  status: ParaStatus;
  comment: string | null;
  edited_text: string | null;
  reviewer: string | null;
  confidence: number | null;
  confidence_flags: string[];
}

export interface PairDetail {
  pair_id: string;
  original_path: string;
  translated_path: string;
  source_lang: string | null;
  target_lang: string | null;
  lifecycle_state: Lifecycle;
  locked: boolean;
  paragraphs: Paragraph[];
}

export interface AuditEvent {
  event_id: number;
  event_type: string;
  actor: string;
  actor_type: string;
  paragraph_idx: number | null;
  event_at: string;
}

export interface GlossaryEntry {
  entry_id: number;
  source_lang: string;
  target_lang: string;
  model_phrase: string;
  correction: string;
  occurrences: number;
  distinct_reviewers: number;
  approved: boolean;
  source: string;
  last_seen_at: string;
}

export interface AppConfig {
  reviewer: string;
  target_language: string;
  delta_sync_enabled: boolean;
}

export interface DocumentStatus {
  file_name: string;
  status: string; // QUEUED | TRANSLATING | TRANSLATED | FAILED_TRANSLATION
  target_language: string | null;
  source_language: string | null;
  started_at: string | null;
  ended_at: string | null;
  error: string | null;
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${body ? `: ${body}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  config: () => fetch("/api/config").then(j<AppConfig>),

  pairs: () => fetch("/api/pairs").then(j<PairSummary[]>),

  pair: (id: string) => fetch(`/api/pairs/${encodeURIComponent(id)}`).then(j<PairDetail>),

  // HTML preview for one side ("original" | "translated"). Returns raw HTML.
  previewUrl: (id: string, side: "original" | "translated") =>
    `/api/pairs/${encodeURIComponent(id)}/preview/${side}`,

  preview: (id: string, side: "original" | "translated") =>
    fetch(api.previewUrl(id, side)).then((r) => r.text()),

  setStatus: (id: string, idx: number, status: ParaStatus) =>
    fetch(`/api/pairs/${encodeURIComponent(id)}/paragraphs/${idx}/status`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ status }),
    }).then(j<Paragraph>),

  setComment: (id: string, idx: number, comment: string) =>
    fetch(`/api/pairs/${encodeURIComponent(id)}/paragraphs/${idx}/comment`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ comment }),
    }).then(j<Paragraph>),

  setEdit: (id: string, idx: number, edited_text: string | null) =>
    fetch(`/api/pairs/${encodeURIComponent(id)}/paragraphs/${idx}/edit`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ edited_text }),
    }).then(j<Paragraph>),

  certifyAll: (id: string) =>
    fetch(`/api/pairs/${encodeURIComponent(id)}/certify-all`, { method: "POST" }).then(
      j<{ certified: number }>
    ),

  certifyPage: (id: string, page: number) =>
    fetch(`/api/pairs/${encodeURIComponent(id)}/certify-page`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ page }),
    }).then(j<{ certified: number; page: number }>),

  upload: (file: File, targetLanguage: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("target_language", targetLanguage);
    return fetch("/api/upload", { method: "POST", body: fd }).then(
      j<{ ok: boolean; name: string; message: string; target_language: string }>
    );
  },

  documents: () =>
    fetch("/api/documents").then(j<{ documents: DocumentStatus[]; warehouse_configured: boolean }>),

  publish: (id: string) =>
    fetch(`/api/pairs/${encodeURIComponent(id)}/publish`, { method: "POST" }).then(
      j<{ output_path: string; edits_applied: number; version: number }>
    ),

  promote: (id: string) =>
    fetch(`/api/pairs/${encodeURIComponent(id)}/promote`, { method: "POST" }).then(
      j<{ ok: boolean; delta_synced: boolean; message: string }>
    ),

  audit: (id: string) =>
    fetch(`/api/pairs/${encodeURIComponent(id)}/audit`).then(j<AuditEvent[]>),

  glossary: (opts?: { source?: string; approved?: boolean }) => {
    const q = new URLSearchParams();
    if (opts?.source) q.set("source", opts.source);
    if (opts?.approved !== undefined) q.set("approved", String(opts.approved));
    const qs = q.toString();
    return fetch(`/api/glossary${qs ? `?${qs}` : ""}`).then(j<GlossaryEntry[]>);
  },

  approveGlossary: (id: number, approved: boolean) =>
    fetch(`/api/glossary/${id}/approve`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ approved }),
    }).then(j<GlossaryEntry>),

  importGlossary: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch("/api/glossary/import", { method: "POST", body: fd }).then(
      j<{ imported: number }>
    );
  },

  mineGlossary: () =>
    fetch("/api/glossary/mine", { method: "POST" }).then(j<{ mined: number }>),

  syncGlossary: () =>
    fetch("/api/glossary/sync-delta", { method: "POST" }).then(
      j<{ rows: number; skipped: boolean }>
    ),
};
