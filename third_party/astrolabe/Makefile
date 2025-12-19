# Nextcloud App Store Release Makefile for Astrolabe
#
# Based on: https://nextcloudappstore.readthedocs.io/en/latest/developer.html

app_name=astrolabe
project_dir=$(CURDIR)
build_dir=$(project_dir)/build
appstore_dir=$(build_dir)/artifacts
package_name=$(appstore_dir)/$(app_name)
cert_dir=$(HOME)/.nextcloud/certificates

# Nextcloud server path (configurable via environment variable)
server_dir?=../../server
occ=$(server_dir)/occ

# Signing
private_key=$(cert_dir)/$(app_name).key
certificate=$(cert_dir)/$(app_name).crt
sign_cmd=php $(occ) integrity:sign-app --privateKey=$(private_key) --certificate=$(certificate)

# Clean build artifacts
.PHONY: clean
clean:
	rm -rf $(build_dir)

# Validate required dependencies
.PHONY: validate-deps
validate-deps:
	@command -v composer >/dev/null 2>&1 || { echo "Error: composer not found. Install from https://getcomposer.org/"; exit 1; }
	@command -v npm >/dev/null 2>&1 || { echo "Error: npm not found. Install Node.js from https://nodejs.org/"; exit 1; }
	@command -v php >/dev/null 2>&1 || { echo "Error: php not found. Install PHP 8.1 or higher."; exit 1; }
	@echo "✓ All dependencies found"

# Install PHP and Node dependencies
.PHONY: install-deps
install-deps: validate-deps
	composer install --no-dev --optimize-autoloader
	npm ci

# Build production frontend assets
.PHONY: build-frontend
build-frontend:
	npm run build

# Run all linters
.PHONY: lint
lint:
	composer lint
	composer cs:check
	npm run lint
	npm run stylelint

# Assemble app files into build directory (exclude dev files)
.PHONY: assemble
assemble: clean install-deps build-frontend
	mkdir -p $(package_name)
	# Copy app files
	rsync -av \
		--exclude='.git*' \
		--exclude='build/' \
		--exclude='tests/' \
		--exclude='node_modules/' \
		--exclude='*.log' \
		--exclude='.github/' \
		--exclude='composer.json' \
		--exclude='composer.lock' \
		--exclude='package.json' \
		--exclude='package-lock.json' \
		--exclude='vite.config.js' \
		--exclude='.eslintrc.js' \
		--exclude='.php-cs-fixer.*' \
		--exclude='psalm.xml' \
		--exclude='*.iml' \
		--exclude='.idea' \
		--exclude='src/' \
		./ $(package_name)/

# Validate signing prerequisites
.PHONY: validate-signing
validate-signing:
	@test -f $(occ) || { echo "Error: Nextcloud server not found at $(server_dir)"; echo "Set server_dir variable: make appstore server_dir=/path/to/server"; exit 1; }
	@test -f $(private_key) || { echo "Error: Private key not found at $(private_key)"; exit 1; }
	@test -f $(certificate) || { echo "Error: Certificate not found at $(certificate)"; exit 1; }
	@echo "✓ Signing prerequisites validated"

# Create signed release tarball for App Store
.PHONY: appstore
appstore: assemble validate-signing
	# Sign the app
	$(sign_cmd) --path=$(package_name)
	# Create tarball
	cd $(appstore_dir) && \
		tar -czf $(app_name).tar.gz $(app_name)
	# Show package info
	@echo "========================================="
	@echo "App package created:"
	@echo "  $(appstore_dir)/$(app_name).tar.gz"
	@echo ""
	@echo "Signature:"
	@cat $(package_name)/appinfo/signature.json | head -n 5
	@echo "========================================="
