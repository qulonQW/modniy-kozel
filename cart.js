// localStorage-backed cart.
// Cart lines are { id, size, qty, price }; product details are looked up from
// PRODUCTS (loaded from the API), so the current price always applies. "price"
// is the price when the line was added, only used to tell the shopper that it
// changed. The server re-prices every order anyway.

function readList(key) {
  try {
    const parsed = JSON.parse(localStorage.getItem(key));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeList(key, list) {
  try {
    localStorage.setItem(key, JSON.stringify(list));
  } catch {
    // Storage can be unavailable (private mode, quota); the page keeps working.
  }
}

const MKCart = {
  KEY: 'mk_cart',
  MAX_QTY: 10,
  MAX_LINES: 50, // products.js looks up at most 50 cart products

  read() {
    return readList(this.KEY).filter(
      (l) => l && typeof l.id === 'string' && typeof l.size === 'string' && Number.isInteger(l.qty) && l.qty > 0
    );
  },

  // Returns false when the cart is full (MAX_LINES different lines).
  add(id, size, price) {
    const lines = this.read();
    const line = lines.find((l) => l.id === id && l.size === size);
    if (line) {
      line.qty = Math.min(line.qty + 1, this.MAX_QTY);
      line.price = price;
    } else if (lines.length >= this.MAX_LINES) {
      return false;
    } else {
      lines.push({ id, size, qty: 1, price });
    }
    writeList(this.KEY, lines);
    return true;
  },

  remove(id, size) {
    writeList(this.KEY, this.read().filter((l) => !(l.id === id && l.size === size)));
  },

  clear() {
    writeList(this.KEY, []);
  },

  // Every line joined with its product and checked:
  //   status 'ok' | 'out_of_stock' | 'invalid_size' | 'missing' (not published any more)
  //   oldPrice: the price when added, if it differs from the current one.
  review() {
    return this.read().map((line) => {
      const product = PRODUCTS.find((p) => p.id === line.id);
      let status = 'ok';
      if (!product) status = 'missing';
      else if (!product.inStock) status = 'out_of_stock';
      else if (product.sizes.length ? !product.sizes.includes(line.size) : line.size !== '') status = 'invalid_size';
      const oldPrice =
        product && typeof line.price === 'number' && line.price !== product.price ? line.price : null;
      return { ...line, qty: Math.min(line.qty, this.MAX_QTY), product, status, oldPrice };
    });
  },

  // Drops lines whose product is gone; returns how many were dropped.
  // Only call after PRODUCTS loaded successfully.
  pruneMissing() {
    const lines = this.review();
    const kept = lines.filter((l) => l.status !== 'missing');
    if (kept.length !== lines.length) {
      writeList(this.KEY, kept.map(({ id, size, qty, price }) => ({ id, size, qty, price })));
    }
    return lines.length - kept.length;
  },

  // Remembers the current prices once the shopper has seen the change.
  acknowledgePrices() {
    writeList(
      this.KEY,
      this.review()
        .filter((l) => l.product)
        .map(({ id, size, qty, product }) => ({ id, size, qty, price: product.price }))
    );
  },

  // Lines that can be ordered right now.
  getOrderable() {
    return this.review().filter((l) => l.status === 'ok');
  },

  // Lines whose product is still in the catalogue (shown in the cart).
  getItems() {
    return this.review().filter((l) => l.product);
  },

  getCount() {
    return this.getItems().reduce((sum, line) => sum + line.qty, 0);
  },

  getTotal() {
    return this.getOrderable().reduce((sum, line) => sum + line.qty * line.product.price, 0);
  },
};
