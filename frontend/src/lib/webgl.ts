// Détection de disponibilité de WebGL (seam pour la tâche 15.3, exigence 7.5).
//
// MapLibre GL exige un contexte WebGL. Sur certains appareils bas de gamme ou
// navigateurs restreints, WebGL peut être absent ou désactivé ; dans ce cas la
// carte ne doit pas « planter » mais se replier sur `RegionFallbackList`
// (tâche 15.3). Ce module fournit une détection légère et un hook React.
//
// La tâche 15.1 n'implémente PAS le repli lui-même — elle expose seulement le
// moyen de le déclencher, pour que 15.3 l'y branche sans retoucher `TogoMap`.

import { useEffect, useState } from "react";

/**
 * Teste si le navigateur peut créer un contexte WebGL.
 *
 * Essaie `webgl2` puis `webgl`/`experimental-webgl`. Entouré d'un try/catch car
 * certaines implémentations lèvent plutôt que de renvoyer `null`. Le canvas est
 * créé en mémoire (jamais inséré dans le DOM).
 */
export function isWebGLAvailable(): boolean {
  if (typeof document === "undefined") return false;
  try {
    const canvas = document.createElement("canvas");
    const gl =
      canvas.getContext("webgl2") ||
      canvas.getContext("webgl") ||
      canvas.getContext("experimental-webgl");
    return gl != null;
  } catch {
    return false;
  }
}

/**
 * Hook renvoyant l'état de disponibilité de WebGL.
 *
 * `null` pendant la détection (premier rendu, avant l'effet), puis `true`/`false`.
 * Permet à un parent de rendre `TogoMap` quand `true` et `RegionFallbackList`
 * quand `false` (tâche 15.3) sans flash de contenu erroné.
 */
export function useWebGLAvailable(): boolean | null {
  const [available, setAvailable] = useState<boolean | null>(null);
  useEffect(() => {
    setAvailable(isWebGLAvailable());
  }, []);
  return available;
}
