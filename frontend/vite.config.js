import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/ask': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/export': 'http://localhost:8000',
    },
  },
})
