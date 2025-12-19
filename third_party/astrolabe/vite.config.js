import { createAppConfig } from '@nextcloud/vite-config'

export default createAppConfig({
  main: 'src/main.js',
  adminSettings: 'src/adminSettings.js',
}, {
  inlineCSS: { relativeCSSInjection: true },
})
