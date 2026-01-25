import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import { readFileSync, copyFileSync, writeFileSync, mkdirSync } from 'fs'

// Plugin to copy PDF.js files to output directory
// Both pdf.mjs and pdf.worker.mjs are loaded externally to avoid Vite transforming
// ES private fields, which breaks compatibility with the fake worker fallback
function copyPdfFiles() {
  return {
    name: 'copy-pdf-files',
    writeBundle() {
      mkdirSync(resolve(__dirname, 'js'), { recursive: true })
      // Copy main library
      copyFileSync(
        resolve(__dirname, 'node_modules/pdfjs-dist/build/pdf.mjs'),
        resolve(__dirname, 'js/pdf.mjs')
      )
      console.log('Copied pdf.mjs to js/')
      // Copy worker (loaded by pdfjs at runtime)
      copyFileSync(
        resolve(__dirname, 'node_modules/pdfjs-dist/build/pdf.worker.mjs'),
        resolve(__dirname, 'js/pdf.worker.mjs')
      )
      console.log('Copied pdf.worker.mjs to js/')
      // Create loader script that imports pdf.mjs and sets window.pdfjsLib
      // This is loaded via script tag before the main app
      const loaderScript = `// PDF.js loader - imports pdf.mjs and exposes it globally
// Loaded before main app to make pdfjsLib available as window.pdfjsLib
import * as pdfjsLib from './pdf.mjs';
window.pdfjsLib = pdfjsLib;
`
      writeFileSync(resolve(__dirname, 'js/pdfjs-loader.mjs'), loaderScript)
      console.log('Created pdfjs-loader.mjs in js/')
    }
  }
}

// Read app info from info.xml for @nextcloud/vue
const infoXml = readFileSync(resolve(__dirname, 'appinfo/info.xml'), 'utf-8')
const appName = infoXml.match(/<id>([^<]+)<\/id>/)?.[1] || 'astrolabe'
const appVersion = infoXml.match(/<version>([^<]+)<\/version>/)?.[1] || ''

export default defineConfig({
  plugins: [vue(), copyPdfFiles()],
  define: {
    appName: JSON.stringify(appName),
    appVersion: JSON.stringify(appVersion),
  },
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
