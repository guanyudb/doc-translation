import { useEffect, useState } from "react";
import { ThemeProvider } from "@/components/apx/theme-provider";
import { Navbar, Tab } from "@/components/apx/navbar";
import { ReviewView } from "@/components/review/ReviewView";
import { GlossaryView } from "@/components/glossary/GlossaryView";
import { AuditView } from "@/components/audit/AuditView";
import { api, AppConfig } from "@/api";

export default function App() {
  const [tab, setTab] = useState<Tab>("review");
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [activePair, setActivePair] = useState<string | null>(null);

  useEffect(() => {
    api.config().then(setCfg).catch(() => setCfg(null));
  }, []);

  return (
    <ThemeProvider defaultTheme="system" storageKey="doc-translation-theme">
      <div className="min-h-screen bg-background text-foreground">
        <Navbar
          active={tab}
          onNavigate={setTab}
          subtitle={cfg ? `${cfg.reviewer} · target ${cfg.target_language}` : undefined}
        />
        <main className="mx-auto max-w-[1400px] px-4 py-6 sm:px-6">
          {tab === "review" && (
            <ReviewView
              activePair={activePair}
              setActivePair={setActivePair}
              onOpenAudit={(id) => {
                setActivePair(id);
                setTab("audit");
              }}
            />
          )}
          {tab === "glossary" && <GlossaryView deltaSyncEnabled={cfg?.delta_sync_enabled ?? false} />}
          {tab === "audit" && <AuditView pairId={activePair} />}
        </main>
      </div>
    </ThemeProvider>
  );
}
