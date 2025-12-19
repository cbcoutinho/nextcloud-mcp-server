<template>
	<!-- eslint-disable-next-line vue/no-v-html -->
	<div class="markdown-viewer" v-html="html" />
</template>

<script>
import MarkdownIt from 'markdown-it'

export default {
	name: 'MarkdownViewer',

	props: {
		content: {
			type: String,
			required: true,
		},
	},

	data() {
		const md = new MarkdownIt({
			html: false, // Disable HTML for security
			linkify: true,
			breaks: true,
			typographer: true,
		})

		return {
			html: '',
			md,
		}
	},

	watch: {
		content: {
			immediate: true,
			handler(newContent) {
				this.renderMarkdown(newContent)
			},
		},
	},

	methods: {
		renderMarkdown(text) {
			if (!text) {
				this.html = ''
				return
			}

			try {
				this.html = this.md.render(text)
			} catch (error) {
				console.error('Markdown rendering error:', error)
				// Fallback to escaped plain text
				this.html = `<pre>${this.escapeHtml(text)}</pre>`
			}
		},

		escapeHtml(text) {
			const div = document.createElement('div')
			div.textContent = text
			return div.innerHTML
		},
	},
}
</script>

<style scoped lang="scss">
.markdown-viewer {
	font-size: 14px;
	line-height: 1.6;
	color: var(--color-main-text);
	word-wrap: break-word;
	overflow-wrap: break-word;

	// Typography
	:deep(h1), :deep(h2), :deep(h3), :deep(h4), :deep(h5), :deep(h6) {
		margin-top: 24px;
		margin-bottom: 16px;
		font-weight: 600;
		line-height: 1.25;
		color: var(--color-main-text);
	}

	:deep(h1) { font-size: 2em; border-bottom: 1px solid var(--color-border); padding-bottom: 8px; }
	:deep(h2) { font-size: 1.5em; border-bottom: 1px solid var(--color-border); padding-bottom: 8px; }
	:deep(h3) { font-size: 1.25em; }
	:deep(h4) { font-size: 1em; }
	:deep(h5) { font-size: 0.875em; }
	:deep(h6) { font-size: 0.85em; color: var(--color-text-maxcontrast); }

	// Paragraphs and spacing
	:deep(p) {
		margin-top: 0;
		margin-bottom: 16px;
	}

	// Lists
	:deep(ul), :deep(ol) {
		margin-top: 0;
		margin-bottom: 16px;
		padding-left: 2em;
	}

	:deep(li) {
		margin-bottom: 4px;
	}

	// Code blocks
	:deep(code) {
		padding: 2px 6px;
		background: var(--color-background-dark);
		border-radius: var(--border-radius);
		font-family: 'Courier New', Courier, monospace;
		font-size: 0.9em;
	}

	:deep(pre) {
		background: var(--color-background-dark);
		padding: 16px;
		border-radius: var(--border-radius);
		overflow-x: auto;
		margin-bottom: 16px;

		code {
			padding: 0;
			background: transparent;
		}
	}

	// Blockquotes
	:deep(blockquote) {
		margin: 0 0 16px 0;
		padding: 0 16px;
		border-left: 4px solid var(--color-primary-element);
		color: var(--color-text-maxcontrast);

		p:last-child {
			margin-bottom: 0;
		}
	}

	// Links
	:deep(a) {
		color: var(--color-primary-element);
		text-decoration: none;

		&:hover {
			text-decoration: underline;
		}
	}

	// Tables
	:deep(table) {
		border-collapse: collapse;
		width: 100%;
		margin-bottom: 16px;
	}

	:deep(th), :deep(td) {
		padding: 8px 12px;
		border: 1px solid var(--color-border);
		text-align: left;
	}

	:deep(th) {
		background: var(--color-background-dark);
		font-weight: 600;
	}

	// Horizontal rule
	:deep(hr) {
		border: none;
		border-top: 1px solid var(--color-border);
		margin: 24px 0;
	}

	// Images
	:deep(img) {
		max-width: 100%;
		height: auto;
		display: block;
		margin: 16px 0;
	}
}
</style>
