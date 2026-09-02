/** Client-side plate-cutting calculation and preset-library UI. */
(function () {
  var root = document.querySelector('[data-plate-cutting]');
  if (!root) return;

  var HOLE_SECONDS = Number(root.dataset.holeSeconds);
  var packagesEl = root.querySelector('[data-packages]');
  var template = root.querySelector('[data-package-template]');
  var addButton = root.querySelector('[data-add-package]');
  var singleSetHoursEl = root.querySelector('[data-single-set-hours]');
  var setQuantityEl = root.querySelector('[data-set-quantity]');
  var totalHoursEl = root.querySelector('[data-total-hours]');
  var totalSecondsEl = root.querySelector('[data-total-seconds]');
  var totalSkippedEl = root.querySelector('[data-total-skipped]');
  var feedbackEl = root.querySelector('[data-feedback]');
  if (!packagesEl || !template || !addButton || !setQuantityEl) return;

  var INTEGER = /^\d+$/;
  var CHECK_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4L19 6"/></svg>';
  var EDIT_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/></svg>';

  function formatNumber(value, digits) {
    return Number(value).toLocaleString('ru-RU', {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
  }

  function formatSeconds(value) {
    return Number(Number(value).toFixed(4)).toLocaleString('ru-RU', {
      maximumFractionDigits: 4,
    });
  }

  function formatHours(value) {
    return formatNumber(value, 2);
  }

  /** The one implementation of the package formula, always for one set. */
  function calculatePackage(range, plates, holes) {
    var plateSeconds = range.seconds * plates;
    var holeSeconds = HOLE_SECONDS * holes;
    var seconds = plateSeconds + holeSeconds;
    return {
      range: range,
      plates: plates,
      holes: holes,
      plateSeconds: plateSeconds,
      holeSeconds: holeSeconds,
      seconds: seconds,
      hours: seconds / 3600,
    };
  }

  function readRange(select) {
    var option = select.options[select.selectedIndex];
    if (!option) return null;
    var seconds = Number(option.dataset.seconds);
    if (!isFinite(seconds) || seconds <= 0) return null;
    return { label: option.textContent.trim(), seconds: seconds };
  }

  function readPackage(element) {
    var range = readRange(element.querySelector('[data-field="range"]'));
    var platesEl = element.querySelector('[data-field="plates"]');
    var holesEl = element.querySelector('[data-field="holes"]');
    var platesText = platesEl.value.trim();
    var holesText = holesEl.value.trim();
    var platesMax = Number(platesEl.getAttribute('max'));
    var holesMax = Number(holesEl.getAttribute('max'));

    if (!range) return { error: 'Выберите диапазон длины пластины.' };
    if (!platesText) return { incomplete: true };
    if (!INTEGER.test(platesText) || Number(platesText) <= 0) {
      return { error: 'Количество пластин — целое число больше 0.' };
    }
    if (platesMax && Number(platesText) > platesMax) {
      return { error: 'Количество пластин — не больше ' + platesMax + '.' };
    }
    // Empty reads as nought holes, and only here. The field starts blank so a
    // new package does not show a zero nobody typed, but a row is still
    // computable and confirmable before it is filled in — exactly what the
    // prefilled `0` used to give. Nothing writes the zero back to the input.
    if (holesText && !INTEGER.test(holesText)) {
      return { error: 'Количество отверстий, всего — целое число от 0.' };
    }
    if (holesMax && Number(holesText) > holesMax) {
      return { error: 'Количество отверстий, всего — не больше ' + holesMax + '.' };
    }
    return {
      result: calculatePackage(range, Number(platesText), holesText ? Number(holesText) : 0),
    };
  }

  function readSetQuantity() {
    var text = setQuantityEl.value.trim();
    var maximum = Number(setQuantityEl.getAttribute('max'));
    if (!INTEGER.test(text) || Number(text) < 1) {
      return { error: 'Количество наборов — целое число больше 0.' };
    }
    if (maximum && Number(text) > maximum) {
      return { error: 'Количество наборов — не больше ' + maximum + '.' };
    }
    return { value: Number(text) };
  }

  function detailRow(term, value) {
    return '<div><dt>' + term + '</dt><dd>' + value + '</dd></div>';
  }

  /** The popup shows the base value and, when active, the set multiplier. */
  function renderDetails(popup, state, quantity) {
    if (!state.result) {
      popup.innerHTML = '<p class="pcut-details-popup__empty">Заполните количество пластин и отверстий, чтобы увидеть расчёт.</p>';
      return;
    }
    var result = state.result;
    var plateSeconds = formatSeconds(result.plateSeconds);
    var holeSeconds = formatSeconds(result.holeSeconds);
    var html = '<dl class="pcut-details-popup__list">'
      + detailRow('Рубка пластин', formatNumber(result.range.seconds, 2) + ' × '
        + formatNumber(result.plates, 0) + ' = ' + plateSeconds + ' с')
      + detailRow('Отверстия', formatNumber(HOLE_SECONDS, 2) + ' × '
        + formatNumber(result.holes, 0) + ' = ' + holeSeconds + ' с')
      + detailRow('Один набор', plateSeconds + ' + ' + holeSeconds + ' = '
        + formatHours(result.hours) + ' ч');
    if (quantity > 1) {
      html += detailRow('С учётом количества', formatHours(result.hours) + ' × '
        + formatNumber(quantity, 0) + ' = ' + formatHours(result.hours * quantity) + ' ч');
    }
    popup.innerHTML = html + '</dl>';
  }

  /**
   * The popup belonging to one details chevron.
   *
   * Resolved from the whole `__result-row`, not from the chevron's parent: the
   * chevron sits inside the `__time` value box while the popup is anchored to
   * the row beside the confirm button, so a `parentNode` lookup finds nothing.
   * Returns null rather than throwing if the markup ever changes again.
   */
  function detailsPopupFor(toggle) {
    var row = toggle.closest('.pcut-package__result-row');
    return row ? row.querySelector('[data-details-popup]') : null;
  }

  function closeDetails(except) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-details-toggle]'), function (toggle) {
      if (toggle === except) return;
      toggle.setAttribute('aria-expanded', 'false');
      var popup = detailsPopupFor(toggle);
      if (popup) popup.hidden = true;
    });
  }

  function packageElements() {
    return Array.prototype.slice.call(packagesEl.querySelectorAll('[data-package]'));
  }

  function isConfirmed(element) {
    return element.dataset.confirmed === 'true';
  }

  function setPackageConfirmed(element, confirmed) {
    element.dataset.confirmed = confirmed ? 'true' : 'false';
    Array.prototype.forEach.call(element.querySelectorAll('[data-field]'), function (field) {
      field.disabled = confirmed;
    });
    var button = element.querySelector('[data-confirm-package]');
    button.classList.toggle('link-button--success', !confirmed);
    button.classList.toggle('link-button--secondary', confirmed);
    button.innerHTML = confirmed ? EDIT_ICON : CHECK_ICON;
    button.setAttribute('aria-label', confirmed ? 'Редактировать пакет' : 'Подтвердить пакет');
    button.title = confirmed ? 'Редактировать пакет' : 'Подтвердить пакет';
  }

  /** Recalculate base totals, then apply quantity only to a fully confirmed set. */
  function refresh() {
    var elements = packageElements();
    var allConfirmed = elements.length > 0 && elements.every(isConfirmed);
    var quantityState = readSetQuantity();
    var quantity = allConfirmed && !quantityState.error ? quantityState.value : 1;
    var singleSetSeconds = 0;
    var skipped = 0;

    setQuantityEl.disabled = !allConfirmed;
    elements.forEach(function (element, index) {
      element.querySelector('[data-package-title]').textContent = 'Пакет ' + (index + 1);
      var remove = element.querySelector('[data-remove-package]');
      remove.hidden = index === 0;
      remove.disabled = isConfirmed(element);

      var state = readPackage(element);
      element.querySelector('[data-package-errors]').textContent = state.error || '';
      element.classList.toggle('pcut-package--invalid', Boolean(state.error));
      element.classList.toggle('pcut-package--confirmed', isConfirmed(element));
      element.querySelector('[data-package-time]').textContent = state.result
        ? formatHours(state.result.hours * quantity) + ' ч'
        : '—';
      renderDetails(element.querySelector('[data-details-popup]'), state, quantity);

      if (state.result) singleSetSeconds += state.result.seconds;
      else skipped += 1;
    });

    var grandTotalSeconds = singleSetSeconds * quantity;
    singleSetHoursEl.textContent = formatHours(singleSetSeconds / 3600);
    totalHoursEl.textContent = formatHours(grandTotalSeconds / 3600);
    totalSecondsEl.textContent = formatSeconds(grandTotalSeconds) + ' с';

    var messages = [];
    if (skipped) {
      messages.push('Не учтено пакетов с незаполненными или некорректными данными: '
        + skipped + '.');
    }
    if (allConfirmed && quantityState.error) messages.push(quantityState.error);
    totalSkippedEl.hidden = messages.length === 0;
    totalSkippedEl.textContent = messages.join(' ');
  }

  /** The sole package-row constructor; loaded rows arrive confirmed. */
  function createPackage(values, confirmed) {
    packagesEl.appendChild(template.content.cloneNode(true));
    var element = packageElements().pop();
    if (values) {
      element.querySelector('[data-field="range"]').value = String(values.range);
      element.querySelector('[data-field="plates"]').value = values.plates;
      element.querySelector('[data-field="holes"]').value = values.holes;
    }
    setPackageConfirmed(element, Boolean(confirmed));
    return element;
  }

  function addPackage() {
    var isFirst = packageElements().length === 0;
    var added = createPackage(null, false);
    refresh();
    if (!isFirst) added.querySelector('[data-field="plates"]').focus();
  }

  var saveModal = root.querySelector('[data-save-modal]');
  var conflictModal = root.querySelector('[data-conflict-modal]');
  var loadModal = root.querySelector('[data-load-modal]');
  var replaceModal = root.querySelector('[data-replace-modal]');
  var deleteModal = root.querySelector('[data-delete-modal]');
  var saveButton = root.querySelector('[data-open-save]');
  var loadButton = root.querySelector('[data-open-load]');
  var presetsUrl = root.dataset.presetsUrl || '';
  var presetCreateUrl = root.dataset.presetCreateUrl || '';
  var presetDeleteUrlTemplate = root.dataset.presetDeleteUrlTemplate || '';
  var canManagePresets = root.dataset.canManagePresets === 'true';
  var SEARCH_DELAY_MS = 250;
  var FEEDBACK_MS = 6000;
  var feedbackTimer = null;

  function showFeedback(message, isError) {
    if (!feedbackEl) return;
    feedbackEl.textContent = message;
    feedbackEl.classList.toggle('pcut-feedback--error', Boolean(isError));
    feedbackEl.hidden = false;
    window.clearTimeout(feedbackTimer);
    feedbackTimer = window.setTimeout(function () {
      feedbackEl.hidden = true;
      feedbackEl.textContent = '';
    }, FEEDBACK_MS);
  }

  function csrfToken() {
    var match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  async function request(url, options) {
    var response;
    try {
      response = await fetch(url, Object.assign({ credentials: 'same-origin' }, options));
    } catch (networkError) {
      throw new Error('Нет связи с сервером. Проверьте подключение и повторите.');
    }
    var payload = null;
    try { payload = await response.json(); } catch (parseError) { payload = null; }
    if (!response.ok) {
      var error = new Error((payload && payload.detail)
        || 'Не удалось выполнить запрос. Повторите попытку.');
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function serializePackages() {
    var elements = packageElements();
    if (!elements.length) return { error: 'Добавьте хотя бы один пакет.' };
    var packages = [];
    for (var index = 0; index < elements.length; index += 1) {
      if (!isConfirmed(elements[index])) {
        return { error: 'Подтвердите пакет ' + (index + 1) + ' перед сохранением.' };
      }
      var state = readPackage(elements[index]);
      if (!state.result) {
        return {
          error: 'Пакет ' + (index + 1) + ': '
            + (state.error || 'заполните количество пластин и отверстий.'),
        };
      }
      packages.push({
        range: elements[index].querySelector('[data-field="range"]').value,
        plates: state.result.plates,
        holes: state.result.holes,
      });
    }
    var quantity = readSetQuantity();
    if (quantity.error) return { error: quantity.error };
    return { packages: packages, setQuantity: quantity.value };
  }

  function hasEnteredData() {
    var elements = packageElements();
    if (elements.length > 1 || setQuantityEl.value.trim() !== '1') return true;
    return elements.some(function (element) {
      var plates = element.querySelector('[data-field="plates"]').value.trim();
      var holes = element.querySelector('[data-field="holes"]').value.trim();
      return plates !== '' || (holes !== '' && holes !== '0');
    });
  }

  function confirmReplace() {
    if (!hasEnteredData()) return Promise.resolve(true);
    var accept = replaceModal && replaceModal.querySelector('[data-replace-accept]');
    var cancel = replaceModal && replaceModal.querySelector('[data-replace-cancel]');
    if (!replaceModal || typeof replaceModal.showModal !== 'function' || !accept || !cancel) {
      return Promise.resolve(true);
    }
    return new Promise(function (resolve) {
      var accepted = false;
      function onAccept() { accepted = true; replaceModal.close(); }
      function onCancel() { replaceModal.close(); }
      function onClose() {
        accept.removeEventListener('click', onAccept);
        cancel.removeEventListener('click', onCancel);
        replaceModal.removeEventListener('close', onClose);
        resolve(accepted);
      }
      accept.addEventListener('click', onAccept);
      cancel.addEventListener('click', onCancel);
      replaceModal.addEventListener('close', onClose);
      replaceModal.showModal();
      cancel.focus();
    });
  }

  function applyPreset(preset) {
    var saved = (preset && preset.packages) || [];
    packagesEl.textContent = '';
    setQuantityEl.value = String((preset && preset.set_quantity) || 1);
    saved.forEach(function (values) { createPackage(values, true); });
    if (!packageElements().length) createPackage(null, false);
    refresh();
  }

  function wireSaveModal() {
    if (!saveButton || !canManagePresets) return;
    if (!saveModal || typeof saveModal.showModal !== 'function') {
      saveButton.hidden = true;
      return;
    }
    var form = saveModal.querySelector('[data-save-form]');
    var nameInput = saveModal.querySelector('[data-save-name]');
    var errorEl = saveModal.querySelector('[data-save-error]');
    var cancel = saveModal.querySelector('[data-save-cancel]');
    var submit = saveModal.querySelector('[data-save-submit]');
    var pendingPayload = null;

    function showError(element, message) {
      element.textContent = message;
      element.hidden = false;
    }

    function clearError(element) {
      element.textContent = '';
      element.hidden = true;
    }

    async function save(payload, conflictAction) {
      return request(presetCreateUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
        body: JSON.stringify({
          name: payload.name,
          packages: payload.packages,
          set_quantity: payload.setQuantity,
          conflict_action: conflictAction,
        }),
      });
    }

    function openConflict(payload, existingName) {
      pendingPayload = payload;
      saveModal.close();
      var textEl = conflictModal.querySelector('[data-conflict-text]');
      clearError(conflictModal.querySelector('[data-conflict-error]'));
      textEl.textContent = 'Набор «' + existingName + '» уже существует. Выберите действие.';
      conflictModal.showModal();
    }

    saveButton.addEventListener('click', function () {
      var serialized = serializePackages();
      if (serialized.error) {
        showFeedback(serialized.error, true);
        return;
      }
      clearError(errorEl);
      nameInput.value = '';
      submit.disabled = false;
      saveModal.showModal();
      nameInput.focus();
    });

    cancel.addEventListener('click', function () { saveModal.close(); });
    nameInput.addEventListener('input', function () {
      if (nameInput.value.trim()) clearError(errorEl);
    });

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      var name = nameInput.value.trim();
      if (!name) {
        showError(errorEl, 'Укажите название набора.');
        nameInput.focus();
        return;
      }
      var serialized = serializePackages();
      if (serialized.error) {
        showError(errorEl, serialized.error);
        return;
      }
      var payload = {
        name: name,
        packages: serialized.packages,
        setQuantity: serialized.setQuantity,
      };
      submit.disabled = true;
      try {
        var result = await save(payload, 'create');
        saveModal.close();
        showFeedback('Набор «' + result.preset.name + '» сохранён.', false);
      } catch (error) {
        if (error.status === 409 && error.payload && error.payload.code === 'name_conflict') {
          openConflict(payload, error.payload.preset.name);
        } else {
          showError(errorEl, error.message);
        }
      } finally {
        submit.disabled = false;
      }
    });

    var conflictCancel = conflictModal.querySelector('[data-conflict-cancel]');
    var overwrite = conflictModal.querySelector('[data-conflict-overwrite]');
    var copy = conflictModal.querySelector('[data-conflict-copy]');
    var conflictError = conflictModal.querySelector('[data-conflict-error]');

    conflictCancel.addEventListener('click', function () {
      conflictModal.close();
      saveModal.showModal();
      nameInput.focus();
    });

    async function resolveConflict(action) {
      overwrite.disabled = true;
      copy.disabled = true;
      clearError(conflictError);
      try {
        var result = await save(pendingPayload, action);
        conflictModal.close();
        showFeedback('Набор «' + result.preset.name + '» сохранён.', false);
      } catch (error) {
        showError(conflictError, error.message);
      } finally {
        overwrite.disabled = false;
        copy.disabled = false;
      }
    }

    overwrite.addEventListener('click', function () { resolveConflict('overwrite'); });
    copy.addEventListener('click', function () { resolveConflict('save_as_new'); });
  }

  function wireLoadModal() {
    if (!loadButton) return;
    if (!loadModal || typeof loadModal.showModal !== 'function') {
      loadButton.hidden = true;
      return;
    }
    var searchInput = loadModal.querySelector('[data-load-search]');
    var listEl = loadModal.querySelector('[data-preset-list]');
    var errorEl = loadModal.querySelector('[data-load-error]');
    var cancel = loadModal.querySelector('[data-load-cancel]');
    var searchTimer = null;
    var searchToken = 0;
    var pendingDelete = null;

    function showError(message) {
      errorEl.textContent = message;
      errorEl.hidden = false;
    }

    function clearError() {
      errorEl.textContent = '';
      errorEl.hidden = true;
    }

    function message(value) {
      var paragraph = document.createElement('p');
      paragraph.className = 'pcut-preset-list__empty';
      paragraph.textContent = value;
      listEl.textContent = '';
      listEl.appendChild(paragraph);
    }

    function renderPresets(presets, query) {
      if (!presets.length) {
        message(query ? 'Ничего не найдено. Измените запрос.' : 'Сохранённых наборов пока нет.');
        return;
      }
      listEl.textContent = '';
      presets.forEach(function (preset) {
        var row = document.createElement('div');
        row.className = 'pcut-preset';
        var info = document.createElement('div');
        info.className = 'pcut-preset__info';
        var name = document.createElement('p');
        name.className = 'pcut-preset__name';
        name.textContent = preset.name;
        var meta = document.createElement('p');
        meta.className = 'pcut-preset__meta';
        meta.textContent = preset.author + ' · ' + preset.created_at
          + ' · пакетов: ' + preset.package_count + ' · наборов: ' + preset.set_quantity;
        info.appendChild(name);
        info.appendChild(meta);

        var actions = document.createElement('div');
        actions.className = 'pcut-preset__actions';
        var load = document.createElement('button');
        load.className = 'link-button link-button--compact';
        load.type = 'button';
        load.dataset.loadPreset = String(preset.id);
        load.textContent = 'Загрузить';
        actions.appendChild(load);
        if (canManagePresets) {
          var remove = document.createElement('button');
          remove.className = 'link-button link-button--compact link-button--danger';
          remove.type = 'button';
          remove.dataset.deletePreset = String(preset.id);
          remove.dataset.presetName = preset.name;
          remove.textContent = 'Удалить';
          actions.appendChild(remove);
        }
        row.appendChild(info);
        row.appendChild(actions);
        listEl.appendChild(row);
      });
    }

    async function search(query) {
      var token = (searchToken += 1);
      clearError();
      message('Загрузка…');
      try {
        var payload = await request(
          presetsUrl + '?q=' + encodeURIComponent(query), { method: 'GET' },
        );
        if (token === searchToken) renderPresets(payload.presets || [], query);
      } catch (error) {
        if (token !== searchToken) return;
        listEl.textContent = '';
        showError(error.message);
      }
    }

    loadButton.addEventListener('click', function () {
      searchInput.value = '';
      clearError();
      loadModal.showModal();
      searchInput.focus();
      search('');
    });
    cancel.addEventListener('click', function () { loadModal.close(); });
    searchInput.addEventListener('input', function () {
      window.clearTimeout(searchTimer);
      var query = searchInput.value.trim();
      searchTimer = window.setTimeout(function () { search(query); }, SEARCH_DELAY_MS);
    });

    if (canManagePresets && deleteModal) {
      var deleteError = deleteModal.querySelector('[data-delete-error]');
      var deleteCancel = deleteModal.querySelector('[data-delete-cancel]');
      var deleteAccept = deleteModal.querySelector('[data-delete-accept]');
      deleteCancel.addEventListener('click', function () {
        deleteModal.close();
        loadModal.showModal();
        searchInput.focus();
      });
      deleteAccept.addEventListener('click', async function () {
        if (!pendingDelete) return;
        deleteAccept.disabled = true;
        deleteError.hidden = true;
        try {
          await request(presetDeleteUrlTemplate.replace('/0/', '/'
            + encodeURIComponent(pendingDelete.id) + '/'), {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken() },
          });
          var deletedName = pendingDelete.name;
          pendingDelete = null;
          deleteModal.close();
          loadModal.showModal();
          await search(searchInput.value.trim());
          showFeedback('Набор «' + deletedName + '» удалён.', false);
        } catch (error) {
          deleteError.textContent = error.message;
          deleteError.hidden = false;
        } finally {
          deleteAccept.disabled = false;
        }
      });
    }

    listEl.addEventListener('click', async function (event) {
      var deleteButton = event.target.closest('[data-delete-preset]');
      if (deleteButton && canManagePresets && deleteModal) {
        pendingDelete = {
          id: deleteButton.dataset.deletePreset,
          name: deleteButton.dataset.presetName,
        };
        deleteModal.querySelector('[data-delete-name]').textContent = pendingDelete.name;
        deleteModal.querySelector('[data-delete-error]').hidden = true;
        loadModal.close();
        deleteModal.showModal();
        return;
      }

      var button = event.target.closest('[data-load-preset]');
      if (!button || button.disabled) return;
      button.disabled = true;
      var payload;
      try {
        payload = await request(
          presetsUrl + encodeURIComponent(button.dataset.loadPreset) + '/',
          { method: 'GET' },
        );
      } catch (error) {
        button.disabled = false;
        showError(error.message);
        return;
      }
      button.disabled = false;
      loadModal.close();
      if (!(await confirmReplace())) {
        loadModal.showModal();
        searchInput.focus();
        return;
      }
      applyPreset(payload.preset);
      showFeedback('Набор «' + payload.preset.name + '» загружен.', false);
    });
  }

  addButton.addEventListener('click', addPackage);
  setQuantityEl.addEventListener('input', refresh);
  setQuantityEl.addEventListener('change', refresh);
  packagesEl.addEventListener('input', refresh);
  packagesEl.addEventListener('change', refresh);

  packagesEl.addEventListener('click', function (event) {
    var confirmation = event.target.closest('[data-confirm-package]');
    if (confirmation) {
      var packageElement = confirmation.closest('[data-package]');
      if (isConfirmed(packageElement)) {
        setPackageConfirmed(packageElement, false);
        refresh();
        packageElement.querySelector('[data-field="range"]').focus();
      } else {
        var state = readPackage(packageElement);
        if (!state.result) {
          refresh();
          showFeedback(state.error || 'Заполните пакет перед подтверждением.', true);
          return;
        }
        setPackageConfirmed(packageElement, true);
        refresh();
      }
      return;
    }

    var remove = event.target.closest('[data-remove-package]');
    if (remove && !remove.disabled) {
      remove.closest('[data-package]').remove();
      refresh();
      return;
    }

    var toggle = event.target.closest('[data-details-toggle]');
    if (!toggle) return;
    var open = toggle.getAttribute('aria-expanded') !== 'true';
    closeDetails(toggle);
    toggle.setAttribute('aria-expanded', String(open));
    var popup = detailsPopupFor(toggle);
    if (popup) popup.hidden = !open;
  });

  document.addEventListener('click', function (event) {
    if (event.target.closest('[data-details-toggle], [data-details-popup]')) return;
    closeDetails(null);
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') closeDetails(null);
  });

  wireSaveModal();
  wireLoadModal();
  addPackage();
})();
