// Typage fort des clés de traduction pour react-i18next.
// Le paquet français fait référence pour l'autocomplétion et la vérification
// des clés passées à `t(...)`.
import "i18next";

import type fr from "./fr";

declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "translation";
    resources: {
      translation: (typeof fr)["translation"];
    };
  }
}
