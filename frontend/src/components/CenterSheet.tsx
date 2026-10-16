// Feuille inférieure des centres d'une région (tâche 15.2) — exigences 7.2, 7.3, 7.4.
//
// Déclenchée par le clic sur une région de `TogoMap` (seam `onRegionSelect`,
// tâche 15.1). Charge les centres publics actifs de la région via
// `GET /api/public/regions/{code}/centers` (tâche 12, lecture publique sans
// auth, projection sûre sans PHI). Mobile-first (NFR-9) : feuille glissant du
// bas, atteignable à une main, cibles tactiles >=44px, dismissible (voile +
// bouton + touche Échap).
//
// États observables :
//  - chargement        → indicateur de progression ;
//  - erreur réseau      → message + bouton « Réessayer » (jamais une page cassée) ;
//  - région vide        → message d'état vide (exigence 7.3), surtout PAS une erreur ;
//  - liste de centres   → chaque entrée navigue vers le profil public (exigence 7.4)
//                         via le seam `onSelectCenter` (la page `CenterProfile`
//                         est construite en tâche 16 ; ici on ne fait que câbler
//                         la navigation vers la route existante).

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  fetchRegionCenters,
  REGION_LABELS,
  type PublicCenterSummary,
  type RegionCode,
} from "../lib/api";

export interface CenterSheetProps {
  /** Région sélectionnée ; `null` ferme la feuille. */
  region: RegionCode | null;
  /** Fermeture demandée (voile, bouton, Échap). */
  onClose: () => void;
  /**
   * Navigation vers le profil public d'un centre (exigence 7.4). Seam vers la
   * route `CenterProfile` (tâche 16) ; `MapHome` fournit l'implémentation.
   */
  onSelectCenter: (centerId: string) => void;
}

type LoadState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "ready"; centers: PublicCenterSummary[] };

export default function CenterSheet({
  region,
  onClose,
  onSelectCenter,
}: CenterSheetProps) {
  const { t } = useTranslation();
  const [state, setState] = useState<LoadState>({ phase: "loading" });
  // Jeton de rechargement : incrémenté par « Réessayer » pour relancer l'effet.
  const [reloadToken, setReloadToken] = useState(0);

  const open = region !== null;

  // Charge les centres à chaque (ré)ouverture / changement de région / retry.
  useEffect(() => {
    if (region === null) return;

    const controller = new AbortController();
    let active = true;
    setState({ phase: "loading" });

    fetchRegionCenters(region, controller.signal)
      .then((centers) => {
        if (active) setState({ phase: "ready", centers });
      })
      .catch((err: unknown) => {
        // Une annulation (changement de région / démontage) n'est pas une erreur.
        if (controller.signal.aborted) return;
        if (!active) return;
        // Un 404 (région inconnue) ne devrait pas arriver depuis la carte ; on
        // le traite comme une liste vide plutôt qu'une erreur (exigence 7.3).
        if (err instanceof ApiError && err.status === 404) {
          setState({ phase: "ready", centers: [] });
          return;
        }
        setState({ phase: "error" });
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [region, reloadToken]);

  // Échap ferme la feuille (accessibilité clavier / desktop).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || region === null) return null;

  const regionName = REGION_LABELS[region];

  return (
    <div
      className="fixed inset-0 z-20 flex flex-col justify-end"
      role="dialog"
      aria-modal="true"
      aria-label={t("pages.centerSheet.title", { region: regionName })}
    >
      {/* Voile : tap pour fermer (dismissible, NFR-9). */}
      <button
        type="button"
        aria-label={t("pages.centerSheet.close")}
        onClick={onClose}
        className="absolute inset-0 h-full w-full cursor-default bg-slate-900/40"
      />

      {/* Panneau glissant du bas ; hauteur plafonnée, contenu défilant. */}
      <div
        className="relative flex max-h-[75vh] flex-col rounded-t-2xl bg-white
          pb-[env(safe-area-inset-bottom)] shadow-xl"
      >
        {/* Poignée visuelle de « drag ». */}
        <div className="flex justify-center pt-2" aria-hidden="true">
          <span className="h-1.5 w-10 rounded-full bg-slate-300" />
        </div>

        <header className="flex items-center gap-3 px-4 py-3">
          <h2 className="min-w-0 flex-1 truncate text-base font-semibold text-slate-800">
            {t("pages.centerSheet.title", { region: regionName })}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("pages.centerSheet.close")}
            className="flex h-11 w-11 shrink-0 items-center justify-center
              rounded-full text-xl text-slate-500 active:bg-slate-100"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </header>

        <div className="min-h-24 overflow-y-auto px-4 pb-4">
          {state.phase === "loading" && (
            <p
              className="py-8 text-center text-sm text-slate-500"
              role="status"
              aria-live="polite"
            >
              {t("pages.centerSheet.loading")}
            </p>
          )}

          {state.phase === "error" && (
            <div className="py-8 text-center" role="alert">
              <p className="text-sm text-red-700">
                {t("pages.centerSheet.loadError")}
              </p>
              <button
                type="button"
                onClick={() => setReloadToken((n) => n + 1)}
                className="mt-3 inline-flex min-h-11 items-center rounded-lg
                  border border-slate-300 px-4 text-sm font-medium text-sky-700
                  active:bg-slate-100"
              >
                {t("pages.centerSheet.retry")}
              </button>
            </div>
          )}

          {state.phase === "ready" && state.centers.length === 0 && (
            <p
              className="py-8 text-center text-sm text-slate-500"
              role="status"
            >
              {t("pages.centerSheet.empty")}
            </p>
          )}

          {state.phase === "ready" && state.centers.length > 0 && (
            <>
              <p className="pb-2 text-xs text-slate-500">
                {t("pages.centerSheet.count", {
                  count: state.centers.length,
                })}
              </p>
              <ul className="flex flex-col divide-y divide-slate-100">
                {state.centers.map((center) => (
                  <li key={center.id}>
                    <button
                      type="button"
                      onClick={() => onSelectCenter(center.id)}
                      aria-label={t("pages.centerSheet.viewProfile", {
                        name: center.name,
                      })}
                      className="flex min-h-14 w-full flex-col items-start gap-0.5
                        py-3 text-left active:bg-slate-50"
                    >
                      <span className="font-medium text-slate-800">
                        {center.name}
                      </span>
                      <span className="text-sm text-slate-500">
                        {center.address ??
                          t("pages.centerSheet.noAddress")}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
