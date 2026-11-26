import { createApp } from 'vue'
import '@fontsource/geist/400.css'
import '@fontsource/geist/500.css'
import '@fontsource/geist/600.css'
import '@fontsource/geist/700.css'
// Tokens first: the Bootstrap theme and every component read them.
import './styles/tokens.css'
import './styles/bootstrap-theme.scss'
import 'bootstrap'
import './assets/main.css'
import App from './App.vue'
import router from './router'
import { initTheme } from './theme/useTheme'

initTheme()
createApp(App).use(router).mount('#app')
