import { useEffect, useState } from "react";
import { ThemeProvider } from "@/components/apx/theme-provider";
import { Navbar, Tab } from "@/components/apx/navbar";
import { ReviewView } from "@/components/review/ReviewView";
import { GlossaryView } from "@/components/glossary/GlossaryView";
import { InstructionsView } from "@/components/instructions/InstructionsView";
import { AuditView } from "@/components/audit/AuditView";
import { SettingsView } from "@/components/settings/SettingsView";
import { api, AppConfig } from "@/api";

export default function App() {
  const [tab, setTab] = useState<Tab>("review");
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [activePair, setActivePair] = useState<string | null>(null);

  const refreshConfig = () => api.config().then(setCfg).catch(() => {});
  useEffect(() => {
    refreshConfig();
  }, []);

  // Keep the browser tab title in sync with the configured app title.
  useEffect(() => {
    if (cfg?.title) document.title = cfg.title;
  }, [cfg?.title]);

  // First-run gate: until the app has been configured once, force the setup
  // wizard and hide the tabs. Saving flips is_configured → the app unlocks.
  const needsSetup = cfg !== null && !cfg.is_configured;

  return (
    <ThemeProvider defaultTheme="system" storageKey="doc-translation-theme">
      <div className="min-h-screen bg-background text-foreground">
        <Navbar
          active={tab}
          onNavigate={setTab}
          showTabs={!needsSetup}
          isAdmin={cfg?.is_admin ?? false}
          title={cfg?.title ?? "Doc Translation Review"}
          logoUrl={cfg?.logo_url ?? null}
          logoAlt={cfg?.logo_alt ?? null}
          logoWidth={cfg?.logo_width ?? null}
          logoHeight={cfg?.logo_height ?? null}
          subtitle={
            cfg && !needsSetup ? (
              <>
                Signed in as <span className="font-medium text-foreground">{cfg.reviewer}</span>
                {" · default target "}
                {cfg.target_language}
              </>
            ) : undefined
          }
        />
        <main className="mx-auto w-full max-w-[1920px] px-4 py-6 sm:px-6 lg:px-8">
          {needsSetup ? (
            <SettingsView firstRun onSaved={() => refreshConfig()} />
          ) : (
            <>
              {tab === "review" && (
                <ReviewView
                  activePair={activePair}
                  setActivePair={setActivePair}
                  defaultTarget={cfg?.target_language ?? "English"}
                  onOpenAudit={(id) => {
                    setActivePair(id);
                    setTab("audit");
                  }}
                />
              )}
              {tab === "glossary" && <GlossaryView deltaSyncEnabled={cfg?.delta_sync_enabled ?? false} />}
              {tab === "instructions" && <InstructionsView />}
              {tab === "audit" && <AuditView pairId={activePair} />}
              {tab === "settings" && cfg?.is_admin && <SettingsView onSaved={() => refreshConfig()} />}
            </>
          )}
        </main>
      </div>
    </ThemeProvider>
  );
}
