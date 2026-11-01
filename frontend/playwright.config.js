import { defineConfig, devices } from '@playwright/test'

// @axe-core/playwright stays on 4.13: 4.14 requires Node 22.12, and `bench build` installs the dev
// dependencies on benches that run Node 20.19 (frappe-ui's minimum).
// Browser tests of the production build (e2e/server.mjs) with the Wolf API mocked in the page
// (e2e/api.js). `yarn build` first. The server listens on WOLF_E2E_PORT (4183). WOLF_CHROMIUM points at a preinstalled Chromium; CI installs
// Playwright's own with `npx playwright install --with-deps chromium`.
const executablePath = process.env.WOLF_CHROMIUM || undefined
// not Vite's 4173 (vite preview), and never another server already listening there
const port = Number(process.env.WOLF_E2E_PORT || 4183)
const origin = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: 'e2e',
  timeout: 30000,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: { baseURL: origin, trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], launchOptions: executablePath ? { executablePath } : {} } },
  ],
  webServer: { command: 'node e2e/server.mjs', url: `${origin}/wolf/`, reuseExistingServer: false, env: { WOLF_E2E_PORT: String(port) } },
})
