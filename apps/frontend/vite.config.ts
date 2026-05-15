import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  root: '.',
  appType: 'spa',
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Keep intentionally large visualization dependencies in named vendor
        // chunks so CI can enforce the app bundle budget separately.
        manualChunks: {
          'vendor-map': ['maplibre-gl', 'react-map-gl'],
          'vendor-charts': ['echarts', 'echarts-for-react'],
          react: ['react', 'react-dom', 'react-router-dom', 'zustand'],
          vendor: ['@radix-ui/react-dialog', '@radix-ui/react-select', '@radix-ui/react-toast', 'openapi-fetch'],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/health': { target: 'http://localhost:8000' },
    },
  },
})
