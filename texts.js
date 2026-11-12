// Shopper-facing texts for the cart, checkout and orders, in ONE place.
// Edit the wording freely; keep the keys and the ${...} placeholders.
// Shown with textContent only, so plain text (no HTML) is expected here.
// No promises about delivery times, payment or returns on purpose.

const TEXTS = {
  checkout: {
    summaryTitle: 'Ваше замовлення',
    total: 'Разом',
    nameLabel: "Ім'я та прізвище",
    phoneLabel: 'Телефон',
    cityLabel: 'Місто',
    deliveryLabel: 'Відділення або поштомат Нової Пошти',
    deliveryPlaceholder: 'Напр.: Відділення №12',
    commentLabel: 'Коментар (необов’язково)',
    commentPlaceholder: 'Побажання до замовлення',
    submit: 'Підтвердити замовлення',
    sending: 'Надсилаємо…',
    skippedNote: 'Товари, яких немає в наявності, не потраплять у замовлення.',
  },

  // Success screen after the order is accepted.
  confirmation: {
    title: 'Заявку отримано',
    message: "Дякуємо! Заявку отримано, менеджер зв'яжеться з вами для підтвердження.",
    orderNumber: (id) => `Номер замовлення: № ${id}`,
    toOrders: 'Мої замовлення',
    continue: 'Продовжити покупки',
  },

  errors: {
    name: 'Вкажіть ім’я та прізвище (від 2 до 100 символів).',
    phone: 'Вкажіть номер телефону: 9 цифр після +380, напр. 50 123 45 67.',
    city: 'Вкажіть місто.',
    delivery: 'Вкажіть відділення або поштомат.',
    comment: 'Коментар — до 500 символів.',
    form: 'Перевірте поля, позначені червоним.',
    cartChanged: 'Деякі товари з кошика більше недоступні або змінилися. Перевірте кошик.',
    priceChanged: (total) => `Ціни змінилися. Нова сума: ${total}. Перевірте й натисніть «Підтвердити» ще раз.`,
    notInTelegram: 'Оформити замовлення можна лише в Telegram: відкрийте магазин через бота.',
    sessionExpired: 'Сесія Telegram застаріла. Закрийте магазин і відкрийте його знову через бота.',
    tooMany: 'Забагато спроб. Зачекайте хвилину й спробуйте ще раз.',
    network: 'Не вдалося надіслати. Перевірте інтернет і спробуйте ще раз — замовлення не задублюється.',
    server: 'Щось пішло не так. Спробуйте ще раз.',
  },

  cart: {
    outOfStock: 'Немає в наявності',
    sizeUnavailable: 'Цього розміру більше немає',
    priceChanged: (oldPrice) => `Ціна змінилася (було ${oldPrice})`,
    pricesUpdated: 'Ціни деяких товарів змінилися — суму оновлено.',
    removed: (count) => `${count} ${count === 1 ? 'товар більше недоступний і його' : 'товари більше недоступні, їх'} прибрано з кошика.`,
    skipped: 'Товари без наявності не потраплять у замовлення.',
    nothingToOrder: 'У кошику немає товарів, які можна замовити.',
  },

  orderStatus: {
    new: 'Очікує підтвердження',
    confirmed: 'Підтверджено',
    cancelled: 'Скасовано',
  },

  orders: {
    empty: 'Замовлень поки немає',
    notInTelegram: 'Ваші замовлення видно лише в Telegram',
    notInTelegramNote: 'Відкрийте магазин через бота.',
    loadError: 'Не вдалося завантажити замовлення',
    number: (id) => `№ ${id}`,
    size: (size) => `розмір ${size}`,
    delivery: (city, point) => `${city}, ${point}`,
  },
};
