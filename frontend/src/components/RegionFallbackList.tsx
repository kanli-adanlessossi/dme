// Repli statique quand WebGL/carte est indisponible (tâche 15.3) —
// exigence 7.5, NFR-11, NFR-9.
//
// MapLibre GL exige WebGL. Sur un appareil bas de gamme ou un navigateur
// restreint, WebGL peut être absent/désactivé : la carte ne doit PAS « planter »
// mais se replier gracieusement sur ce sélecteur de région statique
// (`isWebGLAvailable()` / `useWebGLAvailable()`, seam de 15.1 dans lib/webgl.ts).
//
// Fonctionnellement, sélectionner une région ici DOIT se comporter exactement
// comme cliquer une région sur la carte : le composant n'appelle que
// `onRegionSelect(code)` — le même seam que `TogoMap` — et laisse `MapHome`
// piloter l'état `selected` et ouvrir `CenterSheet`. Aucune logique de
// chargement des centres n'est dupliquée ici.
//
// Mobile-first (NFR-9) : liste verticale pleine largeur, cibles tactiles
// >=44px (ici min-h-14 ≈ 56px), atteignable à une main, libellés français
// réutilisant `REGION_LABELS` (NFR-10).

import { useTranslation } from "react-i18next";

import { REGION_LABELS, type RegionCode } from "../lib/api";

/**
 * Les 5 régions du Togo dans l'ordre géographique nord → sud
 * (Savanes, Kara, Centrale, Plateaux, Maritime). Exporté pour les tests et pour
 * garantir un ordre stable et prévisible du sélecteur de repli.
 */
export const FALLBACK_REGIONS: readonly RegionCode[] = [
  "SAVANES",
  "KARA",
  "CENTRALE",
  "PLATEAUX",
  "MARITIME",
] as const;

export interface RegionFallbackListProps {
  /**
   * Appelé à la sélection d'une région, avec son code canonique. MÊME seam que
   * `TogoMap.onRegionSelect` : `MapHome` y branche `setSelected`, qui ouvre
   * `CenterSheet`. Permet de ne pas dupliquer le flux de sélection/chargement.
   */
  onRegionSelect: (code: RegionCode) => void;
  /** Région actuellement sélectionnée (contrôlée par le parent), optionnel. */
  selectedRegion?: RegionCode | null;
  /** Classe CSS additionnelle appliquée au conteneur. */
  className?: string;
}

/**
 * Sélecteur de région statique affiché quand la carte WebGL est indisponible.
 * Rend les 5 régions comme une liste de boutons tactiles ; aucun canvas, aucun
 * WebGL — fonctionne partout, y compris hors ligne (NFR-11).
 */
export default function RegionFallbackList({
  onRegionSelect,
  selectedRegion = null,
  className,
}: RegionFallbackListProps) {
  const { t } = useTranslation();

  return (
    <nav
      aria-label={t("pages.regionFallback.ariaLabel")}
      className={`flex flex-col gap-2 rounded-lg border border-slate-200
        bg-white p-3 ${className ?? ""}`}
    >
      <p className="px-1 text-sm text-slate-600">
        {t("pages.regionFallback.intro")}
      </p>
      <ul className="flex flex-col gap-2">
        {FALLBACK_REGIONS.map((code) => {
          const selected = code === selectedRegion;
          return (
            <li key={code}>
              <button
                type="button"
                aria-pressed={selected}
                aria-label={t("pages.regionFallback.selectRegion", {
                  region: REGION_LABELS[code],
                })}
                onClick={() => onRegionSelect(code)}
                className={`flex min-h-14 w-full items-center justify-between
                  gap-3 rounded-lg border px-4 text-left text-base font-medium
                  active:bg-sky-100 ${
                    selected
                      ? "border-sky-500 bg-sky-50 text-sky-800"
                      : "border-slate-200 bg-white text-slate-800"
                  }`}
              >
                <span>{REGION_LABELS[code]}</span>
                <span aria-hidden="true" className="text-slate-400">
                  ›
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
