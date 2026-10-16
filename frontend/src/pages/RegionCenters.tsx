// Annuaire public : parcourir / filtrer les centres par région (REQ-8, tâche 16).
//
// Consomme l'API publique (lecture seule, sans auth, projection sûre sans PHI,
// P4) :
//   - `GET /api/public/regions`               → les 5 régions + nombre de centres
//                                                 publics actifs (exigence 8.1) ;
//   - `GET /api/public/regions/{code}/centers` → centres actifs d'une région
//                                                 (exigence 8.2) ;
// Le service worker (sw.ts, route `public-directory`) sert une version en cache
// de ces lectures en cas de coupure réseau — repli cache (NFR-11), l'annuaire
// reste consultable hors ligne après la première visite.
//
// Mobile-first (NFR-9) : filtre par région via un `<select>` natif (atteignable
// à une main, accessible), liste verticale pleine largeur, cibles tactiles
// >=44px. Au clic sur un centre, on affiche son profil public (exigence 7.4) via
// `CenterProfile` — le routage de l'app étant par onglets (pas de react-router),
// la navigation est gérée par un état local, comme dans `MapHome`.
//
// États observables : chargement, erreur réseau (message + « Réessayer », jamais
// une page cassée), région vide → message d'état vide (exigence 7.3, 8.1).

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  fetchPublicRegions,
  fetchRegionCenters,
  REGION_LABELS,
  type PublicCenterSummary,
  type PublicRegion,
  type RegionCode,
} from "../lib/api";
import CenterProfile from "./CenterProfile";

/** Ordre géographique nord → sud, stable, pour le sélecteur de région. */
const REGION_ORDER: readonly RegionCode[] = [
  "SAVANES",
  "KARA",
  "CENTRALE",
  "PLATEAUX",
  "MARITIME",
] as const;

type RegionsState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "ready"; regions: PublicRegion[] };

type CentersState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "ready"; centers: PublicCenterSummary[] };

export default function RegionCenters() {
  const { t } = useTranslation();

  // Région filtrée ; `null` = « Toutes les régions » (agrégat de toutes).
  const [region, setRegion] = useState<RegionCode | null>(null);
  // Centre sélectionné → affichage de son profil public (exigence 7.4).
  const [activeCenterId, setActiveCenterId] = useState<string | null>(null);

  const [regionsState, setRegionsState] = useState<RegionsState>({
    phase: "loading",
  });
  const [centersState, setCentersState] = useState<CentersState>({
    phase: "idle",
  });
  // Jetons de rechargement : incrémentés par « Réessayer » pour relancer l'effet.
  const [regionsReload, setRegionsReload] = useState(0);
  const [centersReload, setCentersReload] = useState(0);

  // Charge la liste des régions (une fois, + sur « Réessayer »).
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setRegionsState({ phase: "loading" });

    fetchPublicRegions(controller.signal)
      .then((regions) => {
        if (active) setRegionsState({ phase: "ready", regions });
      })
      .catch(() => {
        if (controller.signal.aborted || !active) return;
        setRegionsState({ phase: "error" });
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [regionsReload]);

  // Charge les centres de la région filtrée. Pour « Toutes les régions »
  // (`region === null`), agrège les centres des 5 régions en parallèle.
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setCentersState({ phase: "loading" });

    const codes = region === null ? REGION_ORDER : [region];
    Promise.all(codes.map((c) => fetchRegionCenters(c, controller.signal)))
      .then((lists) => {
        if (!active) return;
        const centers = lists.flat();
        setCentersState({ phase: "ready", centers });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || !active) return;
        // Un 404 (code région inconnu) est traité comme une liste vide plutôt
        // qu'une erreur (exigence 7.3) — ne devrait pas arriver depuis ce menu.
        if (err instanceof ApiError && err.status === 404) {
          setCentersState({ phase: "ready", centers: [] });
          return;
        }
        setCentersState({ phase: "error" });
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [region, centersReload]);

  const regionsByCode = useMemo(() => {
    if (regionsState.phase !== "ready") return null;
    const map = new Map<RegionCode, PublicRegion>();
    for (const r of regionsState.regions) map.set(r.code, r);
    return map;
  }, [regionsState]);

  const onChangeRegion = useCallback(
    (value: string) => {
      setRegion(value === "" ? null : (value as RegionCode));
    },
    [],
  );

  // Navigation vers le profil public d'un centre (exigence 7.4).
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
        {t("pages.directory.title")}
      </h1>
      <p className="text-sm text-slate-600">{t("pages.directory.intro")}</p>

      {/* Filtre par région : <select> natif, accessible et atteignable au pouce. */}
      <label className="flex flex-col gap-1">
        <span className="sr-only">{t("pages.directory.intro")}</span>
        <select
          value={region ?? ""}
          onChange={(e) => onChangeRegion(e.target.value)}
          disabled={regionsState.phase !== "ready"}
          aria-label={t("pages.directory.intro")}
          className="min-h-11 w-full rounded-lg border border-slate-300 bg-white
            px-3 text-base text-slate-800 disabled:opacity-60"
        >
          <option value="">{t("pages.directory.allRegions")}</option>
          {REGION_ORDER.map((code) => {
            const count = regionsByCode?.get(code)?.center_count;
            return (
              <option key={code} value={code}>
                {count === undefined
                  ? REGION_LABELS[code]
                  : t("pages.directory.regionOption", {
                      region: REGION_LABELS[code],
                      count,
                    })}
              </option>
            );
          })}
        </select>
      </label>

      {regionsState.phase === "loading" && (
        <p
          className="text-sm text-slate-500"
          role="status"
          aria-live="polite"
        >
          {t("pages.directory.loadingRegions")}
        </p>
      )}

      {regionsState.phase === "error" && (
        <div
          className="flex flex-col items-start gap-2 rounded-lg border
            border-red-200 bg-red-50 p-3"
          role="alert"
        >
          <p className="text-sm text-red-700">
            {t("pages.directory.regionsError")}
          </p>
          <button
            type="button"
            onClick={() => setRegionsReload((n) => n + 1)}
            className="inline-flex min-h-11 items-center rounded-lg border
              border-slate-300 bg-white px-4 text-sm font-medium text-sky-700
              active:bg-slate-100"
          >
            {t("pages.directory.retry")}
          </button>
        </div>
      )}

      {/* Liste des centres de la région filtrée. */}
      {centersState.phase === "loading" && (
        <p
          className="py-6 text-center text-sm text-slate-500"
          role="status"
          aria-live="polite"
        >
          {t("pages.directory.loadingCenters")}
        </p>
      )}

      {centersState.phase === "error" && (
        <div className="py-6 text-center" role="alert">
          <p className="text-sm text-red-700">
            {t("pages.directory.centersError")}
          </p>
          <button
            type="button"
            onClick={() => setCentersReload((n) => n + 1)}
            className="mt-3 inline-flex min-h-11 items-center rounded-lg border
              border-slate-300 bg-white px-4 text-sm font-medium text-sky-700
              active:bg-slate-100"
          >
            {t("pages.directory.retry")}
          </button>
        </div>
      )}

      {centersState.phase === "ready" && centersState.centers.length === 0 && (
        <p className="py-6 text-center text-sm text-slate-500" role="status">
          {region === null
            ? t("pages.directory.emptyAll")
            : t("pages.directory.empty")}
        </p>
      )}

      {centersState.phase === "ready" && centersState.centers.length > 0 && (
        <>
          <p className="text-xs text-slate-500">
            {t("pages.directory.count", {
              count: centersState.centers.length,
            })}
          </p>
          <ul className="flex flex-col divide-y divide-slate-100 rounded-lg
            border border-slate-200 bg-white">
            {centersState.centers.map((center) => (
              <li key={center.id}>
                <button
                  type="button"
                  onClick={() => setActiveCenterId(center.id)}
                  aria-label={t("pages.directory.viewProfile", {
                    name: center.name,
                  })}
                  className="flex min-h-14 w-full flex-col items-start gap-0.5
                    px-4 py-3 text-left active:bg-slate-50"
                >
                  <span className="font-medium text-slate-800">
                    {center.name}
                  </span>
                  <span className="text-sm text-slate-500">
                    {center.address ?? t("pages.directory.noAddress")}
                    {region === null && (
                      <>
                        {" · "}
                        {REGION_LABELS[center.region_code]}
                      </>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
