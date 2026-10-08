import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:5177' },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    emptyOutDir: false,
    reportCompressedSize: false,
    cssMinify: false,
    minify: false,
  },
})
