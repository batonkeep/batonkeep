/** @type {import('tailwindcss').Config} */
// Mission-control / terminal control-room palette (PLAN.md §12, CLAUDE.md design system).
// Colours are CSS-variable-backed (channels in `R G B`) so the same token names
// theme between light (default) and dark — see src/index.css. `<alpha-value>`
// keeps Tailwind's /opacity utilities (bg-amber/10, border-edge/40) working.
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "rgb(var(--c-base) / <alpha-value>)", // page background
        panel: "rgb(var(--c-panel) / <alpha-value>)", // raised surface
        edge: "rgb(var(--c-edge) / <alpha-value>)", // hairline borders
        ink: "rgb(var(--c-ink) / <alpha-value>)", // primary text
        muted: "rgb(var(--c-muted) / <alpha-value>)", // dimmed text
        amber: "rgb(var(--c-amber) / <alpha-value>)", // the one signal accent
        live: "rgb(var(--c-live) / <alpha-value>)", // cyan — live/streaming ONLY
        ok: "rgb(var(--c-ok) / <alpha-value>)",
        warn: "rgb(var(--c-warn) / <alpha-value>)",
        bad: "rgb(var(--c-bad) / <alpha-value>)",
        defer: "rgb(var(--c-defer) / <alpha-value>)",
        // Fixed near-black for text sitting on bright amber/defer fills (both themes).
        coal: "#0a0b0d",
      },
      fontFamily: {
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
        sans: ['"IBM Plex Sans"', "ui-sans-serif", "system-ui", "sans-serif"],
      },
      keyframes: {
        "rise-in": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-live": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
      },
      animation: {
        "rise-in": "rise-in 0.5s cubic-bezier(0.16,1,0.3,1) both",
        "pulse-live": "pulse-live 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
