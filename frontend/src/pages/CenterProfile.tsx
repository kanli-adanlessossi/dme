// Profil public d'un centre (REQ 7.4, tâche 16).
//
// Consomme `GET /api/public/centers/{id}` (lecture publique, sans auth,
// projection sûre sans PHI, P4). Affiche les champs non sensibles exposés
// publiquement : nom, région, adresse, contact (téléphone/courriel), horaires
// d'ouverture, services proposés et coordonnées (exigence 7.4). Aucune donnée
// `patient`/`visit` ni champ interne n'est accessible ici (P4).
//
// Repli cache (NFR-11) : le service worker (sw.ts, route `public-directory`)
// sert une version en cache de cette lecture en cas de coupure réseau — le
// profil reste consultable hors ligne après une première visite.
//
// États observables :
//  - chargement        → indicateur de progression ;
//  - 404 (centre inconnu ou tenant désactivé, P8) → message « introuvable »
//                         dédié, jamais une page cassée ;
//  - autre erreur réseau → message + bouton « Réessayer » ;
//  - profil chargé      → champs non sensibles, contacts actionnables (tel:/mailto:).
//
// Mobile-first (NFR-9) : colonne unique pleine largeur, bouton retour et liens
// de contact à cible tactile >=44px. La page garde les props (`centerId`,
// `onBack`) câblées par `MapHome` (15.2) et `RegionCenters` (16).

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  fetchPublicCenter,
  REGION_LABELS,
  type PublicCenterDetail,
} from "../lib/api";

export interface CenterProfileProps {
  /** Identifiant du centre sélectionné depuis la carte / l'annuaire. */
  centerId: string;
  /** Retour à l'écran précédent (carte ou annuaire). */
  onBack?: () => void;
}

type LoadState =
  | { phase: "loading" }
  | { phase: "notFound" }
  | { phase: "error" }
  | { phase: "ready"; center: PublicCenterDetail };

export default function CenterProfile({
  centerId,
  onBack,
}: CenterProfileProps) {
  const { t } = useTranslation();
  const [state, setState] = useState<LoadState>({ phase: "loading" });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setState({ phase: "loading" });

    fetchPublicCenter(centerId, controller.signal)
      .then((center) => {
        if (active) setState({ phase: "ready", center });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || !active) return;
        // 404 : centre inconnu ou tenant désactivé (P8) — message dédié.
        if (err instanceof ApiError && err.status === 404) {
          setState({ phase: "notFound" });
          return;
        }
        setState({ phase: "error" });
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [centerId, reloadToken]);

  return (
    <section className="flex flex-col gap-3">
      {onBack && (
        <button
          type="button"
          onClick={onBack}
          className="inline-flex min-h-11 w-fit items-center gap-1 text-sm
            font-medium text-sky-700 active:opacity-70"
        >
          <span aria-hidden="true">←</span>
          {t("pages.centerProfile.back")}
        </button>
      )}

      {state.phase === "loading" && (
        <>
          <h1 className="text-xl font-semibold md:text-2xl">
            {t("pages.centerProfile.title")}
          </h1>
          <p
            className="py-8 text-center text-sm text-slate-500"
            role="status"
            aria-live="polite"
          >
            {t("pages.centerProfile.loading")}
          </p>
        </>
      )}

      {state.phase === "notFound" && (
        <>
          <h1 className="text-xl font-semibold md:text-2xl">
            {t("pages.centerProfile.title")}
          </h1>
          <p
            className="rounded-lg border border-slate-200 bg-white p-4
              text-center text-sm text-slate-500"
            role="status"
          >
            {t("pages.centerProfile.notFound")}
          </p>
        </>
      )}

      {state.phase === "error" && (
        <>
          <h1 className="text-xl font-semibold md:text-2xl">
            {t("pages.centerProfile.title")}
          </h1>
          <div className="py-8 text-center" role="alert">
            <p className="text-sm text-red-700">
              {t("pages.centerProfile.loadError")}
            </p>
            <button
              type="button"
              onClick={() => setReloadToken((n) => n + 1)}
              className="mt-3 inline-flex min-h-11 items-center rounded-lg border
                border-slate-300 bg-white px-4 text-sm font-medium text-sky-700
                active:bg-slate-100"
            >
              {t("pages.centerProfile.retry")}
            </button>
          </div>
        </>
      )}

      {state.phase === "ready" && (
        <CenterDetailView center={state.center} />
      )}
    </section>
  );
}

/** Rendu des champs non sensibles d'un centre (P4). Extrait pour la lisibilité. */
function CenterDetailView({ center }: { center: PublicCenterDetail }) {
  const { t } = useTranslation();

  return (
    <article className="flex flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold md:text-2xl">{center.name}</h1>
        <p className="text-sm text-slate-500">
          {t("pages.centerProfile.regionLabel")} :{" "}
          {REGION_LABELS[center.region_code]}
        </p>
      </header>

      {/* Adresse */}
      <Field label={t("pages.centerProfile.addressLabel")}>
        <p className="text-sm text-slate-700">
          {center.address ?? (
            <span className="text-slate-400">
              {t("pages.centerProfile.noAddress")}
            </span>
          )}
        </p>
      </Field>

      {/* Contact : téléphone + courriel actionnables (tel: / mailto:). */}
      <Field label={t("pages.centerProfile.contactLabel")}>
        {center.phone || center.email ? (
          <ul className="flex flex-col gap-2">
            {center.phone && (
              <li>
                <a
                  href={`tel:${center.phone}`}
                  aria-label={t("pages.centerProfile.callAria", {
                    phone: center.phone,
                  })}
                  className="inline-flex min-h-11 items-center gap-2 text-sm
                    font-medium text-sky-700 active:opacity-70"
                >
                  <span aria-hidden="true">📞</span>
                  {center.phone}
                </a>
              </li>
            )}
            {center.email && (
              <li>
                <a
                  href={`mailto:${center.email}`}
                  aria-label={t("pages.centerProfile.emailAria", {
                    email: center.email,
                  })}
                  className="inline-flex min-h-11 items-center gap-2 break-all
                    text-sm font-medium text-sky-700 active:opacity-70"
                >
                  <span aria-hidden="true">✉️</span>
                  {center.email}
                </a>
              </li>
            )}
          </ul>
        ) : (
          <p className="text-sm text-slate-400">
            {t("pages.centerProfile.noContact")}
          </p>
        )}
      </Field>

      {/* Horaires d'ouverture */}
      <Field label={t("pages.centerProfile.hoursLabel")}>
        {center.opening_hours.length > 0 ? (
          <ul className="flex flex-col gap-1">
            {center.opening_hours.map((h, i) => (
              <li
                key={`${h.day}-${i}`}
                className="flex justify-between gap-3 text-sm text-slate-700"
              >
                <span className="capitalize">{h.day}</span>
                <span className="tabular-nums text-slate-600">
                  {h.open} – {h.close}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-400">
            {t("pages.centerProfile.noHours")}
          </p>
        )}
      </Field>

      {/* Services proposés */}
      <Field label={t("pages.centerProfile.servicesLabel")}>
        {center.service_types.length > 0 ? (
          <ul className="flex flex-wrap gap-2">
            {center.service_types.map((svc) => (
              <li
                key={svc}
                className="rounded-full bg-sky-50 px-3 py-1 text-sm
                  text-sky-800"
              >
                {svc}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-400">
            {t("pages.centerProfile.noServices")}
          </p>
        )}
      </Field>

      {/* Coordonnées géographiques */}
      <Field label={t("pages.centerProfile.coordinatesLabel")}>
        {center.location ? (
          <p className="font-mono text-sm text-slate-700">
            {center.location.latitude.toFixed(5)},{" "}
            {center.location.longitude.toFixed(5)}
          </p>
        ) : (
          <p className="text-sm text-slate-400">
            {t("pages.centerProfile.noCoordinates")}
          </p>
        )}
      </Field>
    </article>
  );
}

/** Section étiquetée d'un champ de profil. */
function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-slate-200
      bg-white p-3">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
        {label}
      </h2>
      {children}
    </div>
  );
}
