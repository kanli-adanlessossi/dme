// Bandeau de conflits de synchronisation à revoir (tâche 17.3) — NFR-6, P12.
//
// Rôle
// ----
// Surface de **revue** des conflits de synchronisation. `SyncStatusBar` (14.4)
// n'affiche qu'un compteur ; `ConflictBanner` est l'endroit où l'utilisateur
// *voit* les conflits et les *acquitte*. Il s'abonne à l'observable
// `getSyncEngine().conflicts` (alimenté par `sync.ts` quand le serveur renvoie
// des conflits LWW) et met en avant les entités **sensibles** (notes de visite,
// cf. design « conflits sensibles ») signalées pour revue humaine plutôt
// qu'écrasées silencieusement (P12).
//
// Résolution non destructive (P12)
// --------------------------------
// Le serveur a déjà tranché le conflit en LWW et consigné le perdant dans
// `sync_conflict` ; aucune donnée n'est détruite. Ce bandeau ne « résout » donc
// pas le conflit côté données : il le **porte à l'attention** de l'utilisateur,
// qui peut consulter la valeur perdante (payload du perdant) puis **acquitter**
// la liste une fois les conflits revus (`engine.clearConflicts()`). L'état
// gagnant est déjà appliqué localement par le pull.
//
// Mobile-first (NFR-9) : bandeau pleine largeur, cibles tactiles >=44px,
// contenu repliable (un bandeau compact par défaut, détails à la demande).
// Français par défaut (NFR-10).

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { getSyncEngine, type SyncConflict } from "../lib/sync";

/** Libellé i18n de l'entité en conflit. */
function entityLabelKey(entity: SyncConflict["entity"]) {
  return entity === "visit"
    ? ("components.conflictBanner.entities.visit" as const)
    : ("components.conflictBanner.entities.patient" as const);
}

/**
 * Extrait un libellé lisible du payload perdant pour aider à la revue, sans
 * exposer de PHI superflue : on privilégie un nom/MRN pour un patient, un motif
 * pour une visite, à défaut l'identifiant d'entité.
 */
function describeLoser(conflict: SyncConflict): string {
  const p = conflict.loser_payload ?? {};
  if (conflict.entity === "patient") {
    const given = typeof p.given_name === "string" ? p.given_name : "";
    const family = typeof p.family_name === "string" ? p.family_name : "";
    const name = `${given} ${family}`.trim();
    if (name) return name;
    if (typeof p.mrn === "string" && p.mrn) return p.mrn;
  } else {
    if (typeof p.reason === "string" && p.reason) return p.reason;
  }
  return conflict.entity_id;
}

export default function ConflictBanner() {
  const { t } = useTranslation();
  const engine = getSyncEngine();

  const [conflicts, setConflicts] = useState<SyncConflict[]>([]);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const sub = engine.conflicts.subscribe(setConflicts);
    return () => sub.unsubscribe();
  }, [engine]);

  // Les conflits sensibles (p. ex. notes de visite) sont mis en tête : ce sont
  // ceux qui exigent une revue humaine (P12).
  const { sensitive, others } = useMemo(() => {
    const sensitive = conflicts.filter((c) => c.sensitive);
    const others = conflicts.filter((c) => !c.sensitive);
    return { sensitive, others };
  }, [conflicts]);

  if (conflicts.length === 0) return null;

  const hasSensitive = sensitive.length > 0;
  const ordered = [...sensitive, ...others];

  function acknowledge() {
    // Acquittement : l'utilisateur a revu les conflits. L'état gagnant est déjà
    // appliqué localement ; on vide simplement la file de revue.
    engine.clearConflicts();
    setExpanded(false);
  }

  return (
    <section
      role="alert"
      aria-live="assertive"
      className={`flex flex-col gap-2 rounded-lg border px-4 py-3 ${
        hasSensitive
          ? "border-red-300 bg-red-50"
          : "border-amber-300 bg-amber-50"
      }`}
    >
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className="text-lg leading-none">
          {hasSensitive ? "⚠️" : "🔁"}
        </span>
        <div className="flex-1">
          <p
            className={`text-sm font-semibold ${
              hasSensitive ? "text-red-800" : "text-amber-900"
            }`}
          >
            {hasSensitive
              ? t("components.conflictBanner.sensitiveTitle", {
                  count: sensitive.length,
                })
              : t("components.conflictBanner.title", {
                  count: conflicts.length,
                })}
          </p>
          <p
            className={`text-xs ${
              hasSensitive ? "text-red-700" : "text-amber-800"
            }`}
          >
            {t("components.conflictBanner.subtitle")}
          </p>
        </div>
      </div>

      {expanded && (
        <ul className="flex flex-col gap-2 border-t border-black/5 pt-2">
          {ordered.map((c) => (
            <li
              key={`${c.entity}:${c.entity_id}:${c.winner_version}`}
              className="flex flex-col rounded-md bg-white/70 px-3 py-2 text-xs"
            >
              <span className="flex items-center gap-2">
                <span className="font-medium text-slate-800">
                  {t(entityLabelKey(c.entity))}
                </span>
                {c.sensitive && (
                  <span className="rounded-full bg-red-100 px-2 py-0.5 font-medium text-red-700">
                    {t("components.conflictBanner.sensitiveTag")}
                  </span>
                )}
              </span>
              <span className="truncate text-slate-600">
                {describeLoser(c)}
              </span>
              <span className="text-slate-400">
                {t("components.conflictBanner.winnerVersion", {
                  version: c.winner_version,
                })}
              </span>
            </li>
          ))}
        </ul>
      )}

      <div className="flex items-center gap-2 pt-1">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="inline-flex min-h-11 items-center justify-center rounded-lg
            border border-slate-300 bg-white px-3 text-sm font-medium
            text-slate-700 active:bg-slate-100"
        >
          {expanded
            ? t("components.conflictBanner.hideDetails")
            : t("components.conflictBanner.showDetails")}
        </button>
        <button
          type="button"
          onClick={acknowledge}
          className="ml-auto inline-flex min-h-11 items-center justify-center
            rounded-lg bg-slate-800 px-4 text-sm font-medium text-white
            active:bg-slate-900"
        >
          {t("components.conflictBanner.acknowledge")}
        </button>
      </div>
    </section>
  );
}
