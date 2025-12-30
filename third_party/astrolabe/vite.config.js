import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '.',
    emptyOutDir: false,
    cssCodeSplit: false,  // Bundle all CSS into entry points (Nextcloud doesn't load CSS chunks)
    rollupOptions: {
      input: {
        'astrolabe-main': resolve(__dirname, 'src/main.js'),
        'astrolabe-adminSettings': resolve(__dirname, 'src/adminSettings.js'),
        'astrolabe-personalSettings': resolve(__dirname, 'src/personalSettings.js'),
      },
      output: {
        entryFileNames: 'js/[name].mjs',
        chunkFileNames: 'js/[name]-[hash].chunk.mjs',
        assetFileNames: (assetInfo) => {
          // With cssCodeSplit:false, all CSS goes to a single file
          // Name it astrolabe-main.css to match Nextcloud's Util::addStyle expectation
          if (assetInfo.name && assetInfo.name.endsWith('.css')) {
            return 'css/astrolabe-main.css';
          }
          return 'js/[name][extname]';
        },
      },
    },
    sourcemap: true,
    minify: 'terser',
  },
})
