/**
 * Калькулятор рубки пластин.
 *
 * Entirely client-side once the page is rendered: no fetch, no storage, no
 * shared state. The coefficients are not restated here — a package reads the
 * seconds-per-plate off its selected `<option>` and the hole coefficient off
 * the module's root, both rendered from `plate_cutting/constants.py`.
 *
 * `calculatePackage()` is the only implementation of the formula: the visible
 * «Время пакета», the calculation popup and the «Итого» all render from the
 * object it returns.
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

  function addPackage() {
    var isFirst = packageElements().length === 0;
    packagesEl.appendChild(template.content.cloneNode(true));
    refresh();
    if (isFirst) return;
    var added = packageElements().pop();
    added.querySelector('[data-field="plates"]').focus();
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

  addPackage();
})();
