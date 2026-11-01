import { createApp, nextTick } from 'vue'
import { START_LOCATION } from 'vue-router'

import App from './App.vue'
import { makeRouter } from './router'
import { loadBoot } from './services/boot'
import { focusPageTitle, installFocusGuard } from './services/focus'
import { createLive } from './services/live'
import './style.css'

loadBoot()
  .then((boot) => {
    if (!boot) return // on its way to the login page
    const app = createApp(App)
    app.provide('boot', boot)
    app.provide('live', createLive(boot))
    const router = makeRouter(undefined, boot)
    // a new page: its title takes focus (not on the first load, nor when only the query or
    // the hash changes, as filters and row links do)
    router.afterEach((to, from, failure) => {
      if (failure || from === START_LOCATION || to.path === from.path) return
      nextTick(() => focusPageTitle())
    })
    app.use(router)
    app.mount('#app')
    installFocusGuard()
  })
  .catch((error) => {
    // no app to render into: say so plainly instead of leaving a blank page
    document.getElementById('app').textContent = `Frappe Wolf could not start: ${error.message}`
  })
