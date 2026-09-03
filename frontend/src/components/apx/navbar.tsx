import { Languages } from "lucide-react";
import { ModeToggle } from "@/components/apx/mode-toggle";
import { cn } from "@/lib/utils";

export type Tab = "review" | "glossary" | "instructions" | "audit" | "settings";

export function Navbar({
  active,
  onNavigate,
  subtitle,
  title = "Doc Translation Review",
  logoUrl = null,
  logoAlt = null,
  logoWidth = null,
  logoHeight = null,
  showTabs = true,
  isAdmin = true,
}: {
  active: Tab;
  onNavigate: (t: Tab) => void;
  subtitle?: React.ReactNode;
  title?: string;
  logoUrl?: string | null;
  logoAlt?: string | null;
  logoWidth?: number | null;
  logoHeight?: number | null;
  showTabs?: boolean;
  isAdmin?: boolean;
}) {
  const tabs: { id: Tab; label: string }[] = [
    { id: "review", label: "Review" },
    { id: "glossary", label: "Glossary" },
    { id: "instructions", label: "Instructions" },
    { id: "audit", label: "Audit" },
    // Settings is app configuration — admin (deployer) only.
    ...(isAdmin ? [{ id: "settings" as Tab, label: "Settings" }] : []),
  ];
  return (
    <header className="sticky top-0 z-50 border-b bg-background/80 backdrop-blur-sm">
      <div className="mx-auto flex h-14 w-full max-w-[1920px] items-center gap-4 px-4 sm:px-6 lg:px-8">
        <div className="flex items-center gap-2.5">
          {logoUrl ? (
            <img
              src={logoUrl}
              alt={logoAlt ?? title}
              title={logoAlt ?? title}
              className="rounded-md object-contain"
              style={{
                width: logoWidth ?? undefined,
                height: logoHeight ?? undefined,
              }}
            />
          ) : (
            <div className="flex size-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <Languages className="size-4" />
            </div>
          )}
          <div className="flex flex-col leading-tight">
            <span className="text-sm font-semibold">{title}</span>
            {subtitle && <span className="text-xs text-muted-foreground">{subtitle}</span>}
          </div>
        </div>

        <nav className="ml-4 flex items-center gap-1" hidden={!showTabs}>
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => onNavigate(t.id)}
              className={cn(
                "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                active === t.id
                  ? "bg-secondary text-secondary-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
              )}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <div className="flex-1" />
        <ModeToggle />
      </div>
    </header>
  );
}
