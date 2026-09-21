import { useEffect, useMemo, useState } from "react";
import {
  FlaskConical, ChevronDown, ChevronRight, Loader2, Copy, Check, Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select, Textarea } from "@/components/ui/input";
import { api, Prompt, PlaygroundResult } from "@/api";
import { LANGUAGES } from "@/lib/languages";

const MAX_SOURCE = 10_000;
const MAX_BODY = 8000;
const CUSTOM = "custom";

/**
 * Prompt playground: paste a source paragraph, pick source/target languages, pick or edit a
 * prompt, and translate it immediately to iterate — no upload, no pipeline wait. Reuses the
 * prompts already loaded by InstructionsView (passed as a prop; no second fetch). Collapsed
 * by default so the prompt library stays the primary surface.
 */
export function PlaygroundPanel({ prompts }: { prompts: Prompt[] }) {
  const [open, setOpen] = useState(false);
  const [sourceText, setSourceText] = useState("");
  const [sourceLang, setSourceLang] = useState(""); // "" = auto-detect
  const [targetLang, setTargetLang] = useState(LANGUAGES[0] ?? "English");
  const [sel, setSel] = useState<string>(CUSTOM); // prompt_id as string, or CUSTOM
  const [body, setBody] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<PlaygroundResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showEffective, setShowEffective] = useState(false);
  const [copied, setCopied] = useState(false);

  // Default to the first library prompt (with its body) once prompts load, while sel is
  // still untouched. One-way sync: picking a prompt seeds the editable body.
  useEffect(() => {
    if (prompts.length && sel === CUSTOM && !body) {
      setSel(String(prompts[0].prompt_id));
      setBody(prompts[0].body);
    }
  }, [prompts]); // eslint-disable-line react-hooks/exhaustive-deps

  const onSelectPrompt = (v: string) => {
    setSel(v);
    if (v !== CUSTOM) {
      const p = prompts.find((x) => String(x.prompt_id) === v);
      if (p) setBody(p.body);
    }
  };

  const hasLangToken = useMemo(
    () => body.includes("{lang}") || body.includes("{target_lang}"),
    [body]
  );

  const insertLangToken = () => {
    if (hasLangToken) return;
    setBody((b) => `${b}${b && !b.endsWith("\n") ? "\n" : ""}Translate the text to {lang}.`);
  };

  const translate = () => {
    setRunning(true);
    setError(null);
    setResult(null);
    api
      .playground({
        source_text: sourceText,
        target_lang: targetLang,
        source_lang: sourceLang || null,
        prompt_body: body,
      })
      .then((r) => {
        setResult(r);
        setShowEffective(false);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setRunning(false));
  };

  const copyOut = () => {
    if (!result) return;
    navigator.clipboard?.writeText(result.translation).then(
      () => { setCopied(true); setTimeout(() => setCopied(false), 1500); },
      () => {}
    );
  };

  const canRun = !running && sourceText.trim().length > 0 && body.trim().length > 0;

  return (
    <div className="overflow-hidden rounded-lg border">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 bg-muted/40 px-3 py-2 text-left hover:bg-muted/60"
      >
        {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        <FlaskConical className="size-4 text-primary" />
        <span className="font-medium">Playground</span>
        <span className="text-xs text-muted-foreground">— test a prompt on a paragraph before you save it</span>
      </button>

      {open && (
        <div className="space-y-3 border-t p-4">
          {/* language + prompt pickers */}
          <div className="flex flex-wrap gap-3">
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Source language
              <Select className="w-44" value={sourceLang} onChange={(e) => setSourceLang(e.target.value)}>
                <option value="">Auto-detect</option>
                {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
              </Select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Target language
              <Select className="w-44" value={targetLang} onChange={(e) => setTargetLang(e.target.value)}>
                {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
              </Select>
            </label>
            <label className="flex flex-1 flex-col gap-1 text-xs text-muted-foreground">
              Prompt
              <Select value={sel} onChange={(e) => onSelectPrompt(e.target.value)}>
                {prompts.map((p) => <option key={p.prompt_id} value={String(p.prompt_id)}>{p.name}</option>)}
                <option value={CUSTOM}>Custom (ad-hoc)</option>
              </Select>
            </label>
          </div>

          {/* prompt body (editable) */}
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground">Prompt body (editable — not saved)</span>
              <Button
                variant="ghost" size="sm" onClick={insertLangToken} disabled={hasLangToken}
                title={hasLangToken ? "Prompt already has a {lang} token" : "Insert a {lang} token"}
              >
                Insert {"{lang}"}
              </Button>
            </div>
            <Textarea
              value={body}
              maxLength={MAX_BODY}
              onChange={(e) => { setBody(e.target.value); setSel(CUSTOM); }}
              rows={5}
              className="font-mono text-xs"
              placeholder="Paste or edit a system prompt…"
            />
            {!hasLangToken && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                No <code>{"{lang}"}</code> token — a language directive will be appended automatically.
              </p>
            )}
          </div>

          {/* source paragraph */}
          <div className="space-y-1">
            <span className="text-xs text-muted-foreground">Source paragraph</span>
            <Textarea
              value={sourceText}
              maxLength={MAX_SOURCE}
              onChange={(e) => setSourceText(e.target.value)}
              rows={4}
              placeholder="Paste a paragraph to translate…"
            />
          </div>

          <div className="flex items-center gap-3">
            <Button size="sm" onClick={translate} disabled={!canRun}>
              {running ? <Loader2 className="animate-spin" /> : <Sparkles />} Translate
            </Button>
            <span className="text-xs text-muted-foreground">{sourceText.length}/{MAX_SOURCE}</span>
          </div>

          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          {result && (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium">Translation</span>
                <Badge variant="outline">{result.route === "uc_gateway" ? "UC AI Gateway" : "serving"}</Badge>
                <span className="truncate text-xs text-muted-foreground" title={result.model_endpoint}>
                  {result.model_endpoint}
                </span>
                {result.directive_appended && (
                  <Badge className="bg-amber-500/15 text-amber-600 dark:text-amber-400">
                    language directive auto-appended
                  </Badge>
                )}
                <div className="flex-1" />
                <Button variant="ghost" size="sm" onClick={copyOut}>
                  {copied ? <Check className="text-emerald-600" /> : <Copy />} Copy
                </Button>
              </div>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 text-sm">
                {result.translation}
              </pre>

              {result.glossary_terms.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs text-muted-foreground">
                    {result.glossary_terms.length} glossary term{result.glossary_terms.length === 1 ? "" : "s"} applied:
                  </span>
                  {result.glossary_terms.map((g, i) => (
                    <Badge key={i} variant="outline" className="font-normal">
                      {g.source} → {g.target}
                    </Badge>
                  ))}
                </div>
              )}

              <div className="flex items-center gap-3 text-xs text-muted-foreground">
                <button className="underline hover:text-foreground" onClick={() => setShowEffective((s) => !s)}>
                  {showEffective ? "Hide" : "Show"} effective prompt
                </button>
                <span>·</span>
                <span>{result.elapsed_ms} ms</span>
                {result.usage && (result.usage.prompt_tokens != null || result.usage.completion_tokens != null) && (
                  <>
                    <span>·</span>
                    <span>
                      {result.usage.prompt_tokens ?? "?"} in / {result.usage.completion_tokens ?? "?"} out tokens
                    </span>
                  </>
                )}
              </div>
              {showEffective && (
                <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 text-xs">
                  {result.effective_prompt}
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
