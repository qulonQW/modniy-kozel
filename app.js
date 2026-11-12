// Shared UI: Telegram setup, bottom navigation, cart badges, product cards,
// and the category filter. Each page adds its own script after this file.
//
// All DOM is built with createElement + textContent/setAttribute (MK.el);
// no HTML strings are parsed, so product data can never inject markup.

const MK = (() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  // telegram-web-app.js also defines WebApp in a plain browser; initData is
  // only populated when the page is really running inside Telegram.
  const inTelegram = Boolean(tg && tg.initData);

  // Signed Telegram user data for /api/me, /api/favorites and /api/orders.
  // The server takes the user ID only from this header.
  function authHeaders() {
    return tg && tg.initData ? { 'X-Telegram-Init-Data': tg.initData } : {};
  }

  // SVG path data for the 24x24 icons.
  const ICONS = {
    home: 'M4 11l8-7 8 7v9h-5v-6H9v6H4z',
    catalog: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
    cart: 'M6 8h12l1 12H5zM9 8V6a3 3 0 0 1 6 0v2',
    orders: 'M6 3h12v18H6zM9 8h6M9 12h6M9 16h4',
    heart: 'M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 7.5 2.6C19.5 15.4 12 20 12 20z',
  };

  const NAV_ITEMS = [
    { key: 'home', label: 'Головна', href: './index.html' },
    { key: 'catalog', label: 'Каталог', href: './catalog.html' },
    { key: 'cart', label: 'Кошик', href: './cart.html' },
    { key: 'orders', label: 'Замовлення', href: './orders.html' },
  ];

  /**
   * el('a', { class: 'card', href }, child, 'text', ...)
   * Attributes are set in the given order; true sets an empty attribute,
   * false/null/undefined skips it. String children become text nodes.
   */
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === false || value === null || value === undefined) continue;
      node.setAttribute(name, value === true ? '' : String(value));
    }
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child);
    }
    return node;
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', ICONS[name]);
    svg.append(path);
    return svg;
  }

  const badge = () => el('span', { class: 'badge', 'data-cart-count': true, hidden: true });

  function formatPrice(value) {
    return `${String(value).replace(/\B(?=(\d{3})+$)/g, ' ')} грн`;
  }

  function stockLabel(inStock) {
    return el(
      'span',
      { class: 'stock' },
      el('i', { class: `dot ${inStock ? 'dot-in' : 'dot-out'}` }),
      inStock ? 'Є' : 'Немає'
    );
  }

  // Heart toggle for one product; hidden until favorites are available
  // (i.e. inside Telegram). Clicks are handled by setupFavorites.
  function favButton(id, className = 'fav-btn') {
    return el(
      'button',
      {
        class: className,
        type: 'button',
        'data-fav-id': id,
        'aria-pressed': String(favorites.ids.has(id)),
        'aria-label': 'Обране',
        hidden: !favorites.enabled,
      },
      icon('heart')
    );
  }

  function productCard(p) {
    return el('div', { class: 'card-wrap' }, productLink(p), favButton(p.id));
  }

  function productLink(p) {
    return el(
      'a',
      { class: 'card', href: `./product.html?id=${encodeURIComponent(p.id)}` },
      el(
        'div',
        { class: 'card-media' },
        el('img', { src: p.imageUrl, alt: p.name, loading: 'lazy' }),
        el('span', { class: 'tag' }, p.category)
      ),
      el(
        'div',
        { class: 'card-body' },
        el('h3', { class: 'card-name' }, p.name),
        el(
          'div',
          { class: 'card-meta' },
          el('span', { class: 'price' }, formatPrice(p.price)),
          stockLabel(p.inStock)
        )
      )
    );
  }

  function renderGrid(container, products) {
    container.className = products.length ? 'grid' : '';
    container.replaceChildren(
      ...(products.length
        ? products.map(productCard)
        : [el('p', { class: 'empty-note' }, 'У цій категорії поки нічого немає.')])
    );
  }

  // Tapping the active pill clears it, which is how a shopper gets back to
  // the unfiltered list. Calls onChange with the category, or null when cleared.
  function mountFilter(container, onChange, initial = null) {
    let active = CATEGORIES.includes(initial) ? initial : null;
    container.replaceChildren(
      ...CATEGORIES.map((c) =>
        el('button', { class: 'pill', type: 'button', 'aria-pressed': String(c === active), 'data-category': c }, c)
      )
    );

    container.addEventListener('click', (event) => {
      const pill = event.target.closest('.pill');
      if (!pill) return;
      active = pill.dataset.category === active ? null : pill.dataset.category;
      container.querySelectorAll('.pill').forEach((p) =>
        p.setAttribute('aria-pressed', String(p.dataset.category === active))
      );
      onChange(active);
    });
  }

  function renderNav() {
    const mount = document.querySelector('[data-nav]');
    if (!mount) return;
    const current = mount.dataset.nav;
    mount.className = 'bottom-nav';
    mount.replaceChildren(
      ...NAV_ITEMS.map((item) =>
        el(
          'a',
          { class: 'nav-item', href: item.href, 'aria-current': item.key === current && 'page' },
          el('span', { class: 'nav-icon' }, icon(item.key), item.key === 'cart' && badge()),
          el('span', null, item.label)
        )
      )
    );
  }

  function renderCartLinks() {
    document.querySelectorAll('[data-cart-link]').forEach((link) => {
      link.replaceChildren(icon('cart'), badge());
    });
  }

  // Page headers: [Адмінка] [heart -> Обране] [bag]. The heart link appears
  // once favorites are known to be available.
  function renderHeaderActions() {
    document.querySelectorAll('.header [data-cart-link]').forEach((cart) => {
      const actions = el('div', { class: 'header-actions' });
      cart.replaceWith(actions);
      cart.classList.remove('icon-btn-end');
      const fav = el(
        'a',
        { class: 'icon-btn', href: './favorites.html', 'data-fav-link': true, 'aria-label': 'Обране', hidden: true },
        icon('heart')
      );
      actions.append(fav, cart);
    });
  }

  function refreshCartBadges() {
    const count = MKCart.getCount();
    document.querySelectorAll('[data-cart-count]').forEach((el) => {
      el.textContent = count;
      el.hidden = count === 0;
    });
  }

  function goBack(fallback) {
    if (history.length > 1) history.back();
    else location.href = fallback;
  }

  function setupBackNavigation() {
    const fallback = document.body.dataset.back;
    if (!fallback) return;

    document.querySelectorAll('[data-back-link]').forEach((link) => {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        goBack(fallback);
      });
    });

    if (inTelegram) {
      tg.BackButton.onClick(() => goBack(fallback));
      tg.BackButton.show();
    }
  }

  function setupTelegram() {
    if (!inTelegram) return;
    document.documentElement.classList.add('in-telegram');
    tg.ready();
    tg.expand();
    tg.setHeaderColor('#0A0A0A');
    tg.setBackgroundColor('#0A0A0A');
  }

  function haptic(type) {
    if (inTelegram) tg.HapticFeedback.notificationOccurred(type);
  }

  // Image load errors do not bubble, so this listens in the capture phase.
  function setupPhotoFallback() {
    document.addEventListener(
      'error',
      (event) => {
        const img = event.target;
        if (!(img instanceof HTMLImageElement)) return;
        if (!img.closest('.card-media, .cart-thumb, .product-image')) return;
        const fallback = document.createElement('span');
        fallback.className = 'photo-fallback';
        fallback.textContent = img.alt;
        img.replaceWith(fallback);
      },
      true
    );
  }

  // Shown in place of a page's content when GET /api/products fails.
  function showLoadError(container) {
    const retry = el('button', { class: 'btn btn-inline', type: 'button' }, 'Спробувати ще раз');
    retry.addEventListener('click', () => location.reload());
    container.classList.add('empty');
    container.replaceChildren(
      el('h2', { class: 'empty-title' }, 'Не вдалося завантажити товари'),
      el('p', { class: 'summary-note' }, 'Перевірте з’єднання з інтернетом і спробуйте ще раз.'),
      retry
    );
    container.hidden = false;
  }

  // Short message for the shopper: Telegram's native alert, or the browser's.
  function notify(message) {
    if (inTelegram && tg.showAlert) tg.showAlert(message);
    else window.alert(message);
  }

  // --- favorites (server-side, per Telegram user) ---------------------------
  // state: 'loading' | 'ok' | 'unavailable' (no Telegram user) | 'error'
  const favorites = { enabled: false, state: 'loading', ids: new Set(), products: [] };

  function refreshFavorites() {
    document.querySelectorAll('[data-fav-id]').forEach((b) => {
      b.hidden = !favorites.enabled;
      b.setAttribute('aria-pressed', String(favorites.ids.has(b.dataset.favId)));
    });
    document.querySelectorAll('[data-fav-link]').forEach((a) => {
      a.hidden = !favorites.enabled;
    });
  }

  async function loadFavorites() {
    try {
      const res = await fetch(`${API_BASE_URL}/api/favorites`, { headers: authHeaders(), cache: 'no-store' });
      if (res.status === 401) {
        favorites.state = 'unavailable';
        return false;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (!Array.isArray(data)) throw new Error('unexpected response');
      favorites.products = data.map(normalizeProduct);
      favorites.ids = new Set(favorites.products.map((p) => p.id));
      favorites.enabled = true;
      favorites.state = 'ok';
      return true;
    } catch (err) {
      console.warn('Favorites could not be loaded:', err.name);
      favorites.state = 'error';
      return false;
    } finally {
      refreshFavorites();
    }
  }

  function setFavorite(id, on) {
    if (on) favorites.ids.add(id);
    else favorites.ids.delete(id);
    refreshFavorites();
    document.dispatchEvent(new CustomEvent('mk:favorite', { detail: { id, on } }));
  }

  // Optimistic toggle; reverted with a message if the server says no.
  function setupFavorites() {
    document.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-fav-id]');
      if (!button) return;
      event.preventDefault();
      if (button.disabled || !favorites.enabled) return;
      const id = button.dataset.favId;
      const on = !favorites.ids.has(id);
      setFavorite(id, on);
      button.disabled = true;
      try {
        const res = await fetch(`${API_BASE_URL}/api/favorites/${encodeURIComponent(id)}`, {
          method: on ? 'PUT' : 'DELETE',
          headers: authHeaders(),
        });
        if (!res.ok) {
          setFavorite(id, !on);
          haptic('error');
          notify(
            res.status === 409
              ? 'В обраному вже 200 товарів. Видаліть щось, щоб додати нове.'
              : res.status === 404
                ? 'Цей товар більше недоступний.'
                : 'Не вдалося оновити обране. Спробуйте ще раз.'
          );
        }
      } catch {
        setFavorite(id, !on);
        haptic('error');
        notify('Не вдалося оновити обране. Перевірте з’єднання з інтернетом.');
      } finally {
        button.disabled = false;
      }
    });
  }

  // "Адмінка" link in the page header, left of the cart icon, only for users
  // the backend recognises as admins (same initData check as /api/admin).
  // Any failure just leaves the link out; shoppers never see it.
  async function mountAdminLink() {
    const header = document.querySelector('.header');
    if (!header) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/me`, { headers: authHeaders(), cache: 'no-store' });
      if (!res.ok || !(await res.json()).is_admin) return;
    } catch {
      return;
    }
    const link = el('a', { class: 'admin-link', href: './admin/' }, 'Адмінка');
    const actions = header.querySelector('.header-actions');
    if (actions) actions.prepend(link);
    else header.append(link);
  }

  setupTelegram();
  setupPhotoFallback();
  renderNav();
  renderCartLinks();
  renderHeaderActions();
  mountAdminLink();
  setupFavorites();
  const FAVORITES_READY = loadFavorites();
  setupBackNavigation();
  refreshCartBadges();
  // The cart count depends on the product list, which arrives asynchronously.
  PRODUCTS_READY.then(refreshCartBadges);
  // Pages restored from the back/forward cache keep their old badge otherwise.
  window.addEventListener('pageshow', refreshCartBadges);

  return {
    el,
    icon,
    inTelegram,
    authHeaders,
    notify,
    favorites,
    FAVORITES_READY,
    favButton,
    formatPrice,
    stockLabel,
    productCard,
    renderGrid,
    mountFilter,
    refreshCartBadges,
    showLoadError,
    haptic,
  };
})();
