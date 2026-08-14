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
  var journalFilters = { name: document.getElementById('calc-name-filter'), d: document.getElementById('calc-d-filter'), D: document.getElementById('calc-D-filter'), b: document.getElementById('calc-b-filter') };

  var journalApi = api.createJournalApi(root.dataset.entriesUrl);
  var exportUrl = root.dataset.exportUrl;
  var latestCalculation = null, journal = [], busy = false;

  var formatNumber = api.formatNumber, formatDuration = api.formatDuration;

  function input() { var f = new FormData(form), d = Number(f.get('d')), D = Number(f.get('D')); return { d: d, D: D, b: Number(f.get('b')), windingHeightMm: api.calculateHeightMm(d, D), tapeThicknessMm: Number(f.get('tapeThicknessMm')), calibration: f.get('calibration'), calibrationDiameterMm: Number(f.get('calibrationDiameterMm')) }; }
  function toggleCalibrationDiameter() { calibrationDiameterField.hidden = form.elements.calibration.value !== 'Да'; }
  function showHeight() { var data = input(); if (!Number.isFinite(data.d) || !Number.isFinite(data.D) || data.d <= 0 || data.D <= data.d) { heightEl.textContent = '—'; heightFormulaEl.textContent = 'Введите положительные d и D, где D > d'; return; } heightEl.textContent = formatNumber(data.windingHeightMm, 2) + ' мм'; heightFormulaEl.textContent = '(' + formatNumber(data.D, 2) + ' − ' + formatNumber(data.d, 2) + ') / 2'; }
  function row(label, value) { return '<div><dt>' + label + '</dt><dd>' + value + '</dd></div>'; }
  function escapeHtml(value) { return String(value).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function formatHours(value) { return formatNumber(value, 3) + ' ч'; }

  function setStatus(text, stateClass) { statusEl.textContent = text; statusEl.className = 'calc-status' + (stateClass ? ' calc-status--' + stateClass : ''); }
  function setBusy(value) {
    busy = value;
    reloadButton.disabled = value;
    exportButton.disabled = value;
    Array.prototype.forEach.call(journalBody.querySelectorAll('.calc-confirm, .calc-edit'), function (button) { button.disabled = value; });
  }
  function currentFilters() { return { name: journalFilters.name.value, d: journalFilters.d.value, D: journalFilters.D.value, b: journalFilters.b.value }; }
  function productionInput(className, entry, value, label, attributes) {
    var locked = entry.productionConfirmed ? ' disabled' : '';
    return '<input class="' + className + ' calc-production-input" type="number" ' + attributes + locked + ' value="' + (value == null ? '' : value) + '" aria-label="' + label + ' для ' + escapeHtml(entry.name) + '">';
  }
  function renderJournal() {
    var filtered = api.filterJournalEntries(journal, currentFilters());
    journalBody.innerHTML = filtered.map(function (entry) {
      var action = entry.productionConfirmed
        ? '<button class="link-button link-button--compact link-button--secondary calc-edit" type="button" data-id="' + entry.id + '" title="Редактировать" aria-label="Редактировать производственные данные">✎</button>'
        : '<button class="link-button link-button--compact calc-confirm" type="button" data-id="' + entry.id + '" title="Подтвердить" aria-label="Подтвердить производственные данные">✓</button>';
      var unitTime = entry.actualUnitTimeHours == null ? '—' : formatHours(entry.actualUnitTimeHours);
      var status = entry.productionConfirmed ? '' : '<small class="calc-production-status">Требуется подтверждение</small>';
      return '<tr data-id="' + entry.id + '"><td>' + formatNumber(entry.d, 0) + '</td><td>' + formatNumber(entry.D, 0) + '</td><td>' + formatNumber(entry.b, 0) + '</td><td>' + formatNumber(entry.heightMm, 2) + '</td><td>' + formatNumber(entry.standardCoefficient, 2) + '</td><td>×' + formatNumber(entry.complexityCoefficient, 2) + '</td><td>' + formatNumber(api.calculatedTimeHours(entry.totalTimeSeconds), 3) + '</td><td>' + productionInput('calc-batch-quantity', entry, entry.batchQuantity, 'Единиц в партии', 'min="1" step="1" inputmode="numeric"') + '</td><td>' + productionInput('calc-actual-batch-time', entry, entry.actualBatchTimeHours, 'Фактическое время партии в часах', 'min="0" step="0.01" inputmode="decimal"') + '</td><td><span class="calc-unit-time">' + unitTime + '</span>' + status + '<small class="calc-production-error" aria-live="polite"></small></td><td>' + action + '</td></tr>';
    }).join('');
    journalEmpty.hidden = filtered.length > 0;
    journalEmpty.textContent = journal.length ? 'По заданным условиям записей не найдено.' : 'Записей пока нет. Выполните расчёт, чтобы добавить магнитопровод в журнал.';
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
  function magneticCoreName(data) { return String(data.d) + '/' + String(data.D) + '-' + String(data.b); }
  async function addCurrentCalculation() {
    if (!latestCalculation) return;
    var name = magneticCoreName(latestCalculation.data);
    // The server enforces the same uniqueness; this only avoids a pointless
    // round trip for a case already visible in the loaded journal.
    if (api.journalHasName(journal, name)) return;
    setStatus('Сохранение расчёта…', 'busy');
    try {
      var payload = await journalApi.create(api.buildEntryPayload(name, latestCalculation.data, latestCalculation.result));
      applyEntry(payload.entry);
      setStatus(payload.created
        ? 'Магнитопровод ' + payload.entry.name + ' добавлен в «Проработку».'
        : 'Магнитопровод ' + payload.entry.name + ' уже есть в «Проработке».');
    } catch (error) { setStatus(error.message, 'error'); }
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
  Object.keys(journalFilters).forEach(function (key) { journalFilters[key].addEventListener('input', renderJournal); });

  journalBody.addEventListener('click', async function (event) {
    var button = event.target;
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
      var errors = api.validateProductionData(batchQuantity, actualBatchTimeHours);
      if (Object.keys(errors).length) {
        errorEl.textContent = Object.keys(errors).map(function (key) { return errors[key]; }).join(' ');
        return;
      }
      applyEntry((await journalApi.confirmProduction(id, Number(batchQuantity), Number(actualBatchTimeHours))).entry);
      setStatus('Производственные данные сохранены.');
    } catch (error) {
      errorEl.textContent = error.message;
      setStatus(error.message, 'error');
    } finally { setBusy(false); }
  });

  reloadButton.addEventListener('click', function () { if (!busy) loadJournal(); });

  exportButton.addEventListener('click', async function () {
    if (busy) return;
    setBusy(true);
    setStatus('Формирование выгрузки…', 'busy');
    try {
      // Downloaded through fetch so a refusal ("нет подтверждённых строк")
      // arrives as a message instead of replacing the page with raw JSON.
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
