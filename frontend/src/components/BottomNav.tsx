// Navigation inférieure atteignable au pouce (NFR-9).
// Barre fixée en bas de l'écran pour un usage à une main ; chaque onglet est une
// cible tactile >=44px. Le routeur complet arrive aux tâches/pages ultérieures ;
// ici la navigation pilote simplement l'onglet actif via l'état parent.
import { useTranslation } from "react-i18next";

export type NavKey = "map" | "directory" | "account";

interface BottomNavProps {
  active: NavKey;
  onChange: (key: NavKey) => void;
}

const ICONS: Record<NavKey, string> = {
  map: "🗺️",
  directory: "📋",
  account: "👤",
};

export default function BottomNav({ active, onChange }: BottomNavProps) {
  const { t } = useTranslation();

  const items: { key: NavKey; label: string }[] = [
    { key: "map", label: t("nav.map") },
    { key: "directory", label: t("nav.directory") },
    { key: "account", label: t("nav.account") },
  ];

  return (
    <nav
      aria-label={t("nav.map")}
      className="fixed inset-x-0 bottom-0 z-10 flex border-t border-slate-200
        bg-white pb-[env(safe-area-inset-bottom)]"
    >
      {items.map(({ key, label }) => {
        const isActive = key === active;
        return (
          <button
            key={key}
            type="button"
            aria-current={isActive ? "page" : undefined}
            onClick={() => onChange(key)}
            className={`nav-item${isActive ? " nav-item-active" : ""}`}
          >
            <span aria-hidden="true" className="text-lg leading-none">
              {ICONS[key]}
            </span>
            <span>{label}</span>
          </button>
        );
      })}
    </nav>
  );
}
