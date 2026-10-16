// Accueil public : carte interactive du Togo (REQ-7).
// La tâche 15.1 rend la carte MapLibre (`TogoMap`) : les 5 régions sont
// cliquables et surlignables. La tâche 15.2 branche ici la feuille inférieure
// (`CenterSheet`) : au clic sur une région, elle liste les centres publics
// actifs de la région (exigence 7.2), affiche un état vide si la région n'a
// aucun centre (exigence 7.3), et navigue vers le profil public d'un centre
// (exigence 7.4).
//
// La tâche 15.3 branche ici le repli non-WebGL (exigence 7.5, NFR-11) : si WebGL
// est indisponible, on rend `RegionFallbackList` (sélecteur statique des 5
// régions) à la place de `TogoMap`. Les deux chemins partagent EXACTEMENT le
// même flux de sélection (`setSelected` → `CenterSheet`) : aucune duplication de
// la logique de chargement des centres. La détection (`useWebGLAvailable`)
// renvoie `null` au premier rendu ; tant qu'elle est indéterminée on n'affiche
// ni la carte ni le repli pour éviter un flash de contenu erroné.
import { useState } from "react";
import { useTranslation } from "react-i18next";

import TogoMap from "../components/TogoMap";
import RegionFallbackList from "../components/RegionFallbackList";
import CenterSheet from "../components/CenterSheet";
import CenterProfile from "./CenterProfile";
import { useWebGLAvailable } from "../lib/webgl";
import type { RegionCode } from "../lib/api";

export default function MapHome() {
  const { t } = useTranslation();
  // Disponibilité de WebGL : `null` pendant la détection (premier rendu), puis
  // `true` (carte MapLibre) / `false` (repli statique `RegionFallbackList`).
  const webglAvailable = useWebGLAvailable();
  const [selected, setSelected] = useState<RegionCode | null>(null);
  // Centre choisi dans la feuille → affichage de son profil (exigence 7.4).
  // Le routage de l'app est par onglets (pas de react-router) : on gère la
  // navigation vers `CenterProfile` par un état local, la feuille étant
  // ancrée à l'accueil. La page de profil complète est la tâche 16.
  const [activeCenterId, setActiveCenterId] = useState<string | null>(null);

  if (activeCenterId) {
    return (
      <CenterProfile
        centerId={activeCenterId}
        onBack={() => setActiveCenterId(null)}
      />
    );
  }

  return (
    <section className="flex flex-col gap-3">
      <h1 className="text-xl font-semibold md:text-2xl">
        {t("pages.map.title")}
      </h1>
      <p className="text-sm text-slate-600">{t("pages.map.intro")}</p>

      {/*
        Carte (15.1) si WebGL est disponible, sinon repli statique (15.3). Les
        deux appellent `setSelected`, qui ouvre la feuille 15.2 — même flux. Tant
        que la détection est indéterminée (`null`), on affiche un espace réservé
        neutre pour éviter un flash de contenu erroné.
      */}
      {webglAvailable === null && (
        <div
          className="flex h-[60vh] min-h-72 w-full items-center justify-center
            rounded-lg border border-slate-200 bg-sky-50 text-sm text-slate-500"
          role="status"
          aria-live="polite"
        >
          {t("pages.map.loading")}
        </div>
      )}

      {webglAvailable === true && (
        <TogoMap selectedRegion={selected} onRegionSelect={setSelected} />
      )}

      {webglAvailable === false && (
        <RegionFallbackList
          selectedRegion={selected}
          onRegionSelect={setSelected}
        />
      )}

      {/* 15.2 : feuille inférieure listant les centres de la région. */}
      <CenterSheet
        region={selected}
        onClose={() => setSelected(null)}
        onSelectCenter={(centerId) => {
          setActiveCenterId(centerId);
          setSelected(null);
        }}
      />
    </section>
  );
}
