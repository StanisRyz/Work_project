/**
 * Journal helpers, ported from `js/journal.js`. Filtering, the duplicate
 * check and the production-data validation are unchanged.
 *
 * What is gone is what only existed for the JSON file: entry ids are now the
 * server primary key, entries arrive already normalized from Django, and the
 * export is built by `calculator/export.py` from the database.
 */
(function () {
  var api = window.windingCalculator = window.windingCalculator || {};

  function positiveInteger(value) { var number = Number(value); return String(value).trim() !== '' && Number.isInteger(number) && number > 0; }
  function positiveDecimal(value) { var number = Number(value); return String(value).trim() !== '' && Number.isFinite(number) && number > 0; }

  api.calculatedTimeHours = function (totalTimeSeconds) { return Number(totalTimeSeconds) / 3600; };
  api.validateProductionData = function (batchQuantity, actualBatchTimeHours) {
    var errors = {};
    if (!positiveInteger(batchQuantity)) errors.batchQuantity = 'Введите целое число больше нуля.';
    if (!positiveDecimal(actualBatchTimeHours)) errors.actualBatchTimeHours = 'Введите время в часах больше нуля.';
    return errors;
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
  api.journalHasName = function (entries, name) {
    var normalizedName = String(name).replace(/\s+/g, '').toLowerCase();
    return entries.some(function (entry) { return String(entry.name).replace(/\s+/g, '').toLowerCase() === normalizedName; });
  };
  api.filterJournalEntries = function (entries, filters) {
    var name = String(filters.name || '').replace(/\s+/g, '').toLowerCase();
    return entries.filter(function (entry) {
      if (name && String(entry.name).replace(/\s+/g, '').toLowerCase().indexOf(name) === -1) return false;
      return ['d', 'D', 'b'].every(function (key) { var value = filters[key]; return value === '' || value == null || Number(entry[key]) === Number(value); });
    });
  };
})();
