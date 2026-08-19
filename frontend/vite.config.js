import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
    plugins: [react()],
    css: { modules: { localsConvention: 'camelCaseOnly' } },
    server: {
        proxy: {
            // Frontend calls /api/*; forward to the FastAPI backend on :8000.
            // Strips /api so /api/query -> /query. SSE streaming works through it.
            '/api': {
                target: 'http://127.0.0.1:8000',
                changeOrigin: true,
                rewrite: function (path) { return path.replace(/^\/api/, ''); },
            },
        },
    },
});
