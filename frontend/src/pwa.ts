// Enregistrement du service worker PWA côté application (tâche 14.2).
//
// Utilise l'assistant virtuel de vite-plugin-pwa (`virtual:pwa-register`), qui
// n'enregistre le service worker qu'en build de production — en `vite dev`, le
// module est un no-op sûr (sauf si devOptions.enabled). L'enregistrement est
// invoqué depuis `main.tsx` pour que le rechargement hors ligne fonctionne dès
// la 2ᵉ visite (NFR-5, NFR-11).
import { registerSW } from "virtual:pwa-register";

export function registerPwa(): void {
  // `registerType: "prompt"` : on applique immédiatement la mise à jour puis on
  // recharge, pour garder un comportement simple à ce stade de fondation. Un vrai
  // bandeau « mise à jour disponible » pourra être branché plus tard.
  const updateSW = registerSW({
    immediate: true,
    onNeedRefresh() {
      updateSW(true);
    },
    onOfflineReady() {
      // L'app est prête à fonctionner hors ligne (ressources précachées).
      // Un indicateur UI pourra être ajouté avec la SyncStatusBar (tâche 14.4).
      console.info("Application prête hors ligne.");
    },
  });
}
