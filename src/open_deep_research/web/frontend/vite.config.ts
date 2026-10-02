import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

const here = fileURLToPath(new URL('.', import.meta.url))
export default defineConfig({
  plugins: [react()],
  base: '/static/dist/',
  resolve: {
    alias: {
      '@trajectory-upstream': `${here}/src/trajectory/upstream/src/client/TrajectoryView.tsx`,
      '@deepseek-ai/dsh-client-ui-primitives': `${here}/src/trajectory/compat/primitives.ts`,
    },
  },
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
})
