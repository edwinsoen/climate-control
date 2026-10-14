import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Ports come from the repository's root .env, shared with the backend.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '..', '')
  const backend = `http://127.0.0.1:${env.PORT || '8080'}`
  return {
    plugins: [react()],
    server: {
      port: Number(env.FRONTEND_PORT || 5173),
      // Fail instead of drifting to another port, which would no longer match APP_URL.
      strictPort: true,
      proxy: { '/api': backend, '/auth': backend, '/mcp': backend },
    },
  }
})
