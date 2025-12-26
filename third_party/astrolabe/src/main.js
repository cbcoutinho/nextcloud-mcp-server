import { createApp } from 'vue'
import { translate as t, translatePlural as n } from '@nextcloud/l10n'
import App from './App.vue'

const app = createApp(App)

// Add translation methods globally
app.config.globalProperties.t = t
app.config.globalProperties.n = n

app.mount('#astrolabe')
