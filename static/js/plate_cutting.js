/**
 * Калькулятор рубки пластин.
 *
 * The arithmetic is entirely client-side: the coefficients are not restated
 * here — a package reads the seconds-per-plate off its selected `<option>` and
 * the hole coefficient off the module's root, both rendered from
 * `plate_cutting/constants.py`.
 *
 * `calculatePackage()` is the only implementation of the formula: the visible
 * «Время пакета», the calculation popup and the «Итого» all render from the
 * object it returns.
 *
 * The saved package sets («Сохранить»/«Загрузить») are the one thing that
 * talks to the server, and they carry inputs only — the band, the plates and
 * the holes of every package, in order. Nothing calculated is ever sent or
 * received: a loaded set is rebuilt from the same `<template>` the «+
 * Дополнительный пакет» button uses and then goes through `refresh()`, so the
 * times on screen always come from the formula above.
 */
(function () {
  var root = document.querySelector('[data-plate-cutting]');
  if (!root) return;

  var HOLE_SECONDS = Number(root.dataset.holeSeconds);
  var packagesEl = root.querySelector('[data-packages]');
  var template = root.querySelector('[data-package-template]');
  var addButton = root.querySelector('[data-add-package]');
  var totalHoursEl = root.querySelector('[data-total-hours]');
  var totalSecondsEl = root.querySelector('[data-total-seconds]');
  var totalSkippedEl = root.querySelector('[data-total-skipped]');
  var feedbackEl = root.querySelector('[data-feedback]');
  if (!packagesEl || !template || !addButton) return;

  var INTEGER = /^\d+$/;

  /* ---------------------------------------------------------------- format */

  function formatNumber(value, digits) {
    return Number(value).toLocaleString('ru-RU', {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
  }

  /** Seconds for display only: binary noise trimmed. The value itself is
   *  never rounded before it has been used in the arithmetic. */
  function formatSeconds(value) {
    return Number(Number(value).toFixed(4)).toLocaleString('ru-RU', { maximumFractionDigits: 4 });
  }

  function formatHours(value) {
    return formatNumber(value, 2);
  }

  /* ------------------------------------------------------------ calculation */

  /**
   * The formula, in one place.
   * `package_seconds = range_seconds × plates + 0.95 × holes`, converted to
   * hours only afterwards. Nothing in between is rounded.
   */
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

  /* ------------------------------------------------------------- validation */

  /** The selected band, or null if the `<select>` holds nothing known. */
  function readRange(select) {
    var option = select.options[select.selectedIndex];
    if (!option) return null;
    var seconds = Number(option.dataset.seconds);
    if (!isFinite(seconds) || seconds <= 0) return null;
    return { label: option.textContent.trim(), seconds: seconds };
  }

  /**
   * A package's inputs, or the reason it cannot take part in the total.
   * An empty required field is «incomplete», not an error: a fresh package
   * must not greet the user with red text.
   */
  function readPackage(element) {
    var range = readRange(element.querySelector('[data-field="range"]'));
    var platesText = element.querySelector('[data-field="plates"]').value.trim();
    var holesText = element.querySelector('[data-field="holes"]').value.trim();

    if (!range) {
      return { error: 'Выберите диапазон длины пластины.' };
    }
    if (!platesText) {
      return { incomplete: true };
    }
    if (!INTEGER.test(platesText) || Number(platesText) <= 0) {
      return { error: 'Количество пластин — целое число больше 0.' };
    }
    if (!holesText) {
      return { incomplete: true };
    }
    if (!INTEGER.test(holesText)) {
      return { error: 'Количество отверстий, всего — целое число от 0.' };
    }
    return { result: calculatePackage(range, Number(platesText), Number(holesText)) };
  }

  /* ---------------------------------------------------------------- details */

  function detailRow(term, value) {
    return '<div><dt>' + term + '</dt><dd>' + value + '</dd></div>';
  }

  /**
   * The breakdown, rendered from the very object the visible result came
   * from — there is no second calculation behind the popup.
   *
   * Three lines only: the two halves of the formula and their sum. Every
   * figure they are built from — the band, both coefficients and both
   * quantities — is already on screen in the package's own fields, and the
   * hours are in «Время пакета» right next to the chevron.
   */
  function renderDetails(popup, state) {
    if (!state.result) {
      popup.innerHTML = '<p class="pcut-details-popup__empty">Заполните количество пластин и отверстий, чтобы увидеть расчёт.</p>';
      return;
    }
    var r = state.result;
    // Coefficients are agreed two-decimal figures: show them as such.
    var coefficient = formatNumber(r.range.seconds, 2);
    var holeCoefficient = formatNumber(HOLE_SECONDS, 2);
    var plateSeconds = formatSeconds(r.plateSeconds);
    var holeSeconds = formatSeconds(r.holeSeconds);
    popup.innerHTML = '<dl class="pcut-details-popup__list">'
      + detailRow('Рубка пластин', coefficient + ' × ' + formatNumber(r.plates, 0) + ' = ' + plateSeconds + ' с')
      + detailRow('Отверстия', holeCoefficient + ' × ' + formatNumber(r.holes, 0) + ' = ' + holeSeconds + ' с')
      + detailRow('Итого', plateSeconds + ' + ' + holeSeconds + ' = ' + formatSeconds(r.seconds) + ' с')
      + '</dl>';
  }

  /** At most one breakdown stays open; `except` keeps the one being toggled. */
  function closeDetails(except) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-details-toggle]'), function (toggle) {
      if (toggle === except) return;
      toggle.setAttribute('aria-expanded', 'false');
      toggle.parentNode.querySelector('[data-details-popup]').hidden = true;
    });
  }

  /* ----------------------------------------------------------------- render */

  function packageElements() {
    return Array.prototype.slice.call(packagesEl.querySelectorAll('[data-package]'));
  }

  /** Recalculate every package and the total. The single entry point: every
   *  keystroke, every added package and every removal ends up here. */
  function refresh() {
    var totalSeconds = 0;
    var skipped = 0;

    packageElements().forEach(function (element, index) {
      element.querySelector('[data-package-title]').textContent = 'Пакет ' + (index + 1);
      // The first package is permanent; only added ones can be removed.
      element.querySelector('[data-remove-package]').hidden = index === 0;

      var state = readPackage(element);
      element.querySelector('[data-package-errors]').textContent = state.error || '';
      element.classList.toggle('pcut-package--invalid', Boolean(state.error));
      element.querySelector('[data-package-time]').textContent =
        state.result ? formatHours(state.result.hours) + ' ч' : '—';
      renderDetails(element.querySelector('[data-details-popup]'), state);

      if (state.result) {
        totalSeconds += state.result.seconds;
      } else {
        skipped += 1;
      }
    });

    totalHoursEl.textContent = formatHours(totalSeconds / 3600);
    totalSecondsEl.textContent = formatSeconds(totalSeconds) + ' с';
    totalSkippedEl.hidden = skipped === 0;
    totalSkippedEl.textContent = skipped
      ? 'Не учтено пакетов с незаполненными или некорректными данными: ' + skipped + '.'
      : '';
  }

  /**
   * One package row from the page's own `<template>`, optionally pre-filled.
   * The single way a row is created — «+ Дополнительный пакет» and a loaded
   * preset both come through here, so there is no second row implementation.
   */
  function createPackage(values) {
    packagesEl.appendChild(template.content.cloneNode(true));
    var element = packageElements().pop();
    if (values) {
      element.querySelector('[data-field="range"]').value = String(values.range);
      element.querySelector('[data-field="plates"]').value = values.plates;
      element.querySelector('[data-field="holes"]').value = values.holes;
    }
    return element;
  }

  function addPackage() {
    var isFirst = packageElements().length === 0;
    var added = createPackage(null);
    refresh();
    if (isFirst) return;
    added.querySelector('[data-field="plates"]').focus();
  }

  /* -------------------------------------------------------- saved package sets */

  /*
   * Everything below is the «Сохранить»/«Загрузить» library. It reads and
   * writes package *inputs* and nothing else: no seconds, no hours and no
   * formula text ever leaves or enters the page, so a set saved a year ago is
   * recalculated by today's `calculatePackage()`.
   */

  var saveModal = root.querySelector('[data-save-modal]');
  var loadModal = root.querySelector('[data-load-modal]');
  var saveButton = root.querySelector('[data-open-save]');
  var loadButton = root.querySelector('[data-open-load]');
  var presetsUrl = root.dataset.presetsUrl || '';
  var presetCreateUrl = root.dataset.presetCreateUrl || '';
  var SEARCH_DELAY_MS = 250;
  var FEEDBACK_MS = 6000;
  var feedbackTimer = null;

  /** A short controlled message under the actions — never an alert(). */
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

  /** One request, with the server's own explanation preserved on a refusal. */
  async function request(url, options) {
    var response;
    try {
      response = await fetch(url, Object.assign({ credentials: 'same-origin' }, options));
    } catch (error) {
      throw new Error('Нет связи с сервером. Проверьте подключение и повторите.');
    }
    var payload = null;
    try { payload = await response.json(); } catch (error) { payload = null; }
    if (response.redirected || response.status === 401 || response.status === 403) {
      throw new Error('Сессия завершена. Обновите страницу и войдите заново.');
    }
    if (!response.ok) {
      throw new Error((payload && payload.detail) || 'Не удалось выполнить запрос. Повторите попытку.');
    }
    return payload;
  }

  /**
   * The packages on screen, as the backend wants them: band identifier, plates
   * and holes, in the order they are displayed. A package that cannot be
   * calculated cannot be saved either — the same rules `readPackage()` applies.
   */
  function serializePackages() {
    var elements = packageElements();
    if (!elements.length) {
      return { error: 'Добавьте хотя бы один пакет.' };
    }
    var packages = [];
    for (var index = 0; index < elements.length; index += 1) {
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
    return { packages: packages };
  }

  /**
   * Replace — never extend — the calculator with a saved set, then recalculate.
   * The rows come from the page's own `<template>` through `createPackage()`,
   * and `refresh()` fills in every time from the current formula.
   */
  function applyPreset(preset) {
    var saved = (preset && preset.packages) || [];
    packagesEl.textContent = '';
    saved.forEach(function (values) { createPackage(values); });
    if (!packageElements().length) createPackage(null);
    refresh();
  }

  /* ---------------------------------------------------------- save modal */

  function wireSaveModal() {
    if (!saveButton) return;
    if (!saveModal || typeof saveModal.showModal !== 'function') {
      saveButton.hidden = true;
      return;
    }
    var form = saveModal.querySelector('[data-save-form]');
    var nameInput = saveModal.querySelector('[data-save-name]');
    var errorEl = saveModal.querySelector('[data-save-error]');
    var cancel = saveModal.querySelector('[data-save-cancel]');
    var submit = saveModal.querySelector('[data-save-submit]');

    function showError(message) {
      errorEl.textContent = message;
      errorEl.hidden = false;
    }

    function clearError() {
      errorEl.textContent = '';
      errorEl.hidden = true;
    }

    saveButton.addEventListener('click', function () {
      // Refuse before the dialog opens: an incomplete package is a page-level
      // problem, and its own red text already says which one.
      var serialized = serializePackages();
      if (serialized.error) {
        showFeedback(serialized.error, true);
        return;
      }
      clearError();
      nameInput.value = '';
      submit.disabled = false;
      saveModal.showModal();
      nameInput.focus();
    });

    cancel.addEventListener('click', function () { saveModal.close(); });
    nameInput.addEventListener('input', function () {
      if (nameInput.value.trim()) clearError();
    });

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      var name = nameInput.value.trim();
      if (!name) {
        showError('Укажите название набора.');
        nameInput.focus();
        return;
      }
      var serialized = serializePackages();
      if (serialized.error) {
        showError(serialized.error);
        return;
      }
      submit.disabled = true;
      try {
        await request(presetCreateUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
          body: JSON.stringify({ name: name, packages: serialized.packages }),
        });
      } catch (error) {
        submit.disabled = false;
        showError(error.message);
        return;
      }
      // The calculator itself is deliberately left exactly as it was.
      saveModal.close();
      showFeedback('Набор «' + name + '» сохранён.', false);
    });
  }

  /* ---------------------------------------------------------- load modal */

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
    // Only the newest search may paint the list: a slow earlier answer that
    // arrives afterwards is dropped instead of overwriting it.
    var searchToken = 0;

    function showError(message) {
      errorEl.textContent = message;
      errorEl.hidden = false;
    }

    function clearError() {
      errorEl.textContent = '';
      errorEl.hidden = true;
    }

    function message(text) {
      var paragraph = document.createElement('p');
      paragraph.className = 'pcut-preset-list__empty';
      paragraph.textContent = text;
      listEl.textContent = '';
      listEl.appendChild(paragraph);
    }

    /** Built with DOM nodes, so a preset name is text and never markup. */
    function renderPresets(presets, query) {
      if (!presets.length) {
        message(query
          ? 'Ничего не найдено. Измените запрос.'
          : 'Сохранённых наборов пока нет. Сохраните первый набор кнопкой «Сохранить».');
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
          + ' · пакетов: ' + preset.package_count;
        info.appendChild(name);
        info.appendChild(meta);

        var button = document.createElement('button');
        button.className = 'link-button link-button--compact';
        button.type = 'button';
        button.dataset.loadPreset = String(preset.id);
        button.textContent = 'Загрузить';

        row.appendChild(info);
        row.appendChild(button);
        listEl.appendChild(row);
      });
    }

    async function search(query) {
      var token = (searchToken += 1);
      clearError();
      message('Загрузка…');
      var payload;
      try {
        payload = await request(presetsUrl + '?q=' + encodeURIComponent(query), { method: 'GET' });
      } catch (error) {
        if (token !== searchToken) return;
        listEl.textContent = '';
        showError(error.message);
        return;
      }
      if (token !== searchToken) return;
      renderPresets(payload.presets || [], query);
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

    listEl.addEventListener('click', async function (event) {
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
      loadModal.close();
      applyPreset(payload.preset);
      showFeedback('Набор «' + payload.preset.name + '» загружен.', false);
    });
  }

  /* ---------------------------------------------------------------- wiring */

  addButton.addEventListener('click', addPackage);

  packagesEl.addEventListener('input', refresh);
  packagesEl.addEventListener('change', refresh);

  packagesEl.addEventListener('click', function (event) {
    var remove = event.target.closest('[data-remove-package]');
    if (remove) {
      remove.closest('[data-package]').remove();
      refresh();
      return;
    }

    var toggle = event.target.closest('[data-details-toggle]');
    if (!toggle) return;
    var open = toggle.getAttribute('aria-expanded') !== 'true';
    closeDetails(toggle);
    toggle.setAttribute('aria-expanded', String(open));
    toggle.parentNode.querySelector('[data-details-popup]').hidden = !open;
  });

  // Click only — never hover — and anything outside an open breakdown closes it.
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
