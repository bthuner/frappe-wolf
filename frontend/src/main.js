import { createApp } from 'vue'

import App from './App.vue'
import { makeRouter } from './router'
import { loadBoot } from './services/boot'
import './style.css'

loadBoot()
  .then((boot) => {
    if (!boot) return // on its way to the login page
    const app = createApp(App)
    app.provide('boot', boot)
    app.use(makeRouter(undefined, boot))
    app.mount('#app')
  })
  .catch((error) => {
    // no app to render into: say so plainly instead of leaving a blank page
    document.getElementById('app').textContent = `Frappe Wolf could not start: ${error.message}`
  })
