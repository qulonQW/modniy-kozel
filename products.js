// Storefront config and product data source.
//
// API_BASE_URL: origin of the backend, e.g. 'https://api.example.com'.
// Leave empty when the backend serves the storefront itself (same origin).
const API_BASE_URL = '';

const CATEGORIES = ['Взуття', 'Одяг'];

// Products the page needs to know about: the ones in the cart (loaded from
// GET /api/products?ids=..., published only) plus whatever a page adds (the
// product page adds its product). The array object never changes, so code
// holding a reference (cart.js) sees the loaded data. Lists (home, catalog,
// favorites) fetch their own pages and do not use this array.
const PRODUCTS = [];

// Image paths from the API are resolved against the backend; anything that is
// not http(s) is dropped.
function apiAssetUrl(path) {
  if (typeof path !== 'string' || !path) return '';
  const url = new URL(path, `${API_BASE_URL || location.origin}/`);
  return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : '';
}

// One product from the public API -> the shape the storefront pages use.
function normalizeProduct(p) {
  return {
    id: String(p.id),
    name: String(p.name),
    description: String(p.description || ''),
    category: String(p.category),
    price: Number(p.price),
    sizes: Array.isArray(p.sizes) ? p.sizes.map(String) : [],
    inStock: Boolean(p.inStock),
    popular: Boolean(p.popular),
    imageUrl: apiAssetUrl(p.imageUrl),
    thumbUrl: apiAssetUrl(p.thumbUrl || p.imageUrl),
    images: Array.isArray(p.images) ? p.images.map(apiAssetUrl).filter(Boolean) : [],
  };
}

// Product ids in the cart, read straight from storage (cart.js loads later).
function cartProductIds() {
  try {
    const lines = JSON.parse(localStorage.getItem('mk_cart'));
    if (!Array.isArray(lines)) return [];
    return [...new Set(lines.map((l) => l && l.id).filter((id) => typeof id === 'string'))].slice(0, 50);
  } catch {
    return [];
  }
}

function addProducts(list) {
  for (const p of list) {
    const index = PRODUCTS.findIndex((x) => x.id === p.id);
    if (index >= 0) PRODUCTS[index] = p;
    else PRODUCTS.push(p);
  }
}

// Resolves to true when the cart's products loaded (or the cart is empty),
// false when the API is unreachable.
const PRODUCTS_READY = (async () => {
  const ids = cartProductIds();
  if (!ids.length) return true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const query = new URLSearchParams({ ids: ids.join(',') });
    const res = await fetch(`${API_BASE_URL}/api/products?${query}`, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!Array.isArray(data)) throw new Error('unexpected response');
    addProducts(data.map(normalizeProduct));
    return true;
  } catch (err) {
    console.warn('Products could not be loaded:', err.name);
    return false;
  } finally {
    clearTimeout(timer);
  }
})();
