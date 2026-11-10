// localStorage-backed cart.
// Cart lines are { id, size, qty }; product details are looked up from PRODUCTS
// (loaded from the API), so a price change in the admin applies to items
// already in the cart.

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

  read() {
    return readList(this.KEY);
  },

  add(id, size) {
    const lines = this.read();
    const line = lines.find((l) => l.id === id && l.size === size);
    if (line) line.qty += 1;
    else lines.push({ id, size, qty: 1 });
    writeList(this.KEY, lines);
  },

  remove(id, size) {
    writeList(this.KEY, this.read().filter((l) => !(l.id === id && l.size === size)));
  },

  clear() {
    writeList(this.KEY, []);
  },

  // Lines joined with their product. Lines for products no longer in the
  // catalogue are skipped so totals and counts stay consistent.
  getItems() {
    return this.read()
      .map((line) => ({ ...line, product: PRODUCTS.find((p) => p.id === line.id) }))
      .filter((line) => line.product);
  },

  getCount() {
    return this.getItems().reduce((sum, line) => sum + line.qty, 0);
  },

  getTotal() {
    return this.getItems().reduce((sum, line) => sum + line.qty * line.product.price, 0);
  },
};
