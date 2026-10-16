import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { registerPwa } from "./pwa"; // service worker PWA (hors ligne, NFR-5/NFR-11)
import { restoreSession } from "./lib/session"; // session local-first (NFR-7, tâche 17.1)
import "./i18n"; // initialise react-i18next (français par défaut, NFR-10)
import "./index.css"; // directives Tailwind + styles de base mobile-first (NFR-9)

// Restaure l'état d'auth depuis les jetons persistés AVANT le premier rendu
// (survie au rechargement, NFR-7). N'ouvre pas la base : la clé dérivée n'étant
// pas persistée (P13), la base reste verrouillée jusqu'à une nouvelle saisie du
// mot de passe (écran de déverrouillage de la page Compte).
restoreSession();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

// Enregistre le service worker (no-op en dev, actif en build de production).
registerPwa();
