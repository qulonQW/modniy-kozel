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

  // SVG path data for the 24x24 icons.
  const ICONS = {
    home: 'M4 11l8-7 8 7v9h-5v-6H9v6H4z',
    catalog: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
    cart: 'M6 8h12l1 12H5zM9 8V6a3 3 0 0 1 6 0v2',
    orders: 'M6 3h12v18H6zM9 8h6M9 12h6M9 16h4',
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

  function productCard(p) {
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
  function mountFilter(container, onChange) {
    let active = null;
    container.replaceChildren(
      ...CATEGORIES.map((c) =>
        el('button', { class: 'pill', type: 'button', 'aria-pressed': 'false', 'data-category': c }, c)
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

  setupTelegram();
  setupPhotoFallback();
  renderNav();
  renderCartLinks();
  setupBackNavigation();
  refreshCartBadges();
  // The cart count depends on the product list, which arrives asynchronously.
  PRODUCTS_READY.then(refreshCartBadges);
  // Pages restored from the back/forward cache keep their old badge otherwise.
  window.addEventListener('pageshow', refreshCartBadges);

  return {
    el,
    formatPrice,
    stockLabel,
    renderGrid,
    mountFilter,
    refreshCartBadges,
    showLoadError,
    haptic,
  };
})();
