CREATE TABLE users (user_id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE products (product_id BIGSERIAL PRIMARY KEY, sku TEXT UNIQUE NOT NULL, title TEXT NOT NULL, price_cents BIGINT NOT NULL CHECK(price_cents>=0), active BOOLEAN DEFAULT TRUE);
CREATE TABLE inventory (product_id BIGINT PRIMARY KEY REFERENCES products(product_id), on_hand INT NOT NULL DEFAULT 0, reserved INT NOT NULL DEFAULT 0, CHECK(on_hand>=0), CHECK(reserved>=0), CHECK(reserved<=on_hand));
CREATE TABLE orders (order_id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id), status TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, total_cents BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE order_items (order_id BIGINT REFERENCES orders(order_id) ON DELETE CASCADE, product_id BIGINT REFERENCES products(product_id), quantity INT NOT NULL CHECK(quantity>0), unit_price_cents BIGINT NOT NULL, PRIMARY KEY(order_id, product_id));
CREATE TABLE outbox_events (event_id BIGSERIAL PRIMARY KEY, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(), published_at TIMESTAMPTZ);
CREATE INDEX idx_orders_user_created ON orders(user_id, created_at DESC);
CREATE INDEX idx_outbox_unpublished ON outbox_events(created_at) WHERE published_at IS NULL;
