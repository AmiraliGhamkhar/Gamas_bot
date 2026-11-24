/**
 * The Gamas Mini App theme.
 *
 * Every value here comes from the Gamas identity documented in
 * `docs/BOOKLET_DESIGN.md` and pinned in `gamas_bot/document/theme.py`
 * (`Palette`): deep navy structure, teal/cyan accent, warm cream paper,
 * ink-black text — and red reserved for warnings, failures and critical
 * distinctions only. Components never hardcode colours (`src/test/design_lint.test.ts`
 * fails the build on a raw hex literal in `src/`).
 *
 * Surface registers (see `miniapp/DESIGN.md`):
 *   chrome  — bg / surface / surface2 / surface3 (navy ink scale)
 *   paper   — paper / paper2 with ink text (study content only)
 *   banner  — banner (booklet navy_700), one per screen at most
 */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // --- chrome: navy ink scale (darkened from Palette.navy_950…navy_700)
        bg: "#03181F",
        surface: "#07242D",
        surface2: "#0A2F3A",
        surface3: "#0E3C49",
        line: "#16414D",
        line2: "#103843",
        text: "#EDF4F5",
        muted: "#9CBAC3",
        faint: "#70939E",
        // --- accent: Palette.teal_400 / derived ink
        brand: "#3FC0CB",
        brandInk: "#04222A",
        brandSoft: "#0B3641",
        // --- paper register: Palette.cream_50 / cream_100 / ink / muted
        paper: "#FFF9ED",
        paper2: "#F8F2E6",
        ink: "#102B36",
        inkMuted: "#60747C",
        // ECG trace on paper: Palette.teal_700
        trace: "#087F8C",
        // --- status: Palette semantics; red is reserved.
        ok: "#4ED6A6",
        okSoft: "#0E2E27",
        okInk: "#1F8A70",
        bad: "#FF8A8A",
        badSoft: "#2E1A1D",
        badInk: "#A73822",
        // --- the navy banner register: Palette.navy_700
        banner: "#0B4658",
      },
      fontFamily: {
        sans: [
          "Vazirmatn Variable",
          "Vazirmatn",
          "Segoe UI",
          "Tahoma",
          "system-ui",
          "sans-serif",
        ],
      },
      borderRadius: {
        card: "20px",
        field: "14px",
        pill: "999px",
      },
      boxShadow: {
        // The chrome is flat; paper is the one surface allowed to lift.
        paper: "0 14px 34px -18px rgba(0,0,0,0.65), 0 2px 8px -4px rgba(0,0,0,0.4)",
        float: "0 -8px 30px -18px rgba(0,0,0,0.9)",
        brand: "0 10px 24px -14px rgba(63,192,203,0.45)",
      },
      transitionTimingFunction: {
        smooth: "cubic-bezier(0.22, 0.61, 0.36, 1)",
      },
      keyframes: {
        rise: {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        fade: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        sheet: {
          "0%": { opacity: "0", transform: "translateY(28px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "200% 0" },
          "100%": { backgroundPosition: "-200% 0" },
        },
        // The Gamas pulse: one segment of the ECG trace sweeping the line.
        ecg: {
          "0%": { strokeDashoffset: "560" },
          "100%": { strokeDashoffset: "0" },
        },
      },
      animation: {
        rise: "rise 0.28s cubic-bezier(0.22,0.61,0.36,1) both",
        fade: "fade 0.22s ease-out both",
        sheet: "sheet 0.26s cubic-bezier(0.22,0.61,0.36,1) both",
        shimmer: "shimmer 1.5s linear infinite",
        ecg: "ecg 2.2s linear infinite",
      },
    },
  },
  plugins: [],
};
