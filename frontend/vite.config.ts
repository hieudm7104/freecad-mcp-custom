import { defineConfig } from "vite";

// No @vitejs/plugin-react: esbuild already compiles .tsx with the automatic
// JSX runtime from tsconfig's "jsx": "react-jsx". We lose fast refresh (edits
// full-reload instead), which is not worth a dependency here.
export default defineConfig({
  // Sourcemaps in the production build on purpose. This is a private,
  // loopback-only tool, and the one bug report it can produce is a stack from
  // the error boundary — worthless when it reads `TypeError: d is not a
  // function at Bu`. Costs a .map file nobody downloads unless devtools is
  // open.
  build: { sourcemap: true },
  // Dev only. The production build is static files served by the harness
  // itself, so every path in the app is relative ("/api/..."), never an origin.
  server: { proxy: { "/api": "http://localhost:8003" } },
});
