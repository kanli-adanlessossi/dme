// Ossature de l'application mobile-first (tâche 14.1).
// Mise en page à une main : contenu défilant au-dessus d'une navigation
// inférieure fixe (NFR-9). Le routeur complet, la carte, la base locale RxDB et
// la synchronisation arrivent aux sous-tâches/tâches suivantes (14.2+).
import { useState } from "react";
import { useTranslation } from "react-i18next";

import BottomNav, { type NavKey } from "./components/BottomNav";
import MapHome from "./pages/MapHome";
import RegionCenters from "./pages/RegionCenters";
import Login from "./pages/Login";

const PAGES: Record<NavKey, () => JSX.Element> = {
  map: MapHome,
  directory: RegionCenters,
  account: Login,
};

export default function App() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<NavKey>("map");
  const Page = PAGES[tab];

  return (
    <div className="mx-auto flex min-h-full max-w-screen-md flex-col">
      <header className="border-b border-slate-200 bg-white px-4 py-3">
        <p className="text-sm font-semibold text-sky-700">{t("app.title")}</p>
      </header>

      {/* Contenu défilant ; marge basse pour ne pas passer sous la nav fixe. */}
      <main className="flex-1 overflow-y-auto px-4 py-4 pb-20">
        <Page />
      </main>

      <BottomNav active={tab} onChange={setTab} />
    </div>
  );
}
