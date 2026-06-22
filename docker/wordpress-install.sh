#!/bin/bash
set -e

echo "=== WordPress Installation Script ==="

# Read the actual DB password from .env
DB_PASS=$(grep "^WP_DB_PASSWORD=" /opt/wordpress/.env | head -1 | sed 's/^WP_DB_PASSWORD=//')

echo "Step 1: Writing wp-config.php with real DB credentials..."

# Get fresh salts from WordPress API
SALTS=$(curl -s https://api.wordpress.org/secret-key/1.1/salt/ 2>/dev/null)

# Build wp-config.php with actual values
docker exec wordpress-wp-1 /bin/sh -c "cat > /var/www/html/wp-config.php" << PHPEOF
<?php
define( 'DB_NAME', 'wordpress' );
define( 'DB_USER', 'wordpress' );
define( 'DB_PASSWORD', '${DB_PASS}' );
define( 'DB_HOST', 'wp-db:3306' );
define( 'DB_CHARSET', 'utf8mb4' );
define( 'DB_COLLATE', '' );
PHPEOF

# Append salts
docker exec wordpress-wp-1 /bin/sh -c "cat >> /var/www/html/wp-config.php" << 'PHPEOF'

$table_prefix = 'wp_';
define( 'WP_DEBUG', false );
define( 'WP_MEMORY_LIMIT', '128M' );
define( 'DISALLOW_FILE_EDIT', true );
define( 'FORCE_SSL_ADMIN', true );

if ( isset( \$_SERVER['HTTP_X_FORWARDED_PROTO'] ) && \$_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https' ) {
    \$_SERVER['HTTPS'] = 'on';
}

if ( ! defined( 'ABSPATH' ) ) {
    define( 'ABSPATH', __DIR__ . '/' );
}
require_once ABSPATH . 'wp-settings.php';
PHPEOF

echo "Step 2: Installing WordPress..."
ADMIN_PASS="JumboBlog2026"

docker run --rm --network wordpress_default \
  -v wordpress_wp-data:/var/www/html \
  -e DB_PASSWORD=*** \
  wordpress:cli-php8.3 \
  sh -c "wp core install \
    --url='https://blog.jumbohomes.in' \
    --title='Jumbo Homes Blog' \
    --admin_user='admin' \
    --admin_password='${ADMIN_PASSWORD}' \
    --admin_email='admin@jumbohomes.in' \
    --path=/var/www/html \
    --allow-root 2>&1"

echo ""
echo "Step 3: SEO configuration..."
docker run --rm --network wordpress_default \
  -v wordpress_wp-data:/var/www/html \
  -e DB_PASSWORD=*** \
  wordpress:cli-php8.3 \
  sh -c "wp rewrite structure '/%postname%/' --allow-root --path=/var/www/html 2>&1"

echo ""
echo "Step 4: Set site description..."
docker run --rm --network wordpress_default \
  -v wordpress_wp-data:/var/www/html \
  -e DB_PASSWORD=*** \
  wordpress:cli-php8.3 \
  sh -c "wp option update blogdescription 'Bangalore real estate insights, property guides, and market updates from Jumbo Homes' --allow-root --path=/var/www/html 2>&1"

echo ""
echo "============================================"
echo "WordPress Installation Complete!"
echo "============================================"
echo "Site: https://blog.jumbohomes.in"
echo "Admin: https://blog.jumbohomes.in/wp-admin"
echo "Username: admin"
echo "============================================"
