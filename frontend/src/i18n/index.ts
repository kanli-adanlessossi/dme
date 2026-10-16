// Configuration i18next / react-i18next (NFR-10).
// Français par défaut ; `fallbackLng` et une seule locale enregistrée pour
// l'instant. Ajouter une langue = enregistrer un paquet de ressources
// supplémentaire dans `resources` et l'exposer via `supportedLngs`.
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import fr from "./fr";

export const defaultLanguage = "fr" as const;

export const resources = {
  fr,
} as const;

void i18n.use(initReactI18next).init({
  resources,
  lng: defaultLanguage,
  fallbackLng: defaultLanguage,
  supportedLngs: [defaultLanguage],
  defaultNS: "translation",
  interpolation: {
    // React échappe déjà les valeurs ; pas besoin du double échappement.
    escapeValue: false,
  },
});

export default i18n;
