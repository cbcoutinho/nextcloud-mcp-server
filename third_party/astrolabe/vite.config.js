import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '.',
    emptyOutDir: false,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'src/main.js'),
        'astrolabe-adminSettings': resolve(__dirname, 'src/adminSettings.js'),
        'astrolabe-personalSettings': resolve(__dirname, 'src/personalSettings.js'),
      },
      output: {
        entryFileNames: 'js/[name].mjs',
        chunkFileNames: 'js/[name]-[hash].chunk.mjs',
        assetFileNames: (assetInfo) => {
          // Output CSS to css/ directory, JS/other assets to js/
          if (assetInfo.name && assetInfo.name.endsWith('.css')) {
            return 'css/[name][extname]';
          }
          return 'js/[name][extname]';
        },
      },
    },
    sourcemap: true,
    minify: 'terser',
  },
})
