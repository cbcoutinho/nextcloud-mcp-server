/**
 * Personal settings page JavaScript for Astrolabe.
 *
 * Loads styles for the personal settings page and handles form interactions.
 */

import './styles/settings.css'

// Wait for DOM to be ready
document.addEventListener('DOMContentLoaded', function() {
	// Helper function to show error notifications
	function showError(message) {
		if (typeof OC !== 'undefined' && OC.Notification) {
			OC.Notification.showTemporary(message, { type: 'error' })
		} else {
			alert(message)
		}
	}

	function showSuccess(message) {
		if (typeof OC !== 'undefined' && OC.Notification) {
			OC.Notification.showTemporary(message, { type: 'success' })
		} else {
			alert(message)
		}
	}

	// App password form with error handling
	const appPasswordForm = document.getElementById('mcp-app-password-form')
	if (appPasswordForm) {
		appPasswordForm.addEventListener('submit', async function(e) {
			e.preventDefault()
			const submitButton = document.getElementById('mcp-save-app-password-button')
			const originalText = submitButton.textContent

			try {
				submitButton.disabled = true
				submitButton.textContent = t('astrolabe', 'Saving...')

				const formData = new FormData(appPasswordForm)
				const response = await fetch(appPasswordForm.action, {
					method: 'POST',
					body: formData,
				})

				const result = await response.json()

				if (response.ok && result.success) {
					showSuccess(t('astrolabe', 'Background sync access successfully provisioned!'))
					setTimeout(() => window.location.reload(), 1000)
				} else {
					showError(result.error || t('astrolabe', 'Failed to save app password. Please check that it is valid.'))
				}
			} catch (error) {
				console.error('App password provisioning error:', error)
				showError(t('astrolabe', 'Unable to connect to server. Please check that the MCP server is running and try again.'))
			} finally {
				submitButton.disabled = false
				submitButton.textContent = originalText
			}
		})
	}

	// Revoke form confirmation
	const revokeForm = document.getElementById('mcp-revoke-form')
	if (revokeForm) {
		revokeForm.addEventListener('submit', function(e) {
			if (!confirm(t('astrolabe', 'Are you sure you want to disable indexing? Your content will be removed from semantic search.'))) {
				e.preventDefault()
			}
		})
	}

	// Disconnect form confirmation
	const disconnectForm = document.getElementById('mcp-disconnect-form')
	if (disconnectForm) {
		disconnectForm.addEventListener('submit', function(e) {
			if (!confirm(t('astrolabe', 'Are you sure you want to disconnect from Astrolabe? You will need to re-authorize to use semantic search.'))) {
				e.preventDefault()
			}
		})
	}

	// Revoke background access form with error handling
	const revokeBackgroundForm = document.getElementById('mcp-revoke-background-form')
	if (revokeBackgroundForm) {
		revokeBackgroundForm.addEventListener('submit', async function(e) {
			e.preventDefault()

			if (!confirm(t('astrolabe', 'Are you sure you want to revoke background sync access? The MCP server will no longer be able to access your Nextcloud data for background operations.'))) {
				return
			}

			const submitButton = revokeBackgroundForm.querySelector('button[type="submit"]')
			const originalText = submitButton.textContent

			try {
				submitButton.disabled = true
				submitButton.textContent = t('astrolabe', 'Revoking...')

				const formData = new FormData(revokeBackgroundForm)
				const response = await fetch(revokeBackgroundForm.action, {
					method: 'POST',
					body: formData,
				})

				const result = await response.json()

				if (response.ok && result.success) {
					showSuccess(t('astrolabe', 'Background sync access revoked successfully.'))
					setTimeout(() => window.location.reload(), 1000)
				} else {
					showError(result.error || t('astrolabe', 'Failed to revoke background sync access.'))
				}
			} catch (error) {
				console.error('Revoke error:', error)
				showError(t('astrolabe', 'Unable to connect to server. Your access may already be revoked, or the server may be down.'))
			} finally {
				submitButton.disabled = false
				submitButton.textContent = originalText
			}
		})
	}
})
