// Shared UI: Telegram setup, bottom navigation, cart badges, product cards,
// and the category filter. Each page adds its own script after this file.

const MK = (() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  // telegram-web-app.js also defines WebApp in a plain browser; initData is
  // only populated when the page is really running inside Telegram.
  const inTelegram = Boolean(tg && tg.initData);

  const ICONS = {
    home: '<path d="M4 11l8-7 8 7v9h-5v-6H9v6H4z"/>',
    catalog: '<path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/>',
    cart: '<path d="M6 8h12l1 12H5zM9 8V6a3 3 0 0 1 6 0v2"/>',
    orders: '<path d="M6 3h12v18H6zM9 8h6M9 12h6M9 16h4"/>',
  };

  const NAV_ITEMS = [
    { key: 'home', label: 'Головна', href: './index.html' },
    { key: 'catalog', label: 'Каталог', href: './catalog.html' },
    { key: 'cart', label: 'Кошик', href: './cart.html' },
    { key: 'orders', label: 'Замовлення', href: './orders.html' },
  ];

  const icon = (name) =>
    `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

  const badge = () => '<span class="badge" data-cart-count hidden></span>';

  function esc(value) {
    const el = document.createElement('div');
    el.textContent = value;
    return el.innerHTML;
  }

  function formatPrice(value) {
    return `${String(value).replace(/\B(?=(\d{3})+$)/g, ' ')} грн`;
  }

  function stockLabel(inStock) {
    return `<span class="stock"><i class="dot ${inStock ? 'dot-in' : 'dot-out'}"></i>${
      inStock ? 'Є' : 'Немає'
    }</span>`;
  }

  function productCard(p) {
    return `
      <a class="card" href="./product.html?id=${p.id}">
        <div class="card-media">
          <img src="${esc(p.imageUrl)}" alt="${esc(p.name)}" loading="lazy">
          <span class="tag">${esc(p.category)}</span>
        </div>
        <div class="card-body">
          <h3 class="card-name">${esc(p.name)}</h3>
          <div class="card-meta">
            <span class="price">${formatPrice(p.price)}</span>
            ${stockLabel(p.inStock)}
          </div>
        </div>
      </a>`;
  }

  function renderGrid(el, products) {
    el.className = products.length ? 'grid' : '';
    el.innerHTML = products.length
      ? products.map(productCard).join('')
      : '<p class="empty-note">У цій категорії поки нічого немає.</p>';
  }

  // Tapping the active pill clears it, which is how a shopper gets back to
  // the unfiltered list. Calls onChange with the category, or null when cleared.
  function mountFilter(el, onChange) {
    let active = null;
    el.innerHTML = CATEGORIES.map(
      (c) => `<button class="pill" type="button" aria-pressed="false" data-category="${esc(c)}">${esc(c)}</button>`
    ).join('');

    el.addEventListener('click', (event) => {
      const pill = event.target.closest('.pill');
      if (!pill) return;
      active = pill.dataset.category === active ? null : pill.dataset.category;
      el.querySelectorAll('.pill').forEach((p) =>
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
    mount.innerHTML = NAV_ITEMS.map(
      (item) => `
        <a class="nav-item" href="${item.href}"${item.key === current ? ' aria-current="page"' : ''}>
          <span class="nav-icon">${icon(item.key)}${item.key === 'cart' ? badge() : ''}</span>
          <span>${item.label}</span>
        </a>`
    ).join('');
  }

  function renderCartLinks() {
    document.querySelectorAll('[data-cart-link]').forEach((link) => {
      link.innerHTML = icon('cart') + badge();
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
  function showLoadError(el) {
    const title = document.createElement('h2');
    title.className = 'empty-title';
    title.textContent = 'Не вдалося завантажити товари';
    const note = document.createElement('p');
    note.className = 'summary-note';
    note.textContent = 'Перевірте з’єднання з інтернетом і спробуйте ще раз.';
    const retry = document.createElement('button');
    retry.className = 'btn btn-inline';
    retry.type = 'button';
    retry.textContent = 'Спробувати ще раз';
    retry.addEventListener('click', () => location.reload());
    el.classList.add('empty');
    el.replaceChildren(title, note, retry);
    el.hidden = false;
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
    esc,
    formatPrice,
    stockLabel,
    renderGrid,
    mountFilter,
    refreshCartBadges,
    showLoadError,
    haptic,
  };
})();
