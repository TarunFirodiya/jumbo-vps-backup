#!/bin/bash
set -e

echo "Step 1: Generating wp-config.php..."

DB_PASS=$(grep WP_DB_PASSWORD /opt/wordpress/.env | head -1 | cut -d= -f2)

# Generate WordPress salts
SALTS=$(docker run --rm wordpress:cli-php8.3 wp salt --allow-root --path=/var/www/html 2>/dev/null || echo "")

# Create wp-config.php by copying the sample and injecting values
docker run --rm --network wordpress_default \
  -v wordpress_wp-data:/var/www/html \
  wordpress:cli-php8.3 \
  /bin/sh -c "
    cd /var/www/html
    cp wp-config-sample.php wp-config.php
    
    # Set DB settings
    sed -i \"s/database_name_here/wordpress/\" wp-config.php
    sed -i \"s/username_here/wordpress/\" wp-config.php
    sed -i \"s/password_here/${DB_PASS}/\" wp-config.php
    sed -i \"s/localhost/wp-db:3306/\" wp-config.php
    
    # Generate and set unique keys and salts
    wp config shuffle-salts --allow-root --path=/var/www/html 2>/dev/null || true
  "

echo "Step 2: Installing WordPress core..."
ADMIN_PASS=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 20)

docker run --rm --network wordpress_default \
  -v wordpress_wp-data:/var/www/html \
  wordpress:cli-php8.3 \
  wp core install \
    --url="https://blog.jumbohomes.in" \
    --title="Jumbo Homes Blog" \
    --admin_user="admin" \
    --admin_email="admin@jumbohomes.in" \
    --path=/var/www/html \
    --allow-root

echo ""
echo "============================================"
echo "WordPress installed!"
echo "URL: https://blog.jumbohomes.in"
echo "Admin: https://blog.jumbohomes.in/wp-admin"
echo "============================================"

echo ""
echo "Step 3: Configuring for SEO..."
docker run --rm --network wordpress_default \
  -v wordpress_wp-data:/var/www/html \
  wordpress:cli-php8.3 \
  wp rewrite structure "/%postname%/" --allow-root --path=/var/www/html

docker run --rm --network wordpress_default \
  -v wordpress_wp-data:/var/www/html \
  wordpress:cli-php8.3 \
  wp option update blogdescription "Bangalore real estate insights, property guides, and market updates from Jumbo Homes" --allow-root --path=/var/www/html

echo ""
echo "SEO permalink structure: /%postname%/"
echo "Blog description set"
echo ""
echo "Step 4: Disabling comments (not needed for blog)..."
docker run --rm --network wordpress_default \
  -v wordpress_wp-data:/var/www/html \
  wordpress:cli-php8.3 \
  wp option update default_comment_status "closed" --allow-root --path=/var/www/html

echo ""
echo "============================================"
echo "WordPress setup complete!"
echo "============================================"
