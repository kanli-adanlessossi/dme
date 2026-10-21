// Hook de connectivité réseau — source navigateur canonique (ADR-2, #4).
//
// Rôle
// ----
// Expose un booléen `online` reflétant l'état de connectivité du poste, lu à la
// **source canonique du navigateur** : `navigator.onLine` comme valeur initiale,
// puis maintenu à jour par les événements `window` `online` / `offline`.
//
// Pourquoi pas `SyncState.online` (découplage délibéré — ADR-2 / H1 de la revue)
// -----------------------------------------------------------------------------
//  1. **Fraîcheur.** `SyncState.online` (cf. `sync.ts`) n'est maintenu que si
//     `SyncEngine.start()` a été appelé — c'est `start()` qui pose les écouteurs
//     `online`/`offline`. Hors d'une session déverrouillée (`wireSync()`), lire
//     `getSyncEngine().snapshot.online` renvoie une valeur initiale figée, donc
//     un faux signal.
//  2. **Découplage.** Le profil du centre est précisément le sous-système qui
//     n'utilise PAS la sync (ADR G-6, REST dédié). Faire dépendre son éditeur de
//     `getSyncEngine()` relierait deux sous-systèmes volontairement séparés.
//     `navigator.onLine` est toujours frais, sans dépendance, et trivial à
//     piloter en test.
//
// Ce hook NE touche donc JAMAIS `getSyncEngine()` : la seule source de vérité
// est `navigator.onLine` + les événements navigateur.

import { useEffect, useState } from "react";

/** Lecture synchrone de la connectivité navigateur (défensive côté SSR/test). */
function readOnline(): boolean {
  return typeof navigator !== "undefined" ? navigator.onLine : true;
}

/**
 * Renvoie l'état de connectivité du poste, lu à la source navigateur.
 *
 * - Valeur initiale : `navigator.onLine`.
 * - Mise à jour : écoute les événements `window` `online` et `offline`.
 * - Les écouteurs sont retirés au démontage (cleanup du `useEffect`).
 *
 * @returns `true` si le navigateur se déclare en ligne, `false` sinon.
 */
export function useOnline(): boolean {
  const [online, setOnline] = useState<boolean>(readOnline);

  useEffect(() => {
    if (typeof window === "undefined") return;

    const onOnline = () => setOnline(true);
    const onOffline = () => setOnline(false);

    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);

    // Resynchronise après l'abonnement : l'état a pu basculer entre le rendu
    // initial et la pose des écouteurs.
    setOnline(readOnline());

    return () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, []);

  return online;
}
