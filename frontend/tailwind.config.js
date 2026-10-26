/** Tailwind build for the web panel (run `make assets`). */
module.exports = {
  content: [
    "../src/mail_cadence/web_templates/**/*.html",
    "../src/mail_cadence/web_static/*.js",
  ],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter Variable", "Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono Variable", "JetBrains Mono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
