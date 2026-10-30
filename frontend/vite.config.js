import vue from '@vitejs/plugin-vue'
import frappeui from 'frappe-ui/vite'
import path from 'node:path'
import { defineConfig } from 'vite'

// The build writes the bundle to frappe_wolf/public/frontend and the page to
// frappe_wolf/www/wolf.html, which www/wolf.py serves for /wolf and every route under it.
export default defineConfig({
  plugins: [
    frappeui({
      frontendRoute: '/wolf',
      buildConfig: {
        outDir: path.resolve(__dirname, '../frappe_wolf/public/frontend'),
        indexHtmlPath: path.resolve(__dirname, '../frappe_wolf/www/wolf.html'),
      },
    }),
    vue(),
  ],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.spec.js'],
    // frappe-ui ships its TypeScript source: let Vite transform it instead of Node
    server: { deps: { inline: ['frappe-ui'] } },
  },
})
