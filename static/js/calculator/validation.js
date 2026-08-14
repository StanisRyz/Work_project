/**
 * Immediate form validation, ported from `js/validation.js`. Convenience
 * only: `calculator/services.py` validates the same rules again before
 * anything is stored.
 */
(function () {
  var api = window.windingCalculator = window.windingCalculator || {};

  api.calculateHeightMm = function (d, D) { return (D - d) / 2; };
  api.validateInput = function (data) {
    var errors = {};
    ['d', 'D', 'b', 'tapeThicknessMm'].forEach(function (key) {
      if (!Number.isFinite(data[key]) || data[key] <= 0) errors[key] = 'Введите положительное число.';
    });
    if (Number.isFinite(data.d) && Number.isFinite(data.D) && data.D <= data.d) errors.D = 'Внешний диаметр D должен быть больше внутреннего диаметра d.';
    if (!data.calibration) errors.calibration = 'Выберите значение калибровки.';
    if (data.calibration === 'Да' && (!Number.isFinite(data.calibrationDiameterMm) || data.calibrationDiameterMm <= 0)) errors.calibrationDiameterMm = 'Введите положительный диаметр калибровки.';
    return errors;
  };
})();
