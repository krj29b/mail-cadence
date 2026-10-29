/** Tailwind build for the web panel (run `make assets`). */
module.exports = {
  content: [
    "../src/mail_cadence/web_templates/**/*.html",
    "../src/mail_cadence/web_static/*.js",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        slate: {
          50: "rgb(var(--slate-50) / <alpha-value>)",
          100: "rgb(var(--slate-100) / <alpha-value>)",
          200: "rgb(var(--slate-200) / <alpha-value>)",
          300: "rgb(var(--slate-300) / <alpha-value>)",
          400: "rgb(var(--slate-400) / <alpha-value>)",
          500: "rgb(var(--slate-500) / <alpha-value>)",
          600: "rgb(var(--slate-600) / <alpha-value>)",
          700: "rgb(var(--slate-700) / <alpha-value>)",
          800: "rgb(var(--slate-800) / <alpha-value>)",
          900: "rgb(var(--slate-900) / <alpha-value>)",
          950: "rgb(var(--slate-950) / <alpha-value>)",
        },
        indigo: {
          200: "rgb(var(--indigo-200) / <alpha-value>)",
          300: "rgb(var(--indigo-300) / <alpha-value>)",
          400: "rgb(var(--indigo-400) / <alpha-value>)",
          500: "rgb(var(--indigo-500) / <alpha-value>)",
          600: "rgb(var(--indigo-600) / <alpha-value>)",
          700: "rgb(var(--indigo-700) / <alpha-value>)",
        },
        emerald: {
          400: "rgb(var(--emerald-400) / <alpha-value>)",
          500: "rgb(var(--emerald-500) / <alpha-value>)",
        },
        rose: {
          400: "rgb(var(--rose-400) / <alpha-value>)",
          500: "rgb(var(--rose-500) / <alpha-value>)",
        },
        amber: {
          400: "rgb(var(--amber-400) / <alpha-value>)",
          500: "rgb(var(--amber-500) / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: ["Inter Variable", "Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono Variable", "JetBrains Mono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
