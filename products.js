// Storefront config and product data source.
//
// API_BASE_URL: origin of the backend, e.g. 'https://api.example.com'.
// Leave empty when the backend serves the storefront itself (same origin).
const API_BASE_URL = '';

const CATEGORIES = ['Взуття', 'Одяг'];

// Filled from GET /api/products (published products only). The array object
// never changes, so code holding a reference (cart.js) sees the loaded data.
const PRODUCTS = [];

// Resolves to true when products loaded, false when the API is unreachable.
const PRODUCTS_READY = (async () => {
  const base = API_BASE_URL || location.origin;
  const toUrl = (path) => {
    if (typeof path !== 'string' || !path) return '';
    const url = new URL(path, `${base}/`);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : '';
  };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const res = await fetch(`${API_BASE_URL}/api/products`, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!Array.isArray(data)) throw new Error('unexpected response');
    PRODUCTS.push(
      ...data.map((p) => ({
        id: String(p.id),
        name: String(p.name),
        description: String(p.description || ''),
        category: String(p.category),
        price: Number(p.price),
        sizes: Array.isArray(p.sizes) ? p.sizes.map(String) : [],
        inStock: Boolean(p.inStock),
        popular: Boolean(p.popular),
        imageUrl: toUrl(p.imageUrl),
        images: Array.isArray(p.images) ? p.images.map(toUrl).filter(Boolean) : [],
      }))
    );
    return true;
  } catch (err) {
    console.warn('Products could not be loaded:', err.name);
    return false;
  } finally {
    clearTimeout(timer);
  }
})();
