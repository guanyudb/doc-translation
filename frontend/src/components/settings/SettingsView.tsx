import { useEffect, useState } from "react";
import { Loader2, Save, CheckCircle2, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { api, AppSettings } from "@/api";

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

// Settings / first-run setup. Rendered as the Settings tab normally, and as a
// gate before the app unlocks when it hasn't been configured yet (firstRun).
export function SettingsView({
  firstRun = false,
  onSaved,
}: {
  firstRun?: boolean;
  onSaved?: (s: AppSettings) => void;
}) {
  const [loaded, setLoaded] = useState<AppSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [model, setModel] = useState("");
  const [target, setTarget] = useState("English");
  const [title, setTitle] = useState("");
  const [logoUrl, setLogoUrl] = useState("");
  const [logoAlt, setLogoAlt] = useState("");

  const hydrate = (v: AppSettings) => {
    setLoaded(v);
    setModel(v.model_endpoint);
    setTarget(v.target_language);
    setTitle(v.app_title);
    setLogoUrl(v.logo_url ?? "");
    setLogoAlt(v.logo_alt ?? "");
  };

  useEffect(() => {
    api.settings().then(hydrate).catch((e) => setError(String(e)));
  }, []);

  const save = async () => {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const v = await api.saveSettings({
        model_endpoint: model.trim(),
        target_language: target,
        app_title: title.trim(),
        logo_url: logoUrl.trim(),
        logo_alt: logoAlt.trim(),
      });
      hydrate(v);
      setMsg("Settings saved.");
      onSaved?.(v);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!loaded && !error) {
    return (
      <div className="py-16 text-center text-muted-foreground">
        <Loader2 className="mx-auto animate-spin" />
      </div>
    );
  }

  const modelMissing = !model.trim();

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h2 className="text-lg font-semibold">
          {firstRun ? "Welcome — set up your app" : "Settings"}
        </h2>
        <p className="text-sm text-muted-foreground">
          {firstRun
            ? "Confirm these before you start reviewing. Everything here can be changed later from the Settings tab."
            : "Runtime configuration. Model + default language apply to translations started after saving; branding updates the header immediately."}
        </p>
        {loaded?.updated_at && !firstRun && (
          <p className="mt-1 text-xs text-muted-foreground">
            Last updated {new Date(loaded.updated_at).toLocaleString()}
            {loaded.updated_by ? ` by ${loaded.updated_by}` : ""}.
          </p>
        )}
      </div>

      {/* Model */}
      <section className="space-y-2 rounded-lg border bg-card p-4">
        <label className="block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Translation model — FMAPI / AI Gateway endpoint
        </label>
        <Input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="databricks-claude-sonnet-4-6"
        />
        <p className="text-xs text-muted-foreground">
          The serving endpoint the translation pipeline calls. Applies to documents
          translated after you save (in-flight runs keep their current model).
        </p>
      </section>

      {/* Default target language */}
      <section className="space-y-2 rounded-lg border bg-card p-4">
        <label className="block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Default target language
        </label>
        <Select value={target} onChange={(e) => setTarget(e.target.value)}>
          {LANGUAGES.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </Select>
        <p className="text-xs text-muted-foreground">
          Pre-selected at upload. Source language is auto-detected; each upload can
          still pick a different target.
        </p>
      </section>

      {/* Branding */}
      <section className="space-y-3 rounded-lg border bg-card p-4">
        <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Branding
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">App title</label>
          <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Doc Translation Review" />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Logo URL (optional)</label>
          <Input value={logoUrl} onChange={(e) => setLogoUrl(e.target.value)} placeholder="/brand-logo.png" />
          <p className="mt-1 text-xs text-muted-foreground">
            A served path (drop an image in <code>frontend/public/</code>, e.g.{" "}
            <code>/brand-logo.png</code>) or a full URL. Blank keeps the built-in icon.
          </p>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Logo alt text (optional)</label>
          <Input value={logoAlt} onChange={(e) => setLogoAlt(e.target.value)} placeholder="Acme Corp" />
        </div>
      </section>

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

      <div className="flex items-center justify-end gap-2">
        {!firstRun && loaded && (
          <Button variant="ghost" onClick={() => hydrate(loaded)} disabled={busy}>
            <RotateCcw /> Reset
          </Button>
        )}
        <Button onClick={save} disabled={busy || modelMissing}>
          {busy ? <Loader2 className="animate-spin" /> : firstRun ? <CheckCircle2 /> : <Save />}
          {firstRun ? "Save & start" : "Save settings"}
        </Button>
      </div>
    </div>
  );
}
