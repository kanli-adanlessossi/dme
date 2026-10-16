# Carte publique du Togo — vérification (tâche 15.4)

Vérification du rendu mobile, de la cliquabilité et du rechargement hors ligne
de la carte interactive de l'accueil public (REQ 7.1–7.5, NFR-9, NFR-11).

Les vérifications **automatisées** sont couvertes par la suite de tests
`frontend` (`npm run test`). Ce document ne liste que les étapes qui exigent un
appareil ou un navigateur réel et ne peuvent pas être automatisées sous jsdom
(rendu WebGL de MapLibre, service worker réel, mesure réseau 3G). Elles sont
rejouées lors de la vérification E2E (tâche 18).

## Couverture automatisée (rappel)

| Aspect | Exigence | Test |
| --- | --- | --- |
| Détection WebGL | 7.5 | `src/lib/webgl.test.ts` |
| Chargement GeoJSON + URL cache SW | 7.1, 7.6, NFR-11 | `src/lib/geo.test.ts` |
| Chargement des centres par région | 7.2, 7.3 | `src/lib/centers.test.ts` |
| Aiguillage carte / repli / chargement | 7.5 | `src/pages/MapHome.test.ts` |
| Repli statique (5 régions, sélection) | 7.5, NFR-9 | `src/components/RegionFallbackList.test.ts` |
| Feuille : liste, état vide, erreur, navigation, cibles tactiles | 7.2, 7.3, 7.4, NFR-9 | `src/components/CenterSheet.test.ts` |
| Flux bout-en-bout région → feuille → profil | 7.2, 7.3, 7.4, 7.5 | `src/pages/MapHome.integration.test.ts` |
| Route de cache SW ↔ URL GeoJSON cliente | NFR-11 | `src/sw.cache.test.ts` |

Les cibles tactiles ≥44px sont vérifiées via les classes utilitaires
(`h-11 w-11`, `min-h-11`, `min-h-14` ≈ 44–56px) et les rôles/étiquettes
accessibles (`role="dialog"`, `aria-modal`, `aria-label`, `aria-pressed`) par
les tests ci-dessus.

## 1. Rendu mobile sur appareil réel (NFR-9)

> Automatisation partielle : le dimensionnement mobile-first et les cibles
> tactiles sont assertés dans les tests ; le rendu WebGL réel et l'ergonomie à
> une main se vérifient sur appareil.

1. `cd frontend && npm run build && npm run preview` (ou servir `dist/`).
2. Ouvrir sur un téléphone (ou DevTools → mode appareil, p. ex. Pixel/iPhone SE,
   largeur 360–390px).
3. Attendu :
   - la carte occupe la largeur plein écran, hauteur ~60vh, sans défilement
     horizontal ;
   - les 5 régions sont distinctes et surlignées au tap ;
   - la commande de zoom (coin haut-droit) est atteignable au pouce ;
   - la feuille inférieure glisse du bas, le bouton de fermeture et les entrées
     de centre sont confortablement tapables (≥44px) ;
   - navigation réalisable à une main.

## 2. Cliquabilité / repli WebGL (REQ 7.1, 7.2, 7.4, 7.5)

Le flux complet est couvert automatiquement. Vérification manuelle du rendu
WebGL réel uniquement :

1. Taper une région sur la carte → la feuille s'ouvre et liste les centres
   (ou l'état vide si aucun).
2. Taper un centre → navigation vers le profil ; « Retour » revient à la carte.
3. **Repli** : désactiver WebGL (Chrome `chrome://flags` → *WebGL* = Disabled,
   ou `about:config` `webgl.disabled=true` sous Firefox), recharger.
   Attendu : la liste statique des 5 régions remplace la carte et se comporte à
   l'identique (sélection → feuille).

## 3. Rechargement hors ligne depuis le cache (NFR-11)

> Automatisation : `src/sw.cache.test.ts` prouve que l'URL du GeoJSON interrogée
> par le client matche la route de cache du service worker, et `vite.config.ts`
> précache l'app shell (`globPatterns`). La vérification réelle du service
> worker se fait en navigateur (le SW ne tourne pas sous jsdom).

1. `cd frontend && npm run build && npm run preview`.
2. Ouvrir l'app ; dans DevTools → **Application → Service Workers**, confirmer
   que le worker est *activated and running*.
3. Naviguer sur la carte pour déclencher la mise en cache du GeoJSON
   (DevTools → Application → Cache Storage → `map-geojson` doit contenir
   `…/geo/togo-regions.geojson`).
4. DevTools → **Network → Offline** (ou couper le réseau).
5. Recharger la page (Ctrl/Cmd+R).
   - Attendu : l'app shell se recharge depuis le précache et la carte s'affiche
     en servant le GeoJSON depuis `map-geojson` — aucune page d'erreur réseau.
   - Si WebGL est indisponible hors ligne, le repli statique s'affiche (aucune
     dépendance réseau).

## 4. Performance : premier rendu < 3 s en 3G (NFR-11)

> Mesure manuelle (pas de proxy automatisé fiable sous jsdom).

1. `cd frontend && npm run build && npm run preview`.
2. DevTools → **Network**, profil *Slow 3G* ; **Performance/Lighthouse** en mode
   *Mobile* avec *throttling* appliqué.
3. Lancer un audit Lighthouse (Mobile) ou mesurer *First Contentful Paint* /
   *Largest Contentful Paint* sur l'accueil.
   - **Cible** : premier rendu utile de la carte < 3 s en 3G lente lors de la
     première visite ; quasi instantané aux visites suivantes (service worker).
   - Le style MapLibre est minimal (aucune tuile raster externe, aucune clé
     d'API) pour tenir cette cible ; les ressources de carte sont mises en
     cache (`map-geojson`, `map-tiles`, `map-assets`).
