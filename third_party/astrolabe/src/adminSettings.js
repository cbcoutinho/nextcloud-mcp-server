/**
 * Admin settings page Vue app for Astrolabe.
 *
 * Mounts the AdminSettings Vue component for async loading
 * and improved UX.
 */

import { createApp } from 'vue'
import { translate as t, translatePlural as n } from '@nextcloud/l10n'
import AdminSettings from './components/admin/AdminSettings.vue'

const app = createApp(AdminSettings)

// Add translation methods globally
app.config.globalProperties.t = t
app.config.globalProperties.n = n

app.mount('#astrolabe-admin-settings')
