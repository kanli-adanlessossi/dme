// Barre d'état de synchronisation (tâche 14.4) — NFR-5.
//
// Affiche la connectivité (en ligne / hors ligne) et le nombre d'éléments en
// attente dans l'outbox, en s'abonnant à l'état observable du moteur de sync
// (`SyncEngine.state`). Mobile-first, chaînes en français via i18n. Surface aussi
// l'état des conflits à revoir (le câblage de `ConflictBanner` dans les pages est
// la tâche 17 ; ici on se contente d'exposer le compteur et un bouton réessayer).

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { getSyncEngine, type SyncConflict, type SyncState } from "../lib/sync";

/** Pastille de couleur selon la phase/connectivité. */
function statusColor(state: SyncState): string {
  if (state.phase === "revoked" || state.phase === "error") return "bg-red-500";
  if (!state.online) return "bg-slate-400";
  if (state.phase === "syncing") return "bg-amber-500";
  return "bg-emerald-500";
}

export default function SyncStatusBar() {
  const { t } = useTranslation();
  const engine = getSyncEngine();

  const [state, setState] = useState<SyncState>(engine.snapshot);
  const [conflicts, setConflicts] = useState<SyncConflict[]>([]);

  useEffect(() => {
    const stateSub = engine.state.subscribe(setState);
    const conflictSub = engine.conflicts.subscribe(setConflicts);
    return () => {
      stateSub.unsubscribe();
      conflictSub.unsubscribe();
    };
  }, [engine]);

  // Libellé principal : connectivité, nuancée par la phase de synchro.
  let label: string;
  if (state.phase === "revoked") label = t("sync.revoked");
  else if (!state.online) label = t("sync.offline");
  else if (state.phase === "syncing") label = t("sync.syncing");
  else if (state.phase === "error") label = t("sync.error");
  else label = t("sync.online");

  const pendingLabel =
    state.pending === 0
      ? t("sync.pending_zero")
      : t("sync.pending", { count: state.pending });

  const sensitiveCount = conflicts.filter((c) => c.sensitive).length;
  const canRetry = state.online && state.phase !== "syncing";

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center gap-2 border-b border-slate-200 bg-white
        px-4 py-2 text-xs text-slate-600"
    >
      <span
        aria-hidden="true"
        className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${statusColor(
          state,
        )}`}
      />
      <span className="font-medium text-slate-700">{label}</span>

      <span aria-hidden="true" className="text-slate-300">
        •
      </span>
      <span className="truncate">{pendingLabel}</span>

      {conflicts.length > 0 && (
        <span className="ml-auto rounded-full bg-red-100 px-2 py-0.5 font-medium text-red-700">
          {t("sync.conflicts", { count: conflicts.length })}
          {sensitiveCount > 0 ? ` (${sensitiveCount})` : ""}
        </span>
      )}

      {state.phase === "error" && canRetry && (
        <button
          type="button"
          onClick={() => void engine.syncNow()}
          className={`${conflicts.length > 0 ? "" : "ml-auto"} rounded border
            border-slate-300 px-2 py-0.5 font-medium text-sky-700
            active:bg-slate-100`}
        >
          {t("sync.retry")}
        </button>
      )}
    </div>
  );
}
