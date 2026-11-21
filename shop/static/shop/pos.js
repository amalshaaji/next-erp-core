(() => {
  const bill = document.getElementById('current-bill');
  const currency = bill.dataset.currency;
  const canEditPrices = bill.dataset.editPrices === 'true';
  const search = document.getElementById('item-search');
  const results = [...document.querySelectorAll('.item-result')];
  const devices = JSON.parse(document.getElementById('available-devices').textContent);
  const cart = [];
  let pendingProduct = null;
  const maxMoney = 999999999999n;

  function moneyCents(value) {
    if (!/^\d{1,10}(\.\d{0,2})?$/.test(String(value))) return null;
    const [whole, fraction = ''] = String(value).split('.');
    const cents = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'));
    return cents <= maxMoney ? cents : null;
  }

  function formatMoney(cents) {
    return `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function productFromButton(button) {
    return {
      id: Number(button.dataset.id), name: button.dataset.name, model: button.dataset.model || '',
      price: button.dataset.price, kind: button.dataset.kind,
      tracking: button.dataset.tracking, stock: Number(button.dataset.stock),
    };
  }

  function productLabel(product) {
    return [product.name, product.model].filter(Boolean).join(' ');
  }

  search.addEventListener('input', () => {
    const query = search.value.trim().toLowerCase();
    let matches = 0;
    results.forEach(button => {
      button.hidden = !!query && !button.dataset.search.toLowerCase().includes(query);
      if (!button.hidden) matches++;
    });
    document.getElementById('item-search-status').textContent = query
      ? (matches ? `${matches} suggestion${matches === 1 ? '' : 's'} · choose one to add it to the bill.`
        : 'No matching products or services. Try another name, model, or SKU.')
      : 'Suggestions appear as you type.';
  });

  results.forEach(button => button.addEventListener('click', () => {
    const product = productFromButton(button);
    if (product.tracking === 'serialized' && product.kind === 'stock') {
      pendingProduct = product;
      renderDevices();
      return;
    }
    const line = cart.find(item => item.id === product.id && !item.deviceId);
    const limit = product.kind === 'service' ? 99 : product.stock;
    if (limit < 1) return;
    if (line) line.quantity = Math.min(line.quantity + 1, limit);
    else cart.push({...product, quantity: 1});
    renderCart();
  }));

  function renderDevices() {
    const choices = devices.filter(device => device.product === pendingProduct.id
      && !cart.some(line => line.deviceId === device.id));
    const box = document.getElementById('imei-options');
    box.replaceChildren();
    choices.forEach(device => {
      const button = element('button', 'imei-option');
      button.type = 'button';
      button.append(element('strong', '', device.imei),
        element('span', '', device.secondHand ? device.condition : 'New phone'));
      button.addEventListener('click', () => {
        cart.push({...pendingProduct, quantity: 1, deviceId: device.id, imei: device.imei});
        document.getElementById('imei-modal').hidden = true;
        renderCart();
      });
      box.append(button);
    });
    if (!choices.length) box.append(element('p', 'notice', 'No available phones for this model.'));
    document.getElementById('imei-title').textContent = 'Choose IMEI · ' + productLabel(pendingProduct);
    document.getElementById('imei-modal').hidden = false;
  }
  document.getElementById('close-imei').addEventListener('click', () => {
    document.getElementById('imei-modal').hidden = true;
  });

  function updateTotals() {
    const [whole, fraction = ''] = document.getElementById('tax-total').dataset.rate.split('.');
    const rate = BigInt(whole) * 1000n + BigInt(fraction.padEnd(3, '0'));
    let subtotal = 0n;
    let tax = 0n;
    let valid = true;
    cart.forEach((line, index) => {
      const cents = moneyCents(line.price);
      const amount = cents === null ? null : cents * BigInt(line.quantity);
      const output = document.getElementById(`cart-amount-${index}`);
      if (output) output.textContent = amount === null ? '—' : `${currency} ${formatMoney(amount)}`;
      if (amount === null) valid = false;
      else {
        subtotal += amount;
        tax += (amount * rate + 50000n) / 100000n;
      }
    });
    const total = subtotal + tax;
    if (total > maxMoney) valid = false;
    document.getElementById('subtotal').textContent = valid ? formatMoney(subtotal) : '—';
    document.getElementById('tax-total').textContent = valid ? formatMoney(tax) : '—';
    document.getElementById('grand-total').textContent = valid ? `${currency} ${formatMoney(total)}` : '—';
    document.getElementById('bill-price-error').hidden = valid;
    document.getElementById('complete-sale').disabled = !cart.length || !valid;
    document.getElementById('cart-json').value = JSON.stringify(cart.map(line => ({
      product_id: line.id, quantity: line.quantity, device_id: line.deviceId || null,
      ...(canEditPrices ? {unit_price: line.price} : {}),
    })));
  }

  function renderCart() {
    const wrap = document.getElementById('cart-lines');
    wrap.replaceChildren();
    if (!cart.length) wrap.append(element('div', 'cart-empty', 'Choose an item to add it to the bill.'));
    cart.forEach((line, index) => {
      const row = element('div', 'cart-line');
      const description = element('div');
      const title = element('div', 'pos-product-title');
      title.append(element('strong', 'pos-product-brand', line.name));
      if (line.model) title.append(element('span', 'pos-product-model', line.model));
      description.append(title, element('small', '',
        line.imei ? `IMEI ${line.imei}` : `${line.quantity} ${line.kind === 'service' ? 'service(s)' : 'pcs'}`));
      const amount = element('span');
      amount.id = `cart-amount-${index}`;
      const remove = element('button', 'remove-line', '×');
      remove.type = 'button';
      remove.setAttribute('aria-label', `Remove ${productLabel(line)}`);
      remove.addEventListener('click', () => { cart.splice(index, 1); renderCart(); });
      row.append(description, amount, remove);
      const controls = element('div', 'cart-line-controls');
      if (canEditPrices) {
        const label = element('label', 'cart-price-field', `Selling price (${currency})`);
        const input = element('input');
        input.type = 'number';
        input.min = '0';
        input.max = '9999999999.99';
        input.step = '0.01';
        input.inputMode = 'decimal';
        input.required = true;
        input.value = line.price;
        input.setAttribute('aria-label', `Selling price for ${productLabel(line)}`);
        input.setAttribute('aria-describedby', 'bill-price-error');
        input.addEventListener('input', () => {
          line.price = input.value;
          const valid = moneyCents(line.price) !== null;
          input.setCustomValidity(valid ? '' : 'Enter a nonnegative price with up to two decimal places.');
          input.setAttribute('aria-invalid', String(!valid));
          updateTotals();
        });
        label.append(input);
        controls.append(label);
      }
      if (!line.deviceId) {
        const limit = line.kind === 'service' ? 99 : line.stock;
        const label = element('label', 'cart-quantity-field', line.kind === 'service' ? 'Quantity' : 'Qty (pcs)');
        const input = element('input');
        input.type = 'number';
        input.min = '1';
        input.max = String(limit);
        input.step = '1';
        input.value = String(line.quantity);
        input.setAttribute('aria-label', `Quantity for ${productLabel(line)}`);
        input.addEventListener('change', () => {
          const quantity = Number(input.value);
          line.quantity = Number.isFinite(quantity) ? Math.min(Math.max(1, Math.floor(quantity)), limit) : 1;
          renderCart();
        });
        label.append(input);
        controls.append(label);
      }
      row.append(controls);
      wrap.append(row);
    });
    document.getElementById('cart-count').textContent = cart.length
      ? `${cart.length} line${cart.length === 1 ? '' : 's'}` : 'No items added';
    updateTotals();
  }

  // Restore the draft after server validation; resolve identities from the current catalog.
  const initialCart = JSON.parse(document.getElementById('initial-cart').textContent);
  initialCart.forEach(raw => {
    if (!raw || typeof raw !== 'object') return;
    const button = results.find(item => Number(item.dataset.id) === Number(raw.product_id));
    if (!button) return;
    const product = productFromButton(button);
    const quantity = Number(raw.quantity);
    if (!Number.isInteger(quantity) || quantity < 1) return;
    const device = devices.find(item => item.id === Number(raw.device_id) && item.product === product.id);
    if (product.kind === 'stock' && product.tracking === 'serialized' && !device) return;
    cart.push({...product, quantity,
      ...(device ? {deviceId: device.id, imei: device.imei} : {}),
      ...(canEditPrices && typeof raw.unit_price === 'string' ? {price: raw.unit_price} : {}),
    });
  });
  renderCart();
})();
