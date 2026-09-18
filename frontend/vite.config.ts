import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../backend/static',
    emptyOutDir: true,
    // T-0275 (28.05.): Bundle-Split — vorher 698 kB Hauptbundle.
    // Vendor-Split trennt Recharts (~330 kB) + React-Core vom App-
    // Code; React.lazy in App.tsx splittet die 3 schwersten Tabs
    // (Historie/Ops/Freigabe), die nur bei Klick geladen werden.
    chunkSizeWarningLimit: 400,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          // Vendor-Splits: trennt schwere Libraries vom App-Code.
          if (id.includes('node_modules/recharts')) return 'vendor-recharts'
          if (id.includes('node_modules/react-dom') || id.includes('node_modules/react/')) {
            return 'vendor-react'
          }
          return undefined
        },
      },
    },
  },
})
