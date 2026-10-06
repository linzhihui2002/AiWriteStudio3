import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端 FastAPI 固定监听 127.0.0.1:8790，API 前缀 /api。
// 开发服务器使用 Vite 默认端口 5173（禁止使用 3080 / 8765 / 3456）。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8790',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: (id) => id.replace(/\\/g, '/').includes('/node_modules/cytoscape/') ? 'cytoscape' : undefined,
      },
    },
  },
})
