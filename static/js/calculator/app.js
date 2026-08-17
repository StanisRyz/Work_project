/**
 * Interface controller, adapted from `js/app.js` of StanisRyz/calculate.
 *
 * The calculation and the result rendering are the source's own; what
 * changed is persistence — the file-selection shell, the database bar and
 * the revision-conflict handling are gone, and every mutation goes through
 * `windingCalculator.createJournalApi()` to Django instead.
 */
(function () {
  var api = window.windingCalculator;
  var root = document.querySelector('[data-calculator]');
  if (!root || !api) return;

  var form = document.getElementById('calc-form'), resultEl = document.getElementById('calc-result'), errorsEl = document.getElementById('calc-form-errors');
  var heightEl = document.getElementById('calc-height-display'), heightFormulaEl = document.getElementById('calc-height-formula'), calibrationDiameterField = document.getElementById('calc-calibration-diameter-field');
  var journalBody = document.getElementById('calc-journal-body'), journalEmpty = document.getElementById('calc-journal-empty');
  var exportButton = document.getElementById('calc-export'), reloadButton = document.getElementById('calc-reload'), statusEl = document.getElementById('calc-journal-status');
  var nameFilter = document.getElementById('calc-name-filter');
  var manualForm = document.getElementById('calc-manual-form'), manualErrorsEl = document.getElementById('calc-manual-errors');
  var deleteDialog = document.getElementById('calc-delete-dialog');

  var journalApi = api.createJournalApi(root.dataset.entriesUrl);
  var exportUrl = root.dataset.exportUrl;
  // Presentation only. The server re-checks the ПДО role on every mutation;
  // this flag decides which controls exist, so nothing here restates the rule.
  var canManage = root.dataset.canManageWorkup === '1';
  var latestCalculation = null, journal = [], busy = false;

  var formatNumber = api.formatNumber, formatDuration = api.formatDuration;

  function input() { var f = new FormData(form), d = Number(f.get('d')), D = Number(f.get('D')); return { d: d, D: D, b: Number(f.get('b')), windingHeightMm: api.calculateHeightMm(d, D), tapeThicknessMm: Number(f.get('tapeThicknessMm')), calibration: f.get('calibration'), calibrationDiameterMm: Number(f.get('calibrationDiameterMm')) }; }
  function toggleCalibrationDiameter() { calibrationDiameterField.hidden = form.elements.calibration.value !== 'Да'; }
  function showHeight() { var data = input(); if (!Number.isFinite(data.d) || !Number.isFinite(data.D) || data.d <= 0 || data.D <= data.d) { heightEl.textContent = '—'; heightFormulaEl.textContent = 'Введите положительные d и D, где D > d'; return; } heightEl.textContent = formatNumber(data.windingHeightMm, 2) + ' мм'; heightFormulaEl.textContent = '(' + formatNumber(data.D, 2) + ' − ' + formatNumber(data.d, 2) + ') / 2'; }
  function row(label, value) { return '<div><dt>' + label + '</dt><dd>' + value + '</dd></div>'; }
  function escapeHtml(value) { return String(value).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }

  function setStatus(text, stateClass) { statusEl.textContent = text; statusEl.className = 'calc-status' + (stateClass ? ' calc-status--' + stateClass : ''); }
  function setBusy(value) {
    busy = value;
    reloadButton.disabled = value;
    exportButton.disabled = value;
    if (manualForm) manualForm.querySelector('button[type="submit"]').disabled = value;
    Array.prototype.forEach.call(journalBody.querySelectorAll('.calc-confirm, .calc-edit, .calc-delete'), function (button) { button.disabled = value; });
  }
  /**
   * The production cell. Editable for ПДО, plain text for everyone else: a
   * disabled input would still say «you may type here one day», and the
   * journal is read-only for them by design.
   */
  function productionInput(className, entry, value, label, type, attributes) {
    if (!canManage) return '<span class="' + className + '">' + escapeHtml(value == null || value === '' ? '—' : value) + '</span>';
    var locked = entry.productionConfirmed ? ' disabled' : '';
    return '<input class="' + className + ' calc-production-input" type="' + type + '" ' + attributes + locked + ' value="' + escapeHtml(value == null ? '' : value) + '" aria-label="' + label + ' для ' + escapeHtml(entry.name) + '">';
  }
  var TRASH_ICON = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
  /** ✓ or ✎, then the trash — nothing at all for a non-ПДО reader. */
  function actionCell(entry) {
    if (!canManage) return '';
    var primary = entry.productionConfirmed
      ? '<button class="link-button link-button--compact link-button--secondary calc-edit" type="button" data-id="' + entry.id + '" title="Редактировать" aria-label="Редактировать производственные данные">✎</button>'
      : '<button class="link-button link-button--compact calc-confirm" type="button" data-id="' + entry.id + '" title="Подтвердить" aria-label="Подтвердить производственные данные">✓</button>';
    var remove = '<button class="link-button link-button--compact link-button--danger calc-delete" type="button" data-id="' + entry.id + '" title="Удалить строку" aria-label="Удалить строку магнитопровода ' + escapeHtml(entry.name) + ' из проработки">' + TRASH_ICON + '</button>';
    return '<td><div class="calc-row-actions">' + primary + remove + '</div></td>';
  }
  /** Millimetres as typed: «100» stays «100», a hand-added «100,5» stays «100,5». */
  function formatMm(value) { return Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 }); }
  /**
   * The «1С» cell shows two different things on purpose: the expression while
   * the row is being edited, because that is what the user types and edits,
   * and the hours it evaluates to once the row is confirmed, because that is
   * the number the column is headed with. The expression is never lost — it
   * stays in `oneCExpression` and comes back when the row is reopened with ✎.
   */
  function oneCValue(entry) {
    // A reader who cannot edit is never shown the raw seconds expression: the
    // column is headed in hours and there is no field to hand it back to.
    if (!entry.productionConfirmed && canManage) return entry.oneCExpression;
    return entry.oneCHours == null ? '' : formatNumber(entry.oneCHours, 3);
  }
  /** «Нет» is a state, not a missing number: an uncalibrated core says so. */
  function calibrationCell(entry) {
    return entry.calibrationEnabled && entry.calibrationDiameterMm ? formatMm(entry.calibrationDiameterMm) : 'Нет';
  }
  function renderJournal() {
    var filtered = api.filterJournalEntries(journal, { name: nameFilter.value });
    journalBody.innerHTML = filtered.map(function (entry) {
      // The column header already carries the hour: «0,083», not «0,083 ч».
      // An unconfirmed row simply shows «—» — ✓ says the rest.
      var unitTime = entry.actualUnitTimeHours == null ? '—' : formatNumber(entry.actualUnitTimeHours, 3);
      return '<tr data-id="' + entry.id + '">'
        + '<td>' + formatMm(entry.d) + '</td>'
        + '<td>' + formatMm(entry.D) + '</td>'
        + '<td>' + formatMm(entry.b) + '</td>'
        + '<td>' + formatNumber(entry.heightMm, 2) + '</td>'
        + '<td>' + formatMm(entry.tapeThicknessMm) + '</td>'
        + '<td>' + calibrationCell(entry) + '</td>'
        // The winding speed the calculation already produced and stored.
        + '<td>' + formatNumber(entry.standardCoefficient, 2) + '</td>'
        + '<td>×' + formatNumber(entry.complexityCoefficient, 2) + '</td>'
        + '<td>' + formatNumber(api.calculatedTimeHours(entry.totalTimeSeconds), 3) + '</td>'
        + '<td class="calc-col-compact">' + productionInput('calc-batch-quantity', entry, entry.batchQuantity, 'Единиц в партии', 'number', 'min="1" step="1" inputmode="numeric"') + '</td>'
        + '<td class="calc-col-compact">' + productionInput('calc-actual-batch-time', entry, entry.actualBatchTimeHours, 'Фактическое время партии в часах', 'number', 'min="0" step="0.01" inputmode="decimal"') + '</td>'
        + '<td class="calc-col-compact"><span class="calc-unit-time">' + unitTime + '</span><small class="calc-production-error" aria-live="polite"></small></td>'
        // Text, not `number`: the field accepts «4.4*62» as well as «3600».
        + '<td>' + productionInput('calc-one-c', entry, oneCValue(entry), 'Время 1С', 'text', 'inputmode="text"') + '</td>'
        + '<td>' + productionInput('calc-employee', entry, entry.employeeName, 'Сотрудник', 'text', 'maxlength="120"') + '</td>'
        + actionCell(entry) + '</tr>';
    }).join('');
    journalEmpty.hidden = filtered.length > 0;
    journalEmpty.textContent = journal.length ? 'По заданным условиям записей не найдено.' : 'Записей пока нет. Выполните расчёт или добавьте строку вручную.';
  }
  function applyEntry(entry) {
    var replaced = false;
    journal = journal.map(function (item) { if (item.id !== entry.id) return item; replaced = true; return entry; });
    if (!replaced) journal = journal.concat([entry]);
    renderJournal();
  }
  async function loadJournal() {
    setStatus('Загрузка журнала…', 'busy');
    setBusy(true);
    try {
      journal = await journalApi.load();
      renderJournal();
      setStatus('Журнал загружен: ' + journal.length + ' записей.');
    } catch (error) {
      setStatus(error.message, 'error');
    } finally { setBusy(false); }
  }

  function switchTab(tab) {
    document.getElementById('calc-tab-calculator').hidden = tab !== 'calculator';
    document.getElementById('calc-tab-journal').hidden = tab !== 'journal';
    Array.prototype.forEach.call(root.querySelectorAll('.calc-tab'), function (button) {
      var active = button.dataset.tab === tab;
      button.classList.toggle('calc-tab--active', active);
      button.setAttribute('aria-selected', active);
    });
  }
  async function addCurrentCalculation() {
    // Outside ПДО the calculator is a calculator: the result is shown, and
    // nothing is written. Not an error either — there is nothing to report.
    if (!latestCalculation || !canManage) return;
    var name = api.magneticCoreName(latestCalculation.data);
    // No local duplicate check: whether this is a new case depends on the
    // tape and the calibration too, and only the server knows the signature.
    setStatus('Сохранение расчёта…', 'busy');
    try {
      var payload = await journalApi.create(api.buildEntryPayload(name, latestCalculation.data, latestCalculation.result));
      applyEntry(payload.entry);
      setStatus(payload.created
        ? 'Магнитопровод ' + payload.entry.name + ' добавлен в «Проработку».'
        : 'Этот расчёт магнитопровода ' + payload.entry.name + ' уже есть в «Проработке».');
    } catch (error) { setStatus(error.message, 'error'); }
  }

  /** A row typed into «Проработка»: same engine, and duplicates are allowed. */
  async function addManualEntry() {
    var raw = {};
    ['d', 'D', 'b', 'tapeThicknessMm', 'calibrationDiameterMm'].forEach(function (key) { raw[key] = manualForm.elements[key].value; });
    var checked = api.validateManualInput(raw);
    manualErrorsEl.innerHTML = Object.keys(checked.errors).map(function (key) { return '<span>' + escapeHtml(checked.errors[key]) + '</span>'; }).join(' ');
    if (!checked.data) return;
    var data = checked.data, result = api.calculateWinding(data);
    setBusy(true);
    setStatus('Добавление строки…', 'busy');
    try {
      var payload = await journalApi.createManual(api.buildEntryPayload(api.magneticCoreName(data), data, result));
      applyEntry(payload.entry);
      // Cleared entirely: the next hand-added row is a new set of parameters.
      manualForm.reset();
      setStatus('Строка ' + payload.entry.name + ' добавлена в «Проработку».');
    } catch (error) {
      manualErrorsEl.textContent = error.message;
      setStatus(error.message, 'error');
    } finally { setBusy(false); }
  }

  function render(r, data) { resultEl.innerHTML = '<h2>Результат</h2><section class="calc-user-result"><h3>Время навивки</h3><div class="calc-time">' + formatNumber(r.totalTimeSeconds, 2) + ' с <small>(' + formatDuration(r.totalTimeSeconds) + ')</small></div><dl>' + row('Скорость навивки', formatNumber(r.standard, 2) + ' сек/мм') + row('Коэффициент сложности', '×' + formatNumber(r.complexityCoefficient, 2) + ' (учтены доп. операции)') + row('Параметр обработки — высота навивки', formatNumber(r.heightMm, 2) + ' мм') + '</dl></section><section class="calc-developer-details"><h3>Коэффициенты и детали расчёта</h3><dl>' + row('Высота навивки', formatNumber(r.heightMm, 2) + ' мм') + row('Рассчитанная масса', formatNumber(r.massKg, 4) + ' кг') + row('Стандартный коэффициент (4,4 × 0,3 / толщина)', formatNumber(r.standard, 2) + ' сек/мм') + row('Время навивки', formatNumber(r.windingTimeSeconds, 2) + ' с') + row('Калибровка', data.calibration) + (data.calibration === 'Да' ? row('Радиус калибровки', formatNumber(data.calibrationDiameterMm, 2) + ' мм') + row('Время калибровки', formatNumber(r.calibrationTimeSeconds, 2) + ' с') : '') + row(data.d < 80 ? 'Базовое время доп. операций для d < 80' : 'Базовое время доп. операций от массы', formatNumber(r.baseAdditionalTimeFromMassSeconds, 2) + ' с') + row('Базовое время доп. операций с калибровкой', formatNumber(r.baseAdditionalTimeSeconds, 2) + ' с') + row('Множитель ширины b', '×' + formatNumber(r.widthMultiplier, 2)) + row('Множитель толщины', '×' + formatNumber(r.thicknessMultiplier, 2)) + row('Множитель массы', '×' + formatNumber(r.massMultiplier, 2)) + row('Множитель высоты', '×' + formatNumber(r.heightMultiplier, 2)) + row('Множитель внутреннего диаметра d', '×' + formatNumber(r.innerDiameterMultiplier, 2)) + row('Время дополнительных операций', formatNumber(r.additionalOperationsTimeSeconds, 2) + ' с') + '</dl></section>'; }

  var renderWithoutHoopDetails = render;
  render = function (calculation, data) {
    renderWithoutHoopDetails(calculation, data);
    var details = resultEl.querySelector('.calc-developer-details dl');
    details.insertAdjacentHTML('beforeend', row('Отношение d / H', formatNumber(calculation.hoopRatio, 2)) + row('Минимум обручности по d', '×' + formatNumber(calculation.hoopDiameterFactor, 2)) + row('Коэффициент обручности', '×' + formatNumber(calculation.hoopMultiplier, 2)));
  };

  ['d', 'D'].forEach(function (name) { form.elements[name].addEventListener('input', showHeight); });
  form.elements.calibration.addEventListener('change', toggleCalibrationDiameter);
  root.querySelector('.calc-tabs').addEventListener('click', function (event) { if (event.target.matches('.calc-tab')) switchTab(event.target.dataset.tab); });
  nameFilter.addEventListener('input', renderJournal);
  // Absent entirely for a non-ПДО user: the template does not render it.
  if (manualForm) manualForm.addEventListener('submit', function (event) { event.preventDefault(); if (!busy) addManualEntry(); });

  journalBody.addEventListener('click', async function (event) {
    // `closest`, not the event target: the trash button's own SVG is what gets
    // clicked most of the time.
    var button = event.target.closest ? event.target.closest('button') : null;
    if (!button) return;
    if (button.matches('.calc-delete')) {
      if (!busy) askToDelete(Number(button.dataset.id));
      return;
    }
    if (!button.matches('.calc-confirm, .calc-edit') || busy) return;
    var id = Number(button.dataset.id);
    var rowEl = button.closest('tr'), errorEl = rowEl.querySelector('.calc-production-error');
    errorEl.textContent = '';
    setBusy(true);
    try {
      if (button.matches('.calc-edit')) {
        applyEntry((await journalApi.unlockProduction(id)).entry);
        setStatus('Производственные данные открыты для редактирования.');
        return;
      }
      var batchQuantity = rowEl.querySelector('.calc-batch-quantity').value;
      var actualBatchTimeHours = rowEl.querySelector('.calc-actual-batch-time').value;
      var oneCExpression = rowEl.querySelector('.calc-one-c').value;
      var errors = api.validateProductionData(batchQuantity, actualBatchTimeHours, oneCExpression);
      if (Object.keys(errors).length) {
        errorEl.textContent = Object.keys(errors).map(function (key) { return errors[key]; }).join(' ');
        return;
      }
      // «1С» goes as the typed expression: the server parses it and stores
      // both it and the number it evaluates to.
      applyEntry((await journalApi.confirmProduction(id, {
        batchQuantity: Number(batchQuantity),
        actualBatchTimeHours: Number(actualBatchTimeHours),
        oneCExpression: oneCExpression,
        employeeName: rowEl.querySelector('.calc-employee').value
      })).entry);
      setStatus('Производственные данные сохранены.');
    } catch (error) {
      errorEl.textContent = error.message;
      setStatus(error.message, 'error');
    } finally { setBusy(false); }
  });

  /**
   * Deletion: the module's own `<dialog>`, never `confirm()`.
   *
   * It reuses the application's modal and button classes, but owns its own
   * markup because the answer is a `fetch()`, not a form submission — the row
   * disappears from the table only after the server says it is gone.
   */
  var deleteText = document.getElementById('calc-delete-text');
  var deleteError = document.getElementById('calc-delete-error');
  var deleteAccept = document.getElementById('calc-delete-accept');
  var pendingDeleteId = null;
  function askToDelete(id) {
    var entry = journal.filter(function (item) { return item.id === id; })[0];
    if (!entry || !deleteDialog || typeof deleteDialog.showModal !== 'function') return;
    pendingDeleteId = id;
    deleteText.textContent = 'Магнитопровод ' + entry.name + ' будет удалён из «Проработки».';
    deleteError.hidden = true;
    deleteError.textContent = '';
    deleteAccept.disabled = false;
    deleteDialog.showModal();
    deleteAccept.focus();
  }
  async function confirmDelete() {
    if (pendingDeleteId == null) return;
    var id = pendingDeleteId;
    deleteAccept.disabled = true;
    try {
      await journalApi.remove(id);
    } catch (error) {
      deleteError.textContent = error.message;
      deleteError.hidden = false;
      deleteAccept.disabled = false;
      return;
    }
    // Only now: the local journal follows the database, never predicts it.
    journal = journal.filter(function (item) { return item.id !== id; });
    pendingDeleteId = null;
    deleteDialog.close();
    renderJournal();
    setStatus('Строка удалена из «Проработки».');
  }
  if (deleteDialog) {
    document.getElementById('calc-delete-cancel').addEventListener('click', function () { deleteDialog.close(); });
    deleteAccept.addEventListener('click', function () { confirmDelete(); });
    deleteDialog.addEventListener('close', function () { pendingDeleteId = null; });
  }

  reloadButton.addEventListener('click', function () { if (!busy) loadJournal(); });

  exportButton.addEventListener('click', async function () {
    if (busy) return;
    setBusy(true);
    setStatus('Формирование выгрузки…', 'busy');
    try {
      // Downloaded through fetch so a server-side refusal arrives as a
      // message instead of replacing the page with raw JSON. The export
      // itself never refuses: every row goes in, confirmed or not.
      var response = await fetch(exportUrl, { credentials: 'same-origin' });
      if (!response.ok) {
        var payload = await response.json().catch(function () { return null; });
        throw new Error((payload && payload.detail) || 'Не удалось сформировать выгрузку.');
      }
      var blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = 'проработка.xlsx'; link.click(); URL.revokeObjectURL(url);
      setStatus('Выгрузка сформирована.');
    } catch (error) { setStatus(error.message, 'error'); }
    finally { setBusy(false); }
  });

  toggleCalibrationDiameter();
  showHeight();
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var data = input(), errors = api.validateInput(data);
    errorsEl.innerHTML = Object.keys(errors).map(function (key) { return '<p>' + errors[key] + '</p>'; }).join('');
    if (Object.keys(errors).length) {
      latestCalculation = null;
      resultEl.innerHTML = '<h2>Результат</h2><p class="calc-placeholder">Расчёт не выполнен: исправьте параметры формы.</p>';
      return;
    }
    var calculated = api.calculateWinding(data);
    latestCalculation = { data: data, result: calculated };
    render(calculated, data);
    addCurrentCalculation();
  });

  loadJournal();
})();
