/** @type {import('tailwindcss').Config} */
// Configuration Tailwind — styles mobile-first (NFR-9).
// Base conçue pour petits écrans (>=320px) ; points de rupture md:/lg: pour
// enrichir progressivement la mise en page (tablette/ordinateur).
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    // Points de rupture mobile-first : les styles de base ciblent 320–430px,
    // `md:`/`lg:` enrichissent au-delà.
    screens: {
      md: "768px",
      lg: "1024px",
    },
    extend: {
      spacing: {
        // Cible tactile minimale (NFR-9 : >=44px, atteignable au pouce).
        touch: "44px",
      },
      minHeight: {
        touch: "44px",
      },
      minWidth: {
        touch: "44px",
      },
    },
  },
  plugins: [],
};
