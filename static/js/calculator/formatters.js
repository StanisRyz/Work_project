/** Number and duration formatting, ported from `js/formatters.js`. */
(function () {
  var api = window.windingCalculator = window.windingCalculator || {};

  api.formatNumber = function (value, digits) {
    return Number(value).toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  };
  api.formatDuration = function (seconds) {
    var totalSeconds = Math.round(seconds), hours = Math.floor(totalSeconds / 3600), mins = Math.floor((totalSeconds % 3600) / 60), secs = totalSeconds % 60;
    return hours + ' ч ' + mins + ' мин ' + secs + ' с';
  };
})();
