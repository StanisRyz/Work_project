/**
 * Journal helpers, ported from `js/journal.js` and reshaped for «Проработка» V2.
 *
 * The duplicate check that used to live here is gone: whether a calculation
 * is already in the journal depends on the whole calculation case — geometry,
 * tape thickness and calibration — and that question is answered by the
 * server, which owns the signature. The tab simply posts and is told whether
 * a row was created.
 *
 * Search filters by the visible `d/D-b` name only, and never collapses rows:
 * two lines with the same name are two lines.
 */
(function () {
  var api = window.windingCalculator = window.windingCalculator || {};

  function positiveInteger(value) { var number = Number(value); return String(value).trim() !== '' && Number.isInteger(number) && number > 0; }
  function positiveDecimal(value) { var number = Number(value); return String(value).trim() !== '' && Number.isFinite(number) && number > 0; }
  function normalized(value) { return String(value == null ? '' : value).replace(/\s+/g, '').toLowerCase(); }

  api.calculatedTimeHours = function (totalTimeSeconds) { return Number(totalTimeSeconds) / 3600; };
  api.magneticCoreName = function (data) { return String(data.d) + '/' + String(data.D) + '-' + String(data.b); };

  api.validateProductionData = function (batchQuantity, actualBatchTimeHours, oneCExpression) {
    var errors = {};
    if (!positiveInteger(batchQuantity)) errors.batchQuantity = 'Введите целое число больше нуля.';
    if (!positiveDecimal(actualBatchTimeHours)) errors.actualBatchTimeHours = 'Введите время в часах больше нуля.';
    // Optional: only a filled-in field that does not parse is an error.
    if (Number.isNaN(api.evaluateOneC(oneCExpression))) errors.oneCExpression = '1С: допустимы числа, + − × ÷ и скобки.';
    return errors;
  };

  /**
   * The parameters of a row added by hand. `calibration` follows the
   * calculator form's own vocabulary so the same engine consumes it, and a
   * calibration of `0` simply means the core is not calibrated.
   */
  api.validateManualInput = function (raw) {
    var errors = {}, values = {};
    ['d', 'D', 'b', 'tapeThicknessMm'].forEach(function (key) {
      values[key] = Number(String(raw[key]).replace(',', '.'));
      if (String(raw[key]).trim() === '' || !Number.isFinite(values[key]) || values[key] <= 0) errors[key] = 'Введите положительное число.';
    });
    if (!errors.d && !errors.D && values.D <= values.d) errors.D = 'D должен быть больше d.';
    var calibration = String(raw.calibrationDiameterMm).trim() === '' ? 0 : Number(String(raw.calibrationDiameterMm).replace(',', '.'));
    if (!Number.isFinite(calibration) || calibration < 0) errors.calibrationDiameterMm = 'Калибровка не может быть отрицательной.';
    if (Object.keys(errors).length) return { errors: errors, data: null };
    return {
      errors: errors,
      data: {
        d: values.d, D: values.D, b: values.b,
        windingHeightMm: api.calculateHeightMm(values.d, values.D),
        tapeThicknessMm: values.tapeThicknessMm,
        calibration: calibration > 0 ? 'Да' : 'Нет',
        calibrationDiameterMm: calibration
      }
    };
  };

  /** The payload for a new case. No id and no production data: both belong to the server. */
  api.buildEntryPayload = function (name, input, result) {
    return {
      name: name,
      d: input.d, D: input.D, b: input.b, tapeThicknessMm: input.tapeThicknessMm,
      heightMm: result.heightMm, standardCoefficient: result.standard,
      calibrationEnabled: input.calibration === 'Да',
      calibrationDiameterMm: input.calibration === 'Да' ? input.calibrationDiameterMm : null,
      rawComplexityCoefficient: result.rawComplexityCoefficient,
      windingTimeSeconds: result.windingTimeSeconds,
      additionalOperationsTimeSeconds: result.additionalOperationsTimeSeconds,
      totalTimeSeconds: result.totalTimeSeconds
    };
  };

  api.filterJournalEntries = function (entries, filters) {
    var name = normalized(filters.name);
    if (!name) return entries.slice();
    return entries.filter(function (entry) { return normalized(entry.name).indexOf(name) !== -1; });
  };
})();
