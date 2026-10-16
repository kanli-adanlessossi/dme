// Tests de la page profil public `CenterProfile` (tâche 16) — exigence 7.4, P4.
//
// Comportement observable vérifié :
//   - profil chargé : nom, région, adresse, contact (tel:/mailto:), horaires,
//     services et coordonnées — uniquement des champs non sensibles (P4) ;
//   - 404 (centre inconnu / tenant désactivé, P8) : message « introuvable »
//     dédié, PAS une page cassée ni une alerte d'erreur ;
//   - autre erreur réseau : alerte + bouton « Réessayer » ;
//   - bouton retour câblé (`onBack`).
//
// Fichier en `.ts` : rendu via `react-dom` + `createElement`, `fetchPublicCenter`
// mocké depuis `../lib/api` (aucun appel réseau réel).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import i18n from "../i18n";
import { ApiError, type PublicCenterDetail } from "../lib/api";

const fetchPublicCenter = vi.fn<
  (id: string, signal?: AbortSignal) => Promise<PublicCenterDetail>
>();
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchPublicCenter: (id: string, signal?: AbortSignal) =>
      fetchPublicCenter(id, signal),
  };
});

import CenterProfile from "./CenterProfile";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CENTER: PublicCenterDetail = {
  id: "018f2a00-0000-7000-8000-000000000001",
  name: "Centre de santé de Lomé",
  region_code: "MARITIME",
  address: "12 Rue du Commerce, Lomé",
  location: { longitude: 1.2255, latitude: 6.1319 },
  phone: "+22890000000",
  email: "contact@centre-lome.tg",
  opening_hours: [{ day: "lundi", open: "08:00", close: "17:00" }],
  service_types: ["maternité", "pharmacie"],
};

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function render(props: {
  centerId?: string;
  onBack?: () => void;
}): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(
        I18nextProvider,
        { i18n },
        createElement(CenterProfile, {
          centerId: props.centerId ?? CENTER.id,
          onBack: props.onBack,
        }),
      ),
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  fetchPublicCenter.mockReset();
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  container = null;
  root = null;
  vi.restoreAllMocks();
});

describe("CenterProfile — profil chargé (exigence 7.4)", () => {
  it("affiche les champs publics non sensibles", async () => {
    fetchPublicCenter.mockResolvedValue(CENTER);
    await render({});

    const text = container!.textContent ?? "";
    expect(text).toContain("Centre de santé de Lomé");
    expect(text).toContain("Maritime");
    expect(text).toContain("12 Rue du Commerce, Lomé");
    expect(text).toContain("+22890000000");
    expect(text).toContain("contact@centre-lome.tg");
    expect(text).toContain("maternité");
    expect(text).toContain("pharmacie");
    // Coordonnées projetées (lat, lon).
    expect(text).toContain("6.13190");
    expect(text).toContain("1.22550");
  });

  it("rend des liens de contact actionnables (tel: / mailto:)", async () => {
    fetchPublicCenter.mockResolvedValue(CENTER);
    await render({});

    const links = Array.from(container!.querySelectorAll("a"));
    const tel = links.find((a) => a.getAttribute("href")?.startsWith("tel:"));
    const mail = links.find((a) =>
      a.getAttribute("href")?.startsWith("mailto:"),
    );
    expect(tel?.getAttribute("href")).toBe("tel:+22890000000");
    expect(mail?.getAttribute("href")).toBe("mailto:contact@centre-lome.tg");
  });

  it("ne divulgue aucun champ interne (P4) : pas de tenant_id / statut", async () => {
    fetchPublicCenter.mockResolvedValue(CENTER);
    await render({});
    const text = (container!.textContent ?? "").toLowerCase();
    expect(text).not.toContain("tenant");
    expect(text).not.toContain("patient");
  });
});

describe("CenterProfile — centre introuvable (404, P8)", () => {
  it("affiche un message dédié, pas une alerte d'erreur", async () => {
    fetchPublicCenter.mockRejectedValue(new ApiError(404, "introuvable"));
    await render({});

    expect(container!.querySelector('[role="alert"]')).toBeNull();
    expect(container!.textContent ?? "").toContain(
      "introuvable ou n'est plus disponible",
    );
  });
});

describe("CenterProfile — erreur réseau", () => {
  it("affiche une alerte + bouton Réessayer sur échec non-404", async () => {
    fetchPublicCenter.mockRejectedValue(new ApiError(503, "indisponible"));
    await render({});

    expect(container!.querySelector('[role="alert"]')).not.toBeNull();
    const retry = Array.from(container!.querySelectorAll("button")).find((b) =>
      (b.textContent ?? "").includes("Réessayer"),
    );
    expect(retry).toBeTruthy();
    expect(retry!.className).toContain("min-h-11");
  });
});

describe("CenterProfile — navigation retour", () => {
  it("appelle onBack au clic sur le bouton retour", async () => {
    fetchPublicCenter.mockResolvedValue(CENTER);
    const onBack = vi.fn();
    await render({ onBack });

    const back = Array.from(container!.querySelectorAll("button")).find((b) =>
      (b.textContent ?? "").includes("Retour"),
    );
    expect(back).toBeTruthy();
    act(() => {
      back!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
