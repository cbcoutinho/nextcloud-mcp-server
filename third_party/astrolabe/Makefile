# Nextcloud App Store Release Makefile for Astrolabe
#
# Based on: https://nextcloudappstore.readthedocs.io/en/latest/developer.html

app_name=astrolabe
project_dir=$(CURDIR)
build_dir=$(project_dir)/build
appstore_dir=$(build_dir)/artifacts
package_name=$(appstore_dir)/$(app_name)
cert_dir=$(HOME)/.nextcloud/certificates

# Signing
private_key=$(cert_dir)/$(app_name).key
certificate=$(cert_dir)/$(app_name).crt
sign_cmd=php ../../server/occ integrity:sign-app --privateKey=$(private_key) --certificate=$(certificate)

# Clean build artifacts
.PHONY: clean
clean:
	rm -rf $(build_dir)

# Install PHP and Node dependencies
.PHONY: install-deps
install-deps:
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

# Create signed release tarball for App Store
.PHONY: appstore
appstore: assemble
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
