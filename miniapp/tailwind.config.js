/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // One ink scale and one accent: no per-screen palettes.
        bg: "#0B0C0E",
        surface: "#14161A",
        surface2: "#191C21",
        surface3: "#1F2329",
        line: "#24272D",
        line2: "#1D2025",
        text: "#F2F3F5",
        muted: "#A6ACB4",
        faint: "#8A919B",
        brand: "#F0B54A",
        brandInk: "#1B1305",
        brandSoft: "#2A2214",
        ok: "#4ED6A6",
        okSoft: "#102A24",
        bad: "#FF8A8A",
        badSoft: "#301A1D",
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
        card: "0 1px 0 rgba(255,255,255,0.03) inset, 0 10px 30px -18px rgba(0,0,0,0.9)",
        float: "0 -8px 30px -18px rgba(0,0,0,0.9)",
        brand: "0 10px 24px -14px rgba(240,181,74,0.55)",
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
        pop: {
          "0%": { transform: "scale(0.96)" },
          "60%": { transform: "scale(1.01)" },
          "100%": { transform: "scale(1)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "200% 0" },
          "100%": { backgroundPosition: "-200% 0" },
        },
      },
      animation: {
        rise: "rise 0.28s cubic-bezier(0.22,0.61,0.36,1) both",
        fade: "fade 0.22s ease-out both",
        pop: "pop 0.18s ease-out both",
        shimmer: "shimmer 1.5s linear infinite",
      },
    },
  },
  plugins: [],
};
