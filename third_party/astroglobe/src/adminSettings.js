/**
 * Admin settings page JavaScript for Astroglobe.
 *
 * Handles:
 * - Loading webhook presets
 * - Enabling/disabling webhook presets
 * - Search settings form submission
 */

import { generateUrl } from '@nextcloud/router'
import axios from '@nextcloud/axios'

document.addEventListener('DOMContentLoaded', () => {
	// Initialize search settings form
	initSearchSettingsForm()

	// Initialize webhook management (only if webhook section exists)
	if (document.getElementById('webhook-presets')) {
		initWebhookManagement()
	}
})

/**
 * Initialize search settings form handling.
 */
function initSearchSettingsForm() {
	const form = document.getElementById('astroglobe-search-settings-form')
	if (!form) return

	const scoreThresholdInput = document.getElementById('search-score-threshold')
	const scoreThresholdValue = document.getElementById('score-threshold-value')

	// Update score threshold display when slider changes
	if (scoreThresholdInput && scoreThresholdValue) {
		scoreThresholdInput.addEventListener('input', (e) => {
			scoreThresholdValue.textContent = e.target.value + '%'
		})
	}

	// Handle form submission
	form.addEventListener('submit', async (e) => {
		e.preventDefault()

		const formData = new FormData(form)
		const data = {
			algorithm: formData.get('algorithm'),
			fusion: formData.get('fusion'),
			scoreThreshold: parseInt(formData.get('scoreThreshold')),
			limit: parseInt(formData.get('limit')),
		}

		const statusEl = document.getElementById('search-settings-status')
		if (statusEl) {
			statusEl.textContent = 'Saving...'
			statusEl.className = 'mcp-status-message'
		}

		try {
			const response = await axios.post(
				generateUrl('/apps/astroglobe/api/admin/search-settings'),
				data,
				{ headers: { 'Content-Type': 'application/json' } },
			)

			if (response.data.success) {
				if (statusEl) {
					statusEl.textContent = '✓ Settings saved'
					statusEl.className = 'mcp-status-message success'
					setTimeout(() => {
						statusEl.textContent = ''
					}, 3000)
				}
			}
		} catch (error) {
			console.error('Failed to save search settings:', error)
			if (statusEl) {
				statusEl.textContent = '✗ Failed to save'
				statusEl.className = 'mcp-status-message error'
			}
		}
	})
}

/**
 * Initialize webhook management UI.
 */
async function initWebhookManagement() {
	const container = document.getElementById('webhook-presets-container')
	if (!container) return

	try {
		// Load webhook presets from API
		const response = await axios.get(
			generateUrl('/apps/astroglobe/api/admin/webhooks/presets'),
		)

		if (!response.data.success) {
			throw new Error(response.data.error || 'Failed to load presets')
		}

		const presets = response.data.presets
		renderWebhookPresets(container, presets)
	} catch (error) {
		console.error('Failed to load webhook presets:', error)
		container.innerHTML = `
			<div class="notecard notecard-error">
				<p><strong>Error loading webhook presets:</strong></p>
				<p>${error.message || 'Unknown error'}</p>
			</div>
		`
	}
}

/**
 * Render webhook preset cards.
 *
 * @param {HTMLElement} container Container element
 * @param {object} presets Preset configurations
 */
function renderWebhookPresets(container, presets) {
	const presetIds = Object.keys(presets)

	if (presetIds.length === 0) {
		container.innerHTML = `
			<div class="notecard notecard-info">
				<p>No webhook presets available. Install supported apps (Notes, Calendar, Tables, Forms) to enable webhooks.</p>
			</div>
		`
		return
	}

	// Create preset cards grid
	const grid = document.createElement('div')
	grid.className = 'mcp-preset-grid'

	presetIds.forEach(presetId => {
		const preset = presets[presetId]
		const card = createPresetCard(presetId, preset)
		grid.appendChild(card)
	})

	container.innerHTML = ''
	container.appendChild(grid)
}

/**
 * Create a webhook preset card.
 *
 * @param {string} presetId Preset ID
 * @param {object} preset Preset configuration
 * @return {HTMLElement} Card element
 */
function createPresetCard(presetId, preset) {
	const card = document.createElement('div')
	card.className = 'mcp-preset-card'
	card.dataset.presetId = presetId

	const statusClass = preset.enabled ? 'enabled' : 'disabled'
	const statusText = preset.enabled ? 'Enabled' : 'Disabled'
	const buttonText = preset.enabled ? 'Disable' : 'Enable'
	const buttonClass = preset.enabled ? 'secondary' : 'primary'

	card.innerHTML = `
		<div class="mcp-preset-header">
			<h4>${escapeHtml(preset.name)}</h4>
			<span class="mcp-preset-status mcp-status-${statusClass}">${statusText}</span>
		</div>
		<p class="mcp-preset-description">${escapeHtml(preset.description)}</p>
		<div class="mcp-preset-meta">
			<span class="mcp-preset-app">App: ${escapeHtml(preset.app)}</span>
			<span class="mcp-preset-events">${preset.events.length} events</span>
		</div>
		<div class="mcp-preset-actions">
			<button class="mcp-preset-toggle ${buttonClass}" data-preset-id="${presetId}">
				${buttonText}
			</button>
		</div>
	`

	// Attach event listener to toggle button
	const toggleBtn = card.querySelector('.mcp-preset-toggle')
	toggleBtn.addEventListener('click', () => togglePreset(presetId, preset.enabled))

	return card
}

/**
 * Toggle a webhook preset (enable/disable).
 *
 * @param {string} presetId Preset ID
 * @param {boolean} currentlyEnabled Current enabled state
 */
async function togglePreset(presetId, currentlyEnabled) {
	const card = document.querySelector(`[data-preset-id="${presetId}"]`)
	if (!card) return

	const toggleBtn = card.querySelector('.mcp-preset-toggle')
	const originalText = toggleBtn.textContent

	// Disable button during request
	toggleBtn.disabled = true
	toggleBtn.textContent = currentlyEnabled ? 'Disabling...' : 'Enabling...'

	try {
		const action = currentlyEnabled ? 'disable' : 'enable'
		const url = generateUrl(`/apps/astroglobe/api/admin/webhooks/presets/${presetId}/${action}`)

		const response = await axios.post(url)

		if (!response.data.success) {
			throw new Error(response.data.error || `Failed to ${action} preset`)
		}

		// Reload presets to update UI
		await initWebhookManagement()

		// Show success notification
		OC.Notification.showTemporary(response.data.message || `Preset ${action}d successfully`)
	} catch (error) {
		console.error(`Failed to toggle preset ${presetId}:`, error)

		// Restore button state
		toggleBtn.disabled = false
		toggleBtn.textContent = originalText

		// Show error notification
		OC.Notification.showTemporary(
			error.message || 'Failed to toggle webhook preset',
			{ type: 'error' },
		)
	}
}

/**
 * Escape HTML to prevent XSS.
 *
 * @param {string} text Text to escape
 * @return {string} Escaped text
 */
function escapeHtml(text) {
	const div = document.createElement('div')
	div.textContent = text
	return div.innerHTML
}
