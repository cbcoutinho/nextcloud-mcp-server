import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: 'js',
    emptyOutDir: false,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'src/main.js'),
        'astrolabe-adminSettings': resolve(__dirname, 'src/adminSettings.js'),
        'astrolabe-personalSettings': resolve(__dirname, 'src/personalSettings.js'),
      },
      output: {
        entryFileNames: '[name].mjs',
        chunkFileNames: '[name]-[hash].chunk.mjs',
        assetFileNames: '[name][extname]',
      },
    },
    sourcemap: true,
    minify: 'terser',
  },
})
